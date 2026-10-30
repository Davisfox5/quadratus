"""The integration gate: does the assembled project actually work?

Tasks are sequenced, reviewed, and revised -- but until something *executes*
the combined result, fit between pieces is verified only by models reading.
This gate is the deterministic half: after a task's work is final, the
harness runs the project's own check command (its test suite, its build,
whatever the operator configures) and the outcome is evidence, not opinion.

Deliberately dumb, like the browser evidence: no model in the loop, no
judgement, no retries of its own. It runs one command and reports what
happened, including on timeout or a missing binary -- a gate that crashes
the run has negative value, so every failure mode becomes a failed
:class:`GateResult` rather than an exception.
"""

from __future__ import annotations

import hashlib
import json
import os
import posixpath
import re
import shlex
import shutil
import subprocess
import tempfile
from contextlib import contextmanager
from dataclasses import asdict, dataclass, replace
from pathlib import Path, PurePosixPath
from typing import List, Literal, Optional, Sequence

__all__ = ["GateResult", "IntegrationGate", "GateCommand", "GateReceipt", "GateSuite", "REPORT_TOKEN",
           "CheckUnattributable", "attribute", "read_report", "model_facing"]

#: The argv token a check uses to declare the harness's structured report.
#: Its producers are the harness-owned ones in ``_gate_producer`` (the pytest
#: plugin, the node:test reporter and the unittest runner module), each
#: declared as ``--quadratus-report={report}``. The harness replaces the token
#: with a path it owns, outside the project, loads the producer for that one
#: invocation, and removes the path after reading it (phase 3, #25). Other
#: runners have no producer: their failures are never attributable, so never
#: repaired.
REPORT_TOKEN = "{report}"
#: How a command declares the harness report in its argv.
REPORT_TOKEN_PREFIX = "--quadratus-report"
PRODUCER = "quadratus-pytest/7"
#: The node:test producer (series rule-58a4625 f3: the second cycle in a row
#: ended on a failing node UI test with the work done and the graders
#: passing, because a node check had no producer and so no fix call). A
#: ``node --test`` check declares ``--test-reporter-destination={report}``;
#: the harness copies the shipped reporter and passes it as a second
#: reporter beside TAP on stdout, so the text output the count reader needs
#: is unchanged.
PRODUCER_NODE = "quadratus-node/1"
#: The unittest producer (R21: a required ``python -m unittest discover``
#: check failed with no report of its own, so a plain test-body assertion
#: could not reach a repair). A ``python -m unittest ...`` check declares
#: ``--quadratus-report={report}``; the harness runs the same arguments under
#: the same interpreter through the shipped runner module, which is
#: ``unittest.main(module=None)`` with a recording result class, and writes
#: this invocation's own report. Undeclared, unittest stays unattributable.
PRODUCER_UNITTEST = "quadratus-unittest/3"
PRODUCERS = (PRODUCER, PRODUCER_NODE, PRODUCER_UNITTEST)
_PRODUCER_FILE = Path(__file__).resolve().parent / "_gate_producer" / "quadratus_gate_report.py"
_NODE_PRODUCER_FILE = _PRODUCER_FILE.with_suffix(".mjs")
_UNITTEST_PRODUCER_FILE = _PRODUCER_FILE.with_name("quadratus_unittest_report.py")
#: Kept for the install-layout proof: where the shipped producer lives.
_PRODUCER_DIR = str(_PRODUCER_FILE.parent)
_REPORT_FAILURES = 200
_WHEN = ("setup", "call", "teardown")
_COUNTS = ("passed", "failed", "errors", "skipped")
_REPORT_MAX_BYTES = 1024 * 1024
#: The producers' bound on named passed cases (MAX_PASSED in each).
_REPORT_PASSED = 3000
#: The producers' bounds on attested classes and members (MAX_CLASSES,
#: MAX_CLASS_MEMBERS in each).
_REPORT_CLASSES = 1000
_REPORT_CLASS_MEMBERS = 1000


#: How much command output a failed gate carries back. The tail, because test
#: runners put the summary and the first failures at the end.
_TAIL_CHARS = 2_000


class CheckUnattributable(RuntimeError):
    """A check failed in a way no application repair may address: an
    operator handoff with diagnostics, never a gate-fix call."""


@contextmanager
def _report_slot(argv, env):
    """``argv`` and ``env`` for one invocation: with the report token
    replaced by a harness-owned path and the producer loaded, when the check
    declares it. Yields (argv, env, path, expected); path and expected are
    None otherwise.

    The shipped producer is copied into a directory made for this
    invocation, under a module name derived from a fresh nonce, so a project
    module cannot shadow it by name or import order; ``expected`` holds the
    nonce, the copy's real path and the shipped source digest the report
    must state. Binding, not secrecy: test code in the same process that is
    written to forge a report can do so, as it could fake a pass."""
    argv = list(argv)
    if not any(REPORT_TOKEN in a for a in argv):
        yield argv, env, None, None
        return
    import secrets
    owned = tempfile.mkdtemp(prefix="quadratus-report-")
    nonce = secrets.token_hex(16)
    path = os.path.join(owned, "report.json")
    if _is_unittest(argv):
        module = f"quadratus_unittest_report_{secrets.token_hex(8)}"
        source = _UNITTEST_PRODUCER_FILE.read_bytes()
        copy = os.path.join(owned, module + ".py")
        with open(copy, "wb") as out:
            out.write(source)
        expected = dict(nonce=nonce, module_file=os.path.realpath(copy), producer=PRODUCER_UNITTEST,
                        module_sha256=hashlib.sha256(source).hexdigest())
        # The same interpreter and the same unittest arguments, with only
        # the declaring argument removed; the owned directory goes after
        # the working directory ``-m`` puts first, so no project module is
        # shadowed and none can take the nonce name.
        rest = [a for a in argv if REPORT_TOKEN not in a]
        argv = [rest[0], "-m", module] + rest[3:]
        env = dict(env, QUADRATUS_GATE_NONCE=nonce, QUADRATUS_GATE_REPORT=path,
                   PYTHONPATH=os.pathsep.join([owned] + [p for p in [env.get("PYTHONPATH")] if p]))
        try:
            yield argv, env, path, expected
        finally:
            shutil.rmtree(owned, ignore_errors=True)
        return
    if _is_node_test(argv):
        source = _NODE_PRODUCER_FILE.read_bytes()
        copy = os.path.join(owned, f"quadratus_gate_report_{secrets.token_hex(8)}.mjs")
        with open(copy, "wb") as out:
            out.write(source)
        expected = dict(nonce=nonce, module_file=os.path.realpath(copy), producer=PRODUCER_NODE,
                        module_sha256=hashlib.sha256(source).hexdigest())
        # The declaring argument is replaced, never kept: node pairs each
        # --test-reporter with the next destination, so the runner's own TAP
        # stays on stdout (the count reader needs it) and the harness's
        # reporter writes the report. Placed directly after --test: node
        # reads an option after the first test file as another file and
        # the reporter never loads (probed on node 22).
        rest = [a for a in argv if REPORT_TOKEN not in a]
        at = rest.index("--test") + 1
        argv = rest[:at] + ["--test-reporter=tap", "--test-reporter-destination=stdout",
                            f"--test-reporter={copy}", f"--test-reporter-destination={path}"] + rest[at:]
        env = dict(env, QUADRATUS_GATE_NONCE=nonce)
        try:
            yield argv, env, path, expected
        finally:
            shutil.rmtree(owned, ignore_errors=True)
        return
    module = f"quadratus_gate_report_{secrets.token_hex(8)}"
    source = _PRODUCER_FILE.read_bytes()
    copy = os.path.join(owned, module + ".py")
    with open(copy, "wb") as out:
        out.write(source)
    expected = dict(nonce=nonce, module_file=os.path.realpath(copy), producer=PRODUCER,
                    module_sha256=hashlib.sha256(source).hexdigest())
    plugins = [p for p in env.get("PYTEST_PLUGINS", "").split(",") if p.strip()]
    env = dict(env, QUADRATUS_GATE_NONCE=nonce, PYTEST_PLUGINS=",".join(plugins + [module]),
               PYTHONPATH=os.pathsep.join([owned] + [p for p in [env.get("PYTHONPATH")] if p]))
    try:
        yield [a.replace(REPORT_TOKEN, path) for a in argv], env, path, expected
    finally:
        shutil.rmtree(owned, ignore_errors=True)


def _declared_producer(argv) -> Path:
    """The shipped producer file ``_report_slot`` copies for ``argv``."""
    argv = [str(a) for a in argv]
    if _is_unittest(argv):
        return _UNITTEST_PRODUCER_FILE
    return _NODE_PRODUCER_FILE if _is_node_test(argv) else _PRODUCER_FILE


def _is_unittest(argv) -> bool:
    """``python -m unittest ...`` under a plain ``python``/``pythonX[.Y]``
    interpreter, the only unittest form with a producer; any other runner,
    wrapper or custom script is not one."""
    head = Path(argv[0]).name if argv else ""
    return bool(re.fullmatch(r"python(\d+(\.\d+)*)?", head)) and list(argv[1:3]) == ["-m", "unittest"]


def _is_node_test(argv) -> bool:
    """A ``node --test`` invocation (the runner is node and ``--test`` is an
    argument of its own), the only non-pytest runner with a producer."""
    head = Path(argv[0]).name if argv else ""
    return head in ("node", "nodejs") and "--test" in argv[1:]


def checks_digest(commands) -> str:
    """A digest of the approved commands as they are configured (argv, cwd,
    timeout, required, minimum, skip): what a scratch proof was run with."""
    from dataclasses import is_dataclass
    body = [asdict(c) if is_dataclass(c) else repr(c) for c in commands or ()]
    return hashlib.sha256(json.dumps(body, sort_keys=True, default=str).encode()).hexdigest()[:32]


def model_facing(argv):
    """The check as a model may run it: the harness's report declaration
    removed. The token names nothing outside the harness's own run (series
    rule-58a4625 f3: two leads ran the pytest check with the literal token
    and got exit 4, unrecognized arguments)."""
    return [a for a in argv if REPORT_TOKEN not in a]


def _read_capped(path):
    """The report's bytes, read without following a link, without blocking
    on a FIFO or device, and never more than the cap plus one byte: from a
    descriptor whose regular-file identity matches what was stat'ed.
    Returns (bytes, None) or (None, (state, detail))."""
    import stat as st
    try:
        before = os.lstat(path)
    except OSError:
        return None, ("missing", "")
    if not st.S_ISREG(before.st_mode):
        return None, ("unparsable", "not a regular file")
    try:
        fd = os.open(path, os.O_RDONLY | os.O_NONBLOCK | getattr(os, "O_NOFOLLOW", 0))
    except OSError as exc:
        return None, ("unparsable", f"{type(exc).__name__}: {str(exc)[:120]}")
    try:
        opened = os.fstat(fd)
        if (not st.S_ISREG(opened.st_mode) or (opened.st_dev, opened.st_ino) != (before.st_dev, before.st_ino)):
            return None, ("unparsable", "replaced while being opened")
        chunks, total = [], 0
        while total <= _REPORT_MAX_BYTES:
            chunk = os.read(fd, min(65536, _REPORT_MAX_BYTES + 1 - total))
            if not chunk:
                break
            chunks.append(chunk)
            total += len(chunk)
        if total > _REPORT_MAX_BYTES:
            return None, ("unparsable", f"more than {_REPORT_MAX_BYTES} bytes")
        return b"".join(chunks), None
    except OSError as exc:
        return None, ("unparsable", f"{type(exc).__name__}: {str(exc)[:120]}")
    finally:
        os.close(fd)


def _is_count(value) -> bool:
    return type(value) is int and value >= 0


def _malformed(data) -> str:
    """Why a report's fields are malformed or impossible; "" when sound."""
    counts, failures = data.get("counts"), data.get("failures")
    if not (type(data.get("exitstatus")) is int and 0 <= data["exitstatus"] <= 5):
        return "exitstatus"
    if not _is_count(data.get("collected")) or not _is_count(data.get("collect_errors")):
        return "collected or collect_errors"
    if not isinstance(counts, dict) or set(counts) != set(_COUNTS) or not all(_is_count(counts[k]) for k in _COUNTS):
        return "counts"
    if type(data.get("truncated")) is not bool:
        return "truncated"
    if not isinstance(failures, list) or len(failures) > _REPORT_FAILURES:
        return "failures"
    for f in failures:
        if (not isinstance(f, dict) or not isinstance(f.get("nodeid"), str) or f.get("when") not in _WHEN
                or type(f.get("assertion")) is not bool
                or not (f.get("body") is None or type(f.get("body")) is bool)
                or not (f.get("exc_type") is None or isinstance(f.get("exc_type"), str))):
            return "a failure record"
    if counts["passed"] + counts["failed"] + counts["skipped"] > data["collected"]:
        return "more executed than collected"
    in_call = sum(1 for f in failures if f["when"] == "call")
    if in_call > counts["failed"] or len(failures) - in_call > counts["errors"]:
        return "more failure records than failures"
    if not data["truncated"] and len(failures) != counts["failed"] + counts["errors"]:
        return "failure records do not match the counts"
    if data["truncated"] and len(failures) != _REPORT_FAILURES:
        return "truncation flag without a full record"
    if data.get("producer") in (PRODUCER, PRODUCER_UNITTEST):
        # Named passed cases (root review of 5ca254c): one per counted pass,
        # each distinct, or a full bounded list flagged truncated.
        named, cut = data.get("passed_cases"), data.get("passed_truncated")
        if (not isinstance(named, list) or type(cut) is not bool or len(named) > _REPORT_PASSED
                or not all(isinstance(n, str) and 0 < len(n) <= 300 for n in named)
                or len(set(named)) != len(named)):
            return "passed cases"
        if (len(named) != counts["passed"]) if not cut else (len(named) != _REPORT_PASSED
                                                              or counts["passed"] <= _REPORT_PASSED):
            return "passed cases do not match the counts"
        if "classes" in data or "classes_truncated" in data:
            # Attested class membership (class-claim correction, 2026-10-30):
            # distinct class ids, each with a distinct non-empty bounded list
            # of ``test*`` member names; a report that states the field
            # states it whole or flags it truncated.
            classes, cut = data.get("classes"), data.get("classes_truncated")
            if (not isinstance(classes, list) or type(cut) is not bool or len(classes) > _REPORT_CLASSES
                    or (cut and len(classes) != _REPORT_CLASSES)):
                return "classes"
            ids = []
            for record in classes:
                members = record.get("members") if isinstance(record, dict) else None
                if (not isinstance(record, dict) or not isinstance(record.get("id"), str)
                        or not 0 < len(record["id"]) <= 300 or "::" not in record["id"]
                        or not isinstance(members, list) or not 0 < len(members) <= _REPORT_CLASS_MEMBERS
                        or not all(isinstance(m, str) and m.startswith("test") and 0 < len(m) <= 300 for m in members)
                        or len(set(members)) != len(members)):
                    return "a class record"
                ids.append(record["id"])
            if len(set(ids)) != len(ids):
                return "duplicate class records"
    return ""


def read_report(path, expected) -> dict:
    """The producer's report, bounded and validated. ``state``: undeclared,
    missing, unparsable (unreadable, oversized, malformed or impossible),
    foreign (another producer, invocation or module identity) or parsed."""
    if path is None:
        return dict(state="undeclared")
    raw, problem = _read_capped(path)
    if problem:
        return dict(state=problem[0], **({"detail": problem[1]} if problem[1] else {}))
    try:
        data = json.loads(raw.decode("utf-8"))
    except (ValueError, UnicodeDecodeError, RecursionError) as exc:
        return dict(state="unparsable", detail=f"{type(exc).__name__}: {str(exc)[:160]}")
    if not isinstance(data, dict):
        return dict(state="unparsable", detail="not an object")
    expected = expected or {}
    if (data.get("producer") not in PRODUCERS or data.get("producer") != expected.get("producer", data.get("producer"))
            or data.get("nonce") != expected.get("nonce")
            or data.get("module_file") != expected.get("module_file")
            or data.get("module_sha256") != expected.get("module_sha256")):
        return dict(state="foreign", detail=f"producer {str(data.get('producer'))[:40]!r}, module "
                                            f"{str(data.get('module_file'))[-60:]!r}")
    problem = _malformed(data)
    if problem:
        return dict(state="unparsable", detail=f"malformed or impossible: {problem}")
    return dict(state="parsed", exitstatus=data["exitstatus"], collected=data["collected"],
                collect_errors=data["collect_errors"], counts={k: data["counts"][k] for k in _COUNTS},
                failures=[dict(nodeid=f["nodeid"][:300], when=f["when"], exc_type=(f["exc_type"] or "")[:120],
                               assertion=f["assertion"], body=f.get("body"))
                          for f in data["failures"]],
                truncated=data["truncated"],
                **({"passed_cases": list(data["passed_cases"]), "passed_truncated": data["passed_truncated"]}
                   if "passed_cases" in data and data.get("producer") in (PRODUCER, PRODUCER_UNITTEST) else {}),
                **({"classes": [dict(id=c["id"], members=list(c["members"])) for c in data["classes"]],
                    "classes_truncated": data["classes_truncated"]}
                   if "classes" in data and data.get("producer") in (PRODUCER, PRODUCER_UNITTEST) else {}))


def _report_reasons(gid, status, reason, returncode, report) -> list:
    """Why one failing check is not an attributable assertion failure."""
    report = report or dict(state="undeclared")
    if status != "failed" or reason != "nonzero exit":
        return [f"{gid}: {status}: {reason}"]
    if report["state"] != "parsed":
        return [f"{gid}: structured report {report['state']}"
                + (f" ({report['detail']})" if report.get("detail") else "")]
    counts, failures, found = report["counts"], report["failures"], []
    if returncode != 1 or report["exitstatus"] != 1:
        found.append(f"{gid}: exit {returncode}, reported exit status {report['exitstatus']}; "
                     "only 1 (tests failed) is a test failure")
    if report["collect_errors"]:
        found.append(f"{gid}: {report['collect_errors']} collection error(s)")
    if counts["errors"]:
        found.append(f"{gid}: {counts['errors']} setup or teardown error(s)")
    if (report["truncated"] or not report["collected"] or not counts["failed"]
            or counts["failed"] + counts["errors"] != len(failures)
            or counts["passed"] + counts["failed"] + counts["skipped"] > report["collected"]):
        found.append(f"{gid}: inconsistent, truncated or empty failure record ({report['collected']} "
                     f"collected, {counts['failed']} failed, {len(failures)} recorded)")
    # The producer records whether the exception's type *is* AssertionError;
    # a name is only shown, never trusted.
    other = sorted({f["exc_type"] + (" raised outside the test body"
                                     if f["when"] == "call" and f.get("body") is False else "")
                    for f in failures if f["when"] != "call" or not f["assertion"]})
    if other:
        found.append(f"{gid}: failures that are not assertions: {', '.join(other)[:200]}")
    return found


def attribute(result) -> dict:
    """Whether a failed gate is a product failure a repair may address.

    Only facts decide, never prose, counts alone or message wording: every
    failed required check exited 1 on its own, declared the harness report,
    and that report (this invocation's, by nonce) shows only test-body
    failures raised as a plain ``AssertionError`` whose traceback passes
    through the test function's own frame (a call-phase hook that asserts
    before the body runs is not the test), with no collection, setup
    or teardown error and a consistent record. Anything else is not the
    application's to repair, and the reasons say why. A pass is not product.
    """
    if result.passed:
        return dict(product=False, reasons=[])
    if result.receipts:
        failing = [(r.id, r.status, r.reason, r.returncode, r.report) for r in result.receipts
                   if r.required and r.status != "passed"]
    else:
        status = "failed" if result.returncode not in (None, 0) else "error"
        reason = "nonzero exit" if status == "failed" else (result.output or "no exit status")[:120]
        failing = [("check", status, reason, result.returncode, result.report)]
    reasons = [] if failing else ["the gate failed with no failing required check"]
    for entry in failing:
        reasons += _report_reasons(*entry)
    return dict(product=not reasons, reasons=reasons)


#: Seconds a gate command's own processes get to exit after SIGTERM, then
#: after SIGKILL, and the readers to drain their pipes once they are gone.
_TERM_GRACE, _KILL_GRACE, _DRAIN_GRACE = 2.0, 1.0, 2.0


class _GateTimeout(Exception):
    def __init__(self, output, cleanup):
        super().__init__('gate command timed out')
        self.output, self.cleanup = output, cleanup


def _group_members(pgid: int) -> Optional[dict]:
    """pid -> start-time text of every live process in process group
    ``pgid``, read from ``ps``; None when the process list cannot be read."""
    try:
        proc = subprocess.run(['ps', '-A', '-o', 'pid=,pgid=,lstart='], capture_output=True,
                              text=True, timeout=10, check=False)
    except (OSError, subprocess.SubprocessError):
        return None
    if proc.returncode != 0:
        return None
    members = {}
    for line in proc.stdout.splitlines():
        parts = line.split(None, 2)
        if len(parts) == 3 and parts[1] == str(pgid) and parts[0].isdigit():
            members[int(parts[0])] = parts[2].strip()
    return members


def _signal_member(pgid: int, pid: int, started: str, sig) -> bool:
    """Signal ``pid`` only if, read again just now, it is still in ``pgid``
    with the start time it was seen with. Not atomic with the signal."""
    now = _group_members(pgid)
    if not now or now.get(pid) != started:
        return False
    try:
        os.kill(pid, sig)
        return True
    except OSError:
        return False


def _stop_owned(pgid: int) -> dict:
    """Stop the processes a gate command left in the process group made for
    it alone (``start_new_session``), one verified pid at a time, never a
    whole-group signal. The group is scanned again until it is empty or the
    bounded grace ends, so a member started during cleanup (a child that
    answers SIGTERM by spawning another, Codex review of 574edfb) is found
    and signalled too: SIGTERM within the first grace, SIGKILL to anything
    still there in the second. A process that left the group (its own
    setsid) is not touched. ``complete`` is true only when a final read of
    the process list shows the group empty (Codex, run
    20261010T225947Z-fec6da52: a timed-out original-test check left Node
    and two Python previews alive)."""
    import signal
    import time
    record = dict(found=[], terminated=[], killed=[], remaining=[], unreadable=False, complete=False)
    for grace, sig, key in ((_TERM_GRACE, signal.SIGTERM, 'terminated'), (_KILL_GRACE, signal.SIGKILL, 'killed')):
        end = time.monotonic() + grace
        signalled = set()
        while True:
            members = _group_members(pgid)
            if members is None:
                record['unreadable'] = True
                break
            if not members:
                break
            for pid, started in members.items():
                if pid not in record['found']:
                    record['found'].append(pid)
                if (pid, started) in signalled:
                    continue
                if _signal_member(pgid, pid, started, sig):
                    record[key].append(pid)
                signalled.add((pid, started))
            if time.monotonic() >= end:
                break
            time.sleep(0.05)
        if record['unreadable']:
            break
    final = _group_members(pgid)
    if final is None:
        record['unreadable'] = True
    else:
        record['remaining'] = sorted(final)
    record['complete'] = not record['unreadable'] and not record['remaining']
    return record


def _run_owned(argv, *, cwd, timeout, env):
    """Run one gate command and return ``(returncode, output, cleanup)``;
    ``returncode`` is None when it timed out.

    The command runs in a new session, so the group holds it and every
    descendant that did not leave on its own. Output is read as it arrives,
    so a timeout keeps what was printed before it. When the command times
    out, or exits leaving its own processes behind (a preview server holding
    its pipes open), those processes are stopped by :func:`_stop_owned` and
    ``cleanup`` says which. Nothing outside the group is signalled."""
    import threading
    import time
    proc = subprocess.Popen(argv, cwd=cwd, env=env, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                            stderr=subprocess.PIPE, start_new_session=True)
    chunks = {'out': [], 'err': []}

    def drain(stream, key):
        for block in iter(lambda: stream.read1(65536) if hasattr(stream, 'read1') else stream.read(65536), b''):
            chunks[key].append(block)
    readers = [threading.Thread(target=drain, args=(proc.stdout, 'out'), daemon=True),
               threading.Thread(target=drain, args=(proc.stderr, 'err'), daemon=True)]
    for reader in readers:
        reader.start()
    deadline = time.monotonic() + timeout
    returncode, cleanup = None, None
    try:
        try:
            returncode = proc.wait(timeout=max(0.0, deadline - time.monotonic()))
        except subprocess.TimeoutExpired:
            returncode = None
        for reader in readers:
            reader.join(max(0.0, deadline - time.monotonic()) if returncode is not None else 0)
        if returncode is None or any(r.is_alive() for r in readers) or _group_members(proc.pid) != {}:
            if returncode is not None and any(r.is_alive() for r in readers):
                returncode = None  # the pipes outlived the deadline: a timeout
            cleanup = _stop_owned(proc.pid)
    finally:
        if proc.poll() is None:
            proc.kill()
            proc.wait()
        for reader in readers:
            reader.join(_DRAIN_GRACE)
    text = lambda key: b''.join(chunks[key]).decode('utf-8', errors='replace')  # noqa: E731
    output = (text('out') + '\n' + text('err')).strip()
    if cleanup is not None and any(r.is_alive() for r in readers):
        cleanup['pipes_still_held'] = True
    return returncode, output, cleanup


def _cleanup_note(cleanup) -> str:
    if not cleanup:
        return ''
    note = (f"(owned processes left by the command: found {len(cleanup['found'])}, "
            f"terminated {len(cleanup['terminated'])}, killed {len(cleanup['killed'])}, "
            + ("process list unreadable, cleanup NOT confirmed" if cleanup.get('unreadable') else
               f"still running {len(cleanup['remaining'])}"
               + ("" if cleanup.get('complete') else ", cleanup INCOMPLETE"))
            + ('; output pipes still held by a process outside the command\'s group' if
               cleanup.get('pipes_still_held') else '') + ')')
    return note


@contextmanager
def _fresh_bytecode_env():
    """An environment in which standard CPython source-module imports read
    and write cached ``.pyc`` files only in a private directory made for this
    one execution. Custom or sourceless loaders, and a child process that
    resets its own environment, are outside what this controls.

    CPython trusts a cached module whose recorded source size and mtime
    match, so a same-size edit in the same second as the last import ran the
    old bytecode and a gate passed on broken source (Codex confirmation on
    #25). ``-B`` and ``PYTHONDONTWRITEBYTECODE`` only stop writes; reads of
    an existing cache go on. A fresh ``PYTHONPYCACHEPREFIX`` redirects every
    cache lookup to an empty, owner-private directory that is removed
    afterwards; only that directory is ever deleted, never a project's or a
    dependency's own caches. An inherited prefix is overridden, not reused.
    """
    prefix = tempfile.mkdtemp(prefix="quadratus-gate-pyc-")
    try:
        env = dict(os.environ, PYTHONPYCACHEPREFIX=prefix, PYTHONDONTWRITEBYTECODE="1")
        yield env
    finally:
        shutil.rmtree(prefix, ignore_errors=True)


@dataclass(frozen=True)
class GateResult:
    """One gate run. Facts only."""

    passed: bool
    command: str
    returncode: Optional[int]
    #: The tail of combined stdout/stderr -- what a person would read first.
    output: str
    receipts: tuple[GateReceipt, ...] = ()
    #: The single-command gate's structured report (read_report); a suite
    #: carries one per receipt instead.
    report: Optional[dict] = None
    #: Case names read from the whole output before the tail is kept
    #: (case_record); a suite carries one per receipt instead.
    cases: Optional[dict] = None

    def render(self) -> str:
        """The operator's view: the exact command, for the run record."""
        status = "PASSED" if self.passed else "FAILED"
        lines = [f"Integration gate {status}: `{self.command}`"]
        if not self.passed and self.output:
            lines.append(self.output)
        return "\n".join(lines)

    def for_models(self) -> str:
        """What a seat may see: gate names and outcomes, never the command.

        A gate's command can name a file the seats must not open. In the Q9-v2
        native runs the grader's absolute path reached every lead through this
        text and the role packet, and seven baseline leads read the grader.
        Absolute paths from the command, and their folders, are redacted from
        the output tail as well, since a failing test prints its own path.
        """
        status = "PASSED" if self.passed else "FAILED"
        names = ", ".join(r.id for r in self.receipts) or "project check"
        failed = [r.id for r in self.receipts if r.status not in ("passed", "skipped")]
        passed = [r.id for r in self.receipts if r.status == "passed"]
        if not self.passed and failed and passed:
            # Run 19: "FAILED: check, extra1, extra2" sent a fix call after
            # checks that had passed. The heading names what failed.
            names = ", ".join(failed) + " (passed: " + ", ".join(passed) + ")"
        lines = [f"Integration gate {status}: {names}"]
        for r in self.receipts:
            lines.append(f"{r.id}: {r.status}: {r.reason}"
                         + (f" ({r.tests} tests)" if r.tests is not None else ""))
        if not self.passed and self.output:
            lines.append(redact_command_paths(self.output, self.command_paths()))
        return "\n".join(lines)

    def command_paths(self) -> list:
        """Absolute paths named by this result's commands, longest first."""
        import shlex
        tokens = []
        for text in [self.command, *(r.command for r in self.receipts)]:
            try:
                tokens += shlex.split(text or "")
            except ValueError:
                tokens += (text or "").split()
        paths = set()
        for token in tokens:
            if token.startswith("/") and len(token) > 1:
                paths.add(token.rstrip("/"))
                parent = token.rstrip("/").rsplit("/", 1)[0]
                if parent.count("/") >= 2:
                    paths.add(parent)
        return sorted(paths, key=len, reverse=True)


def redact_command_paths(text: str, paths) -> str:
    """Replace each absolute command path, and each folder holding one, with a
    marker; and each relative spelling of a command *file*, which a test
    runner prints relative to its cwd or rootdir (pytest:
    ``../examiner/grader.py:6``, or a bare ``grader.py::test`` when the
    grader's folder is the rootdir).

    Only the private file's own spellings are matched: any ``../`` prefix
    followed by a trailing run of its path components, or its bare name
    standing alone. A public file that shares the basename but sits under a
    different folder (``tests/grader.py``) keeps its diagnostics. A bare name
    is ambiguous by construction and is redacted: privacy over diagnostics.
    """
    for path in paths:
        text = text.replace(path, "<gate-path>")
    for path in paths:
        parts = [p for p in path.split("/") if p]
        if not parts or "." not in parts[-1]:
            continue
        for depth in range(len(parts), 1, -1):
            suffix = re.escape("/".join(parts[-depth:]))
            text = re.sub(r"(?<![\w.\-/])(?:\.\./)*(?:\./)?" + suffix + r"(?![\w\-])", "<gate-path>", text)
        text = re.sub(r"(?<![\w.\-/])" + re.escape(parts[-1]) + r"(?![\w\-])", "<gate-path>", text)
    return text


class IntegrationGate:
    """Runs the project's own check command and reports the outcome.

    Args:
        command: The check, as an argument list (never a shell string).
            Typically the project's test runner or build.
        cwd: Where to run it -- the project checkout.
        timeout: Seconds before the run counts as failed. A hung test suite
            must not hang the whole session.
    """

    def __init__(self, command: Sequence[str], *, cwd=None, timeout: int = 600,
                 minimum_tests: Optional[int] = None):
        if not command:
            raise ValueError("the gate needs a command to run")
        self.command = list(command)
        self.cwd = cwd
        self.timeout = timeout
        #: Set for a recognised test runner: exit 0 then passes only with a
        #: runner summary showing at least this many executed cases (Codex
        #: review of 8a71d25: a sole all-skipped node --test suite passed).
        self.minimum_tests = minimum_tests

    def run(self) -> GateResult:
        # Quoted, so a path with spaces survives command_paths (redaction).
        shown = shlex.join(self.command)
        try:
            with _fresh_bytecode_env() as fresh, _report_slot(self.command, fresh) as (argv, env, path, expected):
                returncode, combined, cleanup = _run_owned(argv, cwd=self.cwd, timeout=self.timeout, env=env)
                report = read_report(path, expected) if returncode is not None else None
        except FileNotFoundError as exc:
            return GateResult(
                passed=False, command=shown, returncode=None,
                output=f"(command not found: {exc})",
            )
        if returncode is None:
            # The partial output is kept: a hang must name what it was doing.
            return GateResult(
                passed=False, command=shown, returncode=None,
                output=(f"(timed out after {self.timeout}s) {_cleanup_note(cleanup)}\n"
                        + combined[-_TAIL_CHARS:]).strip(),
            )
        if cleanup:
            combined = (combined + "\n" + _cleanup_note(cleanup)).strip()
        passed = returncode == 0
        if passed and self.minimum_tests is not None:
            count = _test_count(combined)
            if count is None:
                passed, combined = False, "(test count unavailable)\n" + combined
            elif count < self.minimum_tests:
                passed, combined = False, f"(zero tests executed: {count} ran)\n" + combined
        return GateResult(
            passed=passed,
            command=shown,
            returncode=returncode,
            output=combined[-_TAIL_CHARS:],
            report=report,
            cases=case_record(combined, runner_of(self.command)),
        )


#: A file a test runner would run, by the conventions the checks here use.
_TEST_FILE = re.compile(r'(?:^|/)(?:test_[^/]*\.py|[^/]*_test\.py|[^/]*(\.(?:test|spec)\.[cm]?[jt]sx?))$')


def _test_family(path: str):
    """(folder, kind) of a test file, or None: kind is ``py`` or the JS tail
    such as ``.test.js``."""
    match = _TEST_FILE.search(path)
    if not match:
        return None
    folder = path.rsplit('/', 1)[0] if '/' in path else ''
    return folder, (match.group(1) or 'py')


#: Flags a discovering command may carry and still be shown to a review
#: seat for a scratch run: output verbosity only. Anything else, a
#: selection, a plugin, a config or a path-bearing option, is refused.
_DISCOVERY_PYTEST_FLAGS = frozenset({"-q", "-qq", "-v", "-vv", "-rA", "-ra"})
_DISCOVERY_UNITTEST_FLAGS = frozenset({"-v", "-q", "--verbose", "--quiet"})


def _local_dir(operand: str, root) -> bool:
    """A project-relative directory: no absolute path, no traversal, no home
    or variable expansion, and, when ``root`` is given, an existing directory
    inside it."""
    path = PurePosixPath(operand)
    if (not operand or path.is_absolute() or ".." in path.parts or operand.startswith("~")
            or any(c in operand for c in "$`*?[]{}:")):
        return False
    if root is None:
        return True
    target = (Path(root) / operand)
    try:
        return target.is_dir() and target.resolve().is_relative_to(Path(root).resolve())
    except OSError:
        return False


def discovers_added_tests(argv, root=None) -> bool:
    """Whether a command would also run a test file added inside ``root``:
    pytest, or ``python -m unittest discover``, whose only options are
    output verbosity and whose only operands are project-relative
    directories (for unittest, ``-s``/``-t`` values). An explicit file, a
    node id, a filter, any other flag, an absolute or outside path, or an
    unknown form is refused, so nothing private or outside the copy is ever
    named (root reviews of b24b6db and 2d9be52). The interpreter is the
    approved one, left as it is."""
    argv = [str(a) for a in argv]
    if not argv or is_filtered(argv):
        return False
    head = Path(argv[0]).name
    if head in _PYTEST_NAMES:
        rest = argv[1:]
    elif head.startswith("python") and argv[1:3] == ["-m", "pytest"]:
        rest = argv[3:]
    elif head.startswith("python") and argv[1:4] == ["-m", "unittest", "discover"]:
        rest, i = argv[4:], 0
        while i < len(rest):
            arg = rest[i]
            if arg in _DISCOVERY_UNITTEST_FLAGS:
                i += 1
            elif arg in ("-s", "-t") and i + 1 < len(rest) and _local_dir(rest[i + 1], root):
                i += 2
            else:
                return False
        return True
    else:
        return False
    for arg in rest:
        if arg.startswith("-"):
            if arg not in _DISCOVERY_PYTEST_FLAGS:
                return False
        elif _test_family(arg) or not _local_dir(arg, root):
            return False
    return True


def uncovered_tests(changed, argvs) -> List[str]:
    """Changed test files no required command runs, where that can be told.

    A command that lists test files explicitly names only those (batch 2
    gui-ui-v3 on 5d9f5ff: t2 added tests/ui/project_search.test.js and the
    frozen Node check named five other files). Not named is not proof of not
    run: a listed file can run another through a wrapper, so callers report
    the result as unnamed, never as unrun (Codex review of 7515f28).
    A changed test file is uncovered when some command lists files of the
    same folder and kind, and no command lists it, names a folder holding
    it, or discovers it (a pytest command with no test-file operands covers
    every Python test file). Where no command lists that family, nothing is
    claimed: unknown is not uncovered.
    """
    listed, folders, discovers_py = set(), [], False
    families = set()
    for argv in argvs:
        argv = [str(a) for a in argv]
        operands = [a for a in argv[1:] if not a.startswith('-')]
        tests = [a for a in operands if _test_family(a)]
        listed.update(tests)
        families.update(_test_family(a) for a in tests)
        folders += [a.rstrip('/') + '/' for a in operands
                    if not _test_family(a) and not a.endswith(('.py', '.js', '.cjs', '.mjs', '.ts'))]
        if 'pytest' in ' '.join(argv[:3]) and not any(_test_family(a) and a.endswith('.py') for a in tests):
            discovers_py = True
    found = []
    for path in sorted(set(changed or ())):
        family = _test_family(path)
        if family is None or family not in families or path in listed:
            continue
        if any(path.startswith(folder) for folder in folders):
            continue
        if family[1] == 'py' and discovers_py:
            continue
        found.append(path)
    return found


@dataclass(frozen=True)
class GateCommand:
    """One configured command. Caching is opt-in for source-only checks."""

    id: str
    argv: tuple[str, ...] = ()
    cwd: str = '.'
    timeout: float = 600
    required: bool = True
    cheap: bool = False
    minimum_tests: Optional[int] = None
    skip_reason: str = ''
    cacheable: bool = False

    def __post_init__(self):
        import math

        if not isinstance(self.id, str) or not self.id.strip():
            raise ValueError('Gate id must be nonempty')
        if isinstance(self.argv, str) or any(not isinstance(a, str) or not a for a in self.argv):
            raise ValueError('Gate argv must be a sequence of nonempty arguments')
        object.__setattr__(self, 'argv', tuple(self.argv))
        if (isinstance(self.timeout, bool) or not isinstance(self.timeout, (int, float))
                or not math.isfinite(self.timeout) or self.timeout <= 0):
            raise ValueError('Gate timeout must be positive and finite')
        for name in ('required', 'cheap', 'cacheable'):
            if type(getattr(self, name)) is not bool:
                raise ValueError(f'{name} must be boolean')
        if self.minimum_tests is not None and (
                type(self.minimum_tests) is not int or self.minimum_tests < 1):
            raise ValueError('minimum_tests must be a positive integer when configured')
        if not isinstance(self.skip_reason, str):
            raise ValueError('skip_reason must be a string')


@dataclass(frozen=True)
class GateReceipt:
    id: str
    status: Literal['passed', 'failed', 'blocked', 'error', 'skipped']
    reason: str
    required: bool
    command: str = ''
    returncode: Optional[int] = None
    output: str = ''
    cached: bool = False
    tests: Optional[int] = None
    source_hash: str = ''
    config_hash: str = ''
    runner_hash: str = ''
    #: The structured report (read_report), when the command declares one.
    report: Optional[dict] = None
    #: Where the command ran, relative to the project: a file it names is
    #: read from there (Codex review of 81adcc7, R5: ``node --test
    #: browser.test.js`` in tests/ui never matched tests/ui/browser.test.js).
    cwd: str = '.'
    #: Test selection the runner takes from configuration or the environment
    #: rather than from argv, '' when none was found (R2: ``addopts = -k
    #: unit`` in pytest.ini ran one case of a named file and looked whole).
    selection: str = ''
    #: Case names read from the whole output before the tail is kept
    #: (case_record), None past its bound (Codex review of 67c9fad, S1: the
    #: 2,000-character tail lost the names that told two skips apart).
    cases: Optional[dict] = None


def _test_count(output):
    """Recognize runner summaries only; absent evidence is not zero or success."""
    if re.search(r'\bno tests (?:ran|collected|found)\b', output, re.I):
        return 0
    # Node's test runner, in TAP ("# pass 1") or its spec reporter
    # ("ℹ pass 1"). Executed cases are pass + fail when both are reported;
    # skipped, todo and cancelled did not run. An all-skipped run is zero,
    # which fails (Codex review of 3ef9962: "ℹ tests 1 / ℹ pass 0 /
    # ℹ skipped 1" passed with no count).
    def node(name):
        found = re.findall(rf'(?m)^(?:#|\u2139)\s*{name} (\d+)\s*$', output)
        return int(found[-1]) if found else None
    passed, failed = node('pass'), node('fail')
    if passed is not None and failed is not None:
        return passed + failed
    tap = re.findall(r'(?m)^(?:#|\u2139)\s*tests (\d+)\s*$', output)
    if tap:
        skipped = re.findall(r'(?m)^(?:#|\u2139)\s*(?:skipped|skip|todo|cancelled) (\d+)\s*$', output)
        return max(0, int(tap[-1]) - sum(map(int, skipped)))
    unittest = re.findall(r'Ran (\d+) tests?\b', output)
    if unittest:
        skipped = re.findall(r'skipped=(\d+)', output)
        return max(0, int(unittest[-1]) - (int(skipped[-1]) if skipped else 0))
    # Pytest (and compact JS summaries) can report failed executions alongside
    # passes. Use one final summary line, excluding cases whose body did not run.
    for line in reversed(output.splitlines()):
        outcomes = re.findall(
            r'\b(\d+) (passed|failed|xfailed|xpassed|skipped|deselected|errors?)\b', line)
        if outcomes:
            return sum(int(count) for count, outcome in outcomes
                       if outcome in {'passed', 'failed', 'xfailed', 'xpassed'})
    return None


class GateSuite:
    """Ordered commands behind the existing run() seam, with same-tree receipts.

    Cache only explicitly marked, deterministic source-only gates. Database,
    network and other external-state gates must leave cacheable=False. Cache
    entries live for this runner instance and only successful receipts are kept.
    """

    def __init__(self, commands: Sequence[GateCommand], *, cwd, exclude=(), report_env=None, isolation=()):
        from .project import Project

        #: Reporting-only environment for the runner (scratch_receipt's case
        #: lines); never selection, never a different command.
        self.report_env = dict(report_env or {})
        #: An OS sandbox launcher put in front of each command, for a scratch
        #: run of a reviewer's test only (runtime._scratch_isolation).
        self.isolation = tuple(isolation)
        self.cwd = Path(cwd).resolve()
        self.project = Project(self.cwd, exclude=exclude)
        self.commands = tuple(commands)
        if len({c.id for c in self.commands}) != len(self.commands):
            raise ValueError('Duplicate gate id')
        for command in self.commands:
            path = Path(command.cwd)
            if path.is_absolute() or '..' in path.parts:
                raise ValueError('Gate cwd must be inside the selected project')
            if not (self.cwd / path).resolve().is_relative_to(self.cwd):
                raise ValueError('Gate cwd escapes the selected project')
        self._cache = {}

    def cheap(self):
        view = GateSuite([c for c in self.commands if c.cheap], cwd=self.cwd,
                         exclude=self.project.exclude, report_env=self.report_env,
                         isolation=self.isolation)
        view._cache = self._cache
        return view

    def _runner_hash(self, command, cwd):
        executable = command.argv[0]
        path = (cwd / executable) if '/' in executable else shutil.which(executable)
        if path is None:
            return ''
        files = [Path(__file__), Path(path)]
        # The shipped producer a declared report selects is part of the
        # runner: changed bytes must miss the cache (root review of f0b4827).
        files += [_declared_producer(command.argv)] if any(REPORT_TOKEN in a for a in command.argv) else []
        # Include explicit runner scripts outside the project as well.
        files += [cwd / a for a in command.argv[1:] if (cwd / a).is_file()]
        digest = hashlib.sha256()
        for file in files:
            digest.update(str(file.resolve()).encode() + b'\0' + file.read_bytes())
        return digest.hexdigest()

    def run(self) -> GateResult:
        receipts = []
        source = self.project.fingerprint()
        changed = False
        for command in self.commands:
            config = hashlib.sha256(json.dumps(asdict(command), sort_keys=True).encode()).hexdigest()
            base = dict(id=command.id, required=command.required, command=shlex.join(command.argv),
                        source_hash=source, config_hash=config, cwd=command.cwd,
                        selection=config_selection(command.argv, self.cwd / command.cwd, self.cwd))
            if changed or command.skip_reason:
                receipts.append(GateReceipt(**base, status='skipped',
                                            reason='source changed during a prior gate' if changed
                                            else command.skip_reason))
                continue
            if not command.argv:
                receipts.append(GateReceipt(**base, status='blocked', reason='command not configured'))
                continue
            cwd = (self.cwd / command.cwd).resolve()
            if not cwd.is_relative_to(self.cwd) or not cwd.is_dir():
                receipts.append(GateReceipt(**base, status='blocked', reason='cwd unavailable or outside project'))
                continue
            try:
                runner = self._runner_hash(command, cwd)
                base['runner_hash'] = runner
                key = (source, config, runner)
                if command.cacheable and runner and key in self._cache:
                    receipts.append(replace(self._cache[key], cached=True))
                    continue
                with _fresh_bytecode_env() as fresh, _report_slot(command.argv, dict(fresh, **self.report_env)) \
                        as (argv, env, path, expected):
                    returncode, output, cleanup = _run_owned([*self.isolation, *argv], cwd=cwd,
                                                             timeout=command.timeout, env=env)
                    report = read_report(path, expected) if returncode is not None else None
                if returncode is None:
                    raise _GateTimeout(output, cleanup)
                if cleanup:
                    output = (output + '\n' + _cleanup_note(cleanup)).strip()
                count = _test_count(output)
                status, reason = ('passed', 'exit 0') if returncode == 0 else ('failed', 'nonzero exit')
                if count == 0:
                    status, reason = 'failed', 'zero tests executed'
                elif returncode == 0 and command.minimum_tests is not None:
                    if count is None:
                        status, reason = 'blocked', 'test count unavailable'
                    elif count < command.minimum_tests:
                        status, reason = 'failed', 'fewer tests than required'
                receipt = GateReceipt(**base, status=status, reason=reason,
                                      returncode=returncode, output=output[-_TAIL_CHARS:], tests=count,
                                      report=report, cases=case_record(output, runner_of(command.argv)))
            except _GateTimeout as exc:
                # Still an error, never a product failure: what was printed
                # before the deadline is kept so the hang can be diagnosed.
                receipt = GateReceipt(**base, status='error', reason=f'timed out after {command.timeout}s',
                                      output=((_cleanup_note(exc.cleanup) + '\n' + exc.output).strip())[-_TAIL_CHARS:])
            except OSError as exc:
                receipt = GateReceipt(**base, status='blocked', reason=f'runner unavailable: {exc}')
            if self.project.fingerprint() != source:
                changed = True
                receipt = replace(receipt, status='error', reason='source changed during gate')
                self._cache.clear()
            if command.cacheable and receipt.status == 'passed' and receipt.runner_hash:
                self._cache[key] = receipt
            receipts.append(receipt)
        passed = not changed and all(r.status == 'passed' for r in receipts if r.required)
        output = '\n'.join(f'{r.id}: {r.status}: {r.reason}\n{r.output}' for r in receipts)
        return GateResult(passed, 'gate suite', 0 if passed else 1, output, tuple(receipts))


#: Folders whose files are test support (helpers, fixtures, harness stubs)
#: as well as tests. Series rule-119c83f f2: the original helper
#: tests/ui/load_app.js matched no test-file pattern and was edited to fit the
#: code, so the gate passed on a harness the original tests never used.
_TEST_DIRS = frozenset({'tests', 'test', '__tests__', 'spec', 'specs'})
_TEST_SUPPORT_NAMES = frozenset({'conftest.py', 'pytest.ini', '.pytest.ini'})
#: Test-runner configuration that only selects or shapes tests: kept and
#: restored with the run-start tests (Codex review of b6ba3ba, F1: a changed
#: pytest.ini deselected the restored original assertion in both checks).
_TEST_CONFIG = re.compile(r'(?:^|/)(?:(?:jest|vitest|playwright|ava)\.config\.[cm]?[jt]s|\.mocharc\.[\w.]+)$')
#: Files that hold test selection beside other project settings, so they are
#: compared section by section and never restored whole (restoring an old
#: package.json "type" or pyproject dependency table would break the current
#: code for reasons unrelated to its tests).
_MIXED_CONFIG = ('pyproject.toml', 'setup.cfg', 'tox.ini', 'package.json')
_PACKAGE_TEST_KEYS = ('type', 'imports', 'jest', 'mocha', 'ava', 'vitest', 'c8', 'nyc')
#: Arguments that run only part of what a command names: such a run never
#: shows that a whole named file executed (F3: --test-name-pattern=unit ran
#: the unit case of the browser file and lifted its NOT RUN).
_FILTER_FLAGS = ('--test-name-pattern', '--test-skip-pattern', '--test-only', '--test-shard', '-k', '-m',
                 '--deselect', '--lf', '--last-failed', '--sw', '--stepwise', '--grep', '-g', '--fgrep',
                 '-t', '--testNamePattern', '--testPathPattern', '--only', '--ignore', '--ignore-glob')
#: Runtime-dependency trees a copy may need; linked, never copied, and
#: guarded by quadratus.deptree like the project's own checks.
_DEPENDENCY_DIRS = ('node_modules', '.venv', 'venv', 'env')


def is_test_support(path: str) -> bool:
    """A test file, a file under a test folder (helpers and fixtures), or a
    test-only runner configuration such as pytest.ini."""
    parts = path.split('/')
    return (_test_family(path) is not None or parts[-1] in _TEST_SUPPORT_NAMES
            or _TEST_CONFIG.search(path) is not None
            or any(part in _TEST_DIRS for part in parts[:-1]))


def selection_sections(contents: dict) -> dict:
    """The test-selection part of each mixed configuration file: pyproject's
    [tool.pytest] tables, setup.cfg's [tool:pytest], tox.ini's [pytest] and
    [tool:pytest], and package.json's module and test-runner keys. A file that
    cannot be read this way is kept whole, so any change to it counts."""
    import configparser
    out = {}
    for name, data in contents.items():
        base = name.rsplit('/', 1)[-1]
        if base not in _MIXED_CONFIG:
            continue
        try:
            text = data.decode('utf-8')
            if base == 'pyproject.toml':
                import tomllib
                tool = tomllib.loads(text).get('tool', {})
                section = tool.get('pytest', {}) if isinstance(tool, dict) else {}
            elif base == 'package.json':
                loaded = json.loads(text)
                section = {k: loaded.get(k) for k in _PACKAGE_TEST_KEYS} if isinstance(loaded, dict) else loaded
                scripts = loaded.get('scripts') if isinstance(loaded, dict) else None
                section['scripts.test'] = scripts.get('test') if isinstance(scripts, dict) else None
            else:
                parser = configparser.RawConfigParser()
                parser.read_string(text)
                section = {s: dict(parser.items(s)) for s in ('pytest', 'tool:pytest') if parser.has_section(s)}
            out[name] = json.dumps(section, sort_keys=True, default=str)
        except Exception:  # noqa: BLE001 -- unreadable: the whole file is the section
            out[name] = hashlib.sha256(data).hexdigest()
    return out


def changed_selection(original: dict, current: dict) -> List[str]:
    """Mixed configuration files whose test-selection section changed, or
    that were added or removed, since run start."""
    now = selection_sections(current)
    return sorted(name for name in original.keys() | now.keys() if original.get(name) != now.get(name))


def _runner_args(argv) -> list:
    """The runner's own arguments: after ``python -m <module>`` when the
    interpreter is what the command starts (its -m is not pytest's -m)."""
    argv = [str(a) for a in argv]
    if len(argv) > 2 and argv[1] == '-m':
        return argv[3:]
    return argv[1:]


def is_filtered(argv) -> bool:
    """Whether a command runs only part of what it names."""
    return _has_filter(_runner_args(argv))


def _has_filter(args) -> bool:
    for arg in args:
        arg = str(arg)
        if '::' in arg:
            return True
        if any(arg == f or arg.startswith(f + '=') or (len(f) == 2 and arg.startswith(f) and len(arg) > 2
                                                        and not arg.startswith('--'))
               for f in _FILTER_FLAGS):
            return True
    return False


def operand_paths(argv, root=None, cwd='.') -> List[str]:
    """The file operands a command names, project-relative where they can
    be: an absolute path inside ``root`` loses the root, and a relative one is
    read from the command's ``cwd`` (R5)."""
    import posixpath
    out = []
    modules = unittest_modules(argv, root, cwd)
    if modules is not None:
        # A whole module is its file: ``tests.test_app`` names
        # tests/test_app.py (root review of d48014d, 2026-10-30).
        return [_module_file(m, root, cwd) if root is not None else m.replace('.', '/') + '.py' for m in modules]
    unittest = _is_unittest([str(a) for a in argv])
    for arg in _runner_args(argv):
        arg = str(arg)
        if arg.startswith('-') or '::' in arg:
            continue
        if unittest and _MODULE_NAME.match(arg):
            # A dotted unittest token that did not read as a whole module
            # above is a selection inside one (``pkg.module.Class.test``) or a
            # module this project does not hold: it names no file whole
            # (root's counterexample on 6527479725, 2026-10-30).
            continue
        if root is not None and arg.startswith(str(root).rstrip('/') + '/'):
            arg = arg[len(str(root).rstrip('/')) + 1:]
        elif not posixpath.isabs(arg):
            arg = posixpath.normpath(posixpath.join(cwd or '.', arg))
        out.append(arg[2:] if arg.startswith('./') else arg)
    return out


#: Files pytest reads ``addopts`` from, looked for from a command's cwd up to
#: the project root.
_PYTEST_CONFIGS = ('pytest.ini', '.pytest.ini', 'pyproject.toml', 'tox.ini', 'setup.cfg')


_PYTEST_NAMES = ('pytest', 'py.test')
#: Launchers that run their next argument as the command: ``uv run pytest``.
_RUN_WRAPPERS = ('uv', 'poetry', 'pipenv', 'pdm', 'hatch', 'rye')


def _is_pytest(argv) -> bool:
    """Whether a command starts pytest, read from the executable's position
    only: ``pytest`` itself, ``<python> [flags] -m pytest``, or a ``run``
    launcher followed by one of those. A test-file operand named ``pytest``
    never decides it (Codex review of 02accbd, X2)."""
    argv = [str(a) for a in argv]
    if not argv:
        return False
    name = Path(argv[0]).name
    if name in _PYTEST_NAMES:
        return True
    if name.startswith('python'):
        i = 1
        while i < len(argv) and argv[i].startswith('-') and argv[i] != '-m':
            i += 1
        return i + 1 < len(argv) and argv[i] == '-m' and argv[i + 1] in _PYTEST_NAMES
    if name in _RUN_WRAPPERS and len(argv) > 2 and argv[1] == 'run':
        return _is_pytest(argv[2:])
    return False


def _config_addopts(path: Path) -> Optional[str]:
    """The pytest ``addopts`` one configuration file sets: '' for none,
    None when the file exists and cannot be read (unknown, never none)."""
    import configparser
    if not path.is_file():
        return ''
    try:
        text = path.read_text(encoding='utf-8')
        if path.name == 'pyproject.toml':
            import tomllib
            tool = tomllib.loads(text).get('tool', {}) or {}
            pytest_table = tool.get('pytest', {}) if isinstance(tool, dict) else {}
            values = []
            for table in (pytest_table.get('ini_options', {}), pytest_table):
                value = table.get('addopts') if isinstance(table, dict) else None
                if isinstance(value, list):
                    value = shlex.join(str(v) for v in value)
                if value:
                    values.append(str(value))
            return ' '.join(values)
        parser = configparser.RawConfigParser()
        parser.read_string(text)
        sections = {'setup.cfg': ('tool:pytest',), 'tox.ini': ('pytest', 'tool:pytest')}.get(path.name, ('pytest',))
        return ' '.join(parser.get(s, 'addopts') for s in sections
                        if parser.has_section(s) and parser.has_option(s, 'addopts'))
    except Exception:  # noqa: BLE001 -- unreadable selection is unknown selection
        return None


def config_selection(argv, cwd, root) -> str:
    """Test selection a command takes from outside its argv: ``addopts`` in
    pytest's configuration files from ``cwd`` up to ``root`` and
    ``PYTEST_ADDOPTS`` for a pytest command, ``NODE_OPTIONS`` for any.
    '' when none of them filters; an unreadable configuration counts, and so
    does a runner ``runner_of`` cannot name.

    Every candidate file is read, not only the one pytest would pick, so this
    can name a file pytest ignores: it only ever withholds a "whole file ran"
    reading, never grants one (Codex review of 81adcc7, R2)."""
    found = []
    # A runner this cannot name has a collection this cannot inspect: a shell
    # wrapper around python still loads conftest hooks, and none of the pytest
    # checks below apply to it. Its selection is unknown, never empty (Codex
    # review of 0d834bd, Y1).
    if runner_of(argv, root, cwd) is None:
        found.append('test runner not recognised from argv; selection not established')
    names = ('NODE_OPTIONS', 'PYTEST_ADDOPTS') if _is_pytest(argv) else ('NODE_OPTIONS',)
    # Plugins named in PYTEST_PLUGINS load into every pytest the gate starts
    # and can drop collected tests silently (Codex review of d157378, T3).
    if _is_pytest(argv) and os.environ.get('PYTEST_PLUGINS', '').strip():
        found.append(f"PYTEST_PLUGINS={os.environ['PYTEST_PLUGINS'].strip()}")
    for name in names:
        value = os.environ.get(name, '')
        if value and (_filters(value) or _NODE_CONFIG.search(value) or (
                name == 'PYTEST_ADDOPTS' and _plugin_option(value))):
            found.append(f'{name}={value}')
    # A node configuration file can carry testRunner selection the argv never
    # shows (Codex review of 67c9fad, S2: --experimental-config-file with
    # test-name-pattern = unit). Its contents are not read: selection there is
    # unknown, so the run is not whole.
    configured = [str(a) for a in argv if _NODE_CONFIG.search(str(a))]
    if configured:
        found.append(f'node configuration file: {configured[0]}')
    if _is_pytest(argv):
        try:
            root, here = Path(root).resolve(), Path(cwd).resolve()
        except OSError:
            return 'command cwd unreadable'
        plugins = _plugin_option(shlex.join(str(a) for a in _runner_args(argv)))
        if plugins:
            found.append(f'argv loads plugin {plugins}')
        start = here
        while here.is_relative_to(root):
            for name in _PYTEST_CONFIGS:
                addopts = _config_addopts(here / name)
                where = (here / name).relative_to(root).as_posix()
                if addopts is None:
                    found.append(f'{where}: unreadable')
                elif addopts and (_filters(addopts) or _plugin_option(addopts)):
                    found.append(f'{where}: addopts = {addopts}')
            if here == root:
                break
            here = here.parent
        hooks = _collection_hooks(argv, start, root)
        if hooks:
            found.append(hooks)
    return '; '.join(found)[:300]


#: Node options that read a configuration file (node 23.10 and later).
_NODE_CONFIG = re.compile(r'^--experimental(?:-default)?-config-file\b|\s--experimental(?:-default)?-config-file\b')
#: conftest.py names that can drop, replace or bypass collected tests without
#: the runner reporting them deselected or skipped (S2: a
#: pytest_collection_modifyitems hook removed test_browser silently), or load
#: a plugin that can.
_COLLECTION_HOOKS = re.compile(r'\b(?:pytest_collection_modifyitems|pytest_ignore_collect|pytest_collect_file|'
                               r'pytest_collect_directory|pytest_pycollect_makeitem|pytest_pycollect_makemodule|'
                               r'pytest_collection|pytest_runtest_protocol|pytest_pyfunc_call|pytest_runtest_call|'
                               r'pytest_deselected|collect_ignore|collect_ignore_glob|pytest_plugins)\b')


def _plugin_option(options: str) -> str:
    """A ``-p NAME`` plugin load other than ``-p no:NAME`` (a plugin can
    select tests); '' when there is none."""
    try:
        tokens = shlex.split(options)
    except ValueError:
        return 'unparsable options'
    for i, token in enumerate(tokens):
        value = tokens[i + 1] if token == '-p' and i + 1 < len(tokens) else (
            token[2:] if token.startswith('-p') and len(token) > 2 else '')
        if value and not value.startswith('no:'):
            return value
    return ''


def _collection_hooks(argv, cwd: Path, root: Path) -> str:
    """The first conftest.py pytest would load for this command (from the
    project root down to its cwd and to each file it names) that defines a
    collection or execution hook; '' when none does. Read only, never run."""
    rel = cwd.relative_to(root).as_posix() if cwd.is_relative_to(root) else '.'
    dirs = {Path(rel)}
    for operand in operand_paths(argv, root, rel):
        target = root / operand
        dirs.add(Path(operand) if target.is_dir() else Path(operand).parent)
    seen = set()
    for directory in sorted(dirs):
        parts = [p for p in directory.parts if p not in ('', '.')]
        for depth in range(len(parts) + 1):
            conftest = root.joinpath(*parts[:depth], 'conftest.py')
            if conftest in seen:
                continue
            seen.add(conftest)
            if not conftest.is_file():
                continue
            try:
                text = conftest.read_text(encoding='utf-8', errors='replace')
            except OSError:
                return f'{conftest.relative_to(root).as_posix()}: unreadable'
            hook = _COLLECTION_HOOKS.search(text)
            if hook:
                return f'{conftest.relative_to(root).as_posix()}: {hook.group(0)}'
    return ''


def _filters(options: str) -> bool:
    try:
        tokens = shlex.split(options)
    except ValueError:
        return True
    return _has_filter(tokens)


def deselected_count(output: str) -> int:
    """Cases the runner's final summary line reports deselected (pytest
    prints ``N deselected`` only when there are some)."""
    for line in reversed((output or '').splitlines()):
        found = re.findall(r'\b(\d+) (passed|failed|xfailed|xpassed|skipped|deselected|errors?)\b', line)
        if found:
            return sum(int(n) for n, kind in found if kind == 'deselected')
    return 0


_SPEC_CASE = re.compile(r'^\s*(\u2714|\u2716|\ufe63)\s+(.*?)\s+\([\d.]+m?s\)(?:\s+#\s*(.*?))?\s*$')
_TAP_CASE = re.compile(r'^\s*(not ok|ok)\s+\d+\s+-\s+(.*?)(?:\s+#\s*(SKIP|TODO)\b.*?)?\s*$', re.I)
_PYTEST_CASE = re.compile(r'^(\S+::\S+)\s+(PASSED|FAILED|SKIPPED|XFAIL|XPASS|ERROR)\b')


def case_key(name: str) -> str:
    """A case's identity: its full name, or for a name over 300 characters a
    readable prefix plus the SHA256 of the whole name, so two distinct long
    names never share a key (Codex review of d157378, T1)."""
    if len(name) <= 300:
        return name
    return f"{name[:200]}\u2026sha256:{hashlib.sha256(name.encode('utf-8', 'surrogatepass')).hexdigest()}"


_UNITTEST_PLAIN_FLAGS = frozenset({'-v', '--verbose', '-q', '--quiet', '-b', '--buffer', '-f', '--failfast',
                                   '-c', '--catch'})
_MODULE_NAME = re.compile(r'^[A-Za-z_]\w*(?:\.[A-Za-z_]\w*)*$')


def _module_file(module: str, root, cwd='.') -> Optional[str]:
    """The project-relative ``.py`` file a dotted unittest token names when
    that file exists under ``root`` (read from the command's ``cwd``), else
    None: a package, a missing module or a ``module.Class.test`` selection
    resolves to no file. Read only; nothing is imported."""
    import posixpath
    if root is None:
        return None
    cwd = str(cwd or '.')
    try:
        base = Path(root).resolve()
        if posixpath.isabs(cwd):
            here = Path(cwd).resolve()
            rel_cwd = here.relative_to(base).as_posix() if here.is_relative_to(base) else None
        else:
            rel_cwd = posixpath.normpath(cwd)
        if rel_cwd is None:
            return None
        rel = posixpath.normpath(posixpath.join(rel_cwd, module.replace('.', '/') + '.py'))
        if rel.startswith('..'):
            return None
        return rel if (base / rel).is_file() else None
    except (OSError, ValueError):
        return None


def unittest_modules(argv, root=None, cwd='.') -> Optional[List[str]]:
    """The modules a plain ``python -m unittest <module>...`` command runs
    whole (only the runner's plain flags and the harness report token
    beside them), or None for any other unittest form (``discover``, a
    file path, a selection inside a module, a filter): the one unittest
    form whose selection the harness can establish, so a declared report
    of it can discharge what names those modules (root review of d48014d,
    2026-10-30). With ``root``, a dotted token is a module only when it
    resolves to an existing ``.py`` file under the project from the
    command's ``cwd``: ``tests.test_sample.Target.test_own`` is a selection
    inside a module and read as one, so a run of it is never a whole-module
    run (root's counterexample on 6527479725, 2026-10-30). Without a root
    the token's shape alone is read, as before."""
    argv = [str(a) for a in argv]
    if not _is_unittest(argv):
        return None
    modules = []
    for token in argv[3:]:
        if token in _UNITTEST_PLAIN_FLAGS or token.startswith(REPORT_TOKEN_PREFIX):
            continue
        if token.startswith('-') or token == 'discover' or not _MODULE_NAME.match(token):
            return None
        if root is not None and _module_file(token, root, cwd) is None:
            return None
        modules.append(token)
    return modules or None


def runner_of(argv, root=None, cwd='.') -> Optional[str]:
    """Which test runner a command starts: 'pytest', 'node' (``node
    --test``), 'unittest' (the plain module form ``python -m unittest
    <module>...``, whose selection is its modules whole; with ``root`` the
    modules must resolve to files) or None when none can be told from its
    argv."""
    argv = [str(a) for a in argv]
    if _is_pytest(argv):
        return 'pytest'
    if argv and Path(argv[0]).name.startswith('node') and '--test' in argv:
        return 'node'
    if unittest_modules(argv, root, cwd) is not None:
        return 'unittest'
    return None


def _case_lines(output: str, runner: Optional[str] = None):
    """``(name, kind, qualified)`` for each case line the runner printed,
    ``kind`` skipped, executed or other. Which lines count follows the
    command's own runner: a pytest command reads only pytest verbose lines,
    a node command only spec and TAP lines, so text a test prints that merely
    looks like another runner's record is not a case (Codex review of
    f2f66ff, W1). ``qualified`` is True only for a pytest line from a pytest
    command, whose ``path::name`` names the case's file; a node name may
    contain ``::`` too and is never qualified (5292fc2, V1). Node's spec reporter (``\u2714``/``\u2716``/
    ``\ufe63``), TAP (``ok N - name # SKIP``) and pytest's verbose lines. A
    todo case and a pytest XFAIL are other: XFAIL does not say whether the
    body ran, and ``xfail(run=False)`` never runs it (Codex review of
    67c9fad, S5)."""
    node_lines, pytest_lines = runner in (None, 'node'), runner in (None, 'pytest')
    for line in (output or '').splitlines():
        spec = _SPEC_CASE.match(line) if node_lines else None
        if spec:
            mark, name, directive = spec.group(1), spec.group(2), (spec.group(3) or '')
            yield name, ('other' if directive.upper().startswith('TODO')
                         else 'skipped' if mark == '\ufe63' else 'executed'), False
            continue
        tap = _TAP_CASE.match(line) if node_lines else None
        if tap:
            directive = (tap.group(3) or '').upper()
            yield tap.group(2), {'SKIP': 'skipped', '': 'executed'}.get(directive, 'other'), False
            continue
        case = _PYTEST_CASE.match(line) if pytest_lines else None
        if case:
            yield case.group(1), {'SKIPPED': 'skipped', 'XFAIL': 'other'}.get(case.group(2), 'executed'), \
                runner == 'pytest'


def case_outcomes(output: str, runner: Optional[str] = None):
    """``(skipped, executed)`` case names the runner printed, as Counters.
    Names, not file-qualified: node's spec output carries no file per case."""
    from collections import Counter
    skipped, executed = Counter(), Counter()
    for name, kind, _ in _case_lines(output, runner):
        if kind == 'skipped':
            skipped[case_key(name)] += 1
        elif kind == 'executed':
            executed[case_key(name)] += 1
    return skipped, executed


#: Bound on case lines kept per command; past it the record is None.
_MAX_CASES = 20_000


def case_record(output: str, runner: Optional[str] = None) -> Optional[dict]:
    """Every case the whole output prints, by kind (skipped, executed,
    other), as ``case_key``-to-count maps; None past ``_MAX_CASES`` lines.
    None is unknown identity, which callers never rebuild from the output
    tail (Codex review of d157378, T2)."""
    record = dict(skipped={}, executed={}, other={})
    qualified = set()
    for i, (name, kind, from_pytest) in enumerate(_case_lines(output, runner)):
        if i >= _MAX_CASES:
            return None
        key = case_key(name)
        record[kind][key] = record[kind].get(key, 0) + 1
        if from_pytest:
            qualified.add(key)
    # Keys a pytest verbose line printed: only these carry their file (V1).
    record['qualified'] = sorted(qualified)
    # Executed cases the runner printed as failed (scratch_receipt): a
    # subset of ``executed``, so a reader of the other keys is unaffected.
    record['failed'] = _failed_cases(output, runner)
    return record


def _failed_cases(output: str, runner: Optional[str] = None) -> dict:
    """``case_key``-to-count of the executed case lines that report a
    failure: pytest FAILED or ERROR, TAP ``not ok``, spec \u2716."""
    failed = {}
    node_lines, pytest_lines = runner in (None, 'node'), runner in (None, 'pytest')
    for line in (output or '').splitlines():
        spec = _SPEC_CASE.match(line) if node_lines else None
        tap = None if spec or not node_lines else _TAP_CASE.match(line)
        case = None if spec or tap or not pytest_lines else _PYTEST_CASE.match(line)
        if spec and spec.group(1) == '\u2716' and not (spec.group(3) or ''):
            name = spec.group(2)
        elif tap and tap.group(1).lower() == 'not ok' and not tap.group(3):
            name = tap.group(2)
        elif case and case.group(2) in ('FAILED', 'ERROR'):
            name = case.group(1)
        else:
            continue
        key = case_key(name)
        failed[key] = failed.get(key, 0) + 1
    return failed


def skipped_cases(receipt):
    """The names of the cases a check skipped, as a Counter, when the output
    names exactly as many as it counts; None otherwise (unknown)."""
    count = skipped_count(receipt)
    if count is None:
        return None
    skipped, _ = case_outcomes(getattr(receipt, 'output', '') or '')
    return skipped if sum(skipped.values()) == count else None


def added_selectors(originals: dict, current: dict) -> List[str]:
    """Test-only configuration added since run start (a pytest.ini, a
    conftest.py, a jest config): its appearance can deselect tests the
    run-start suite ran, so it is a change to the original suite the same
    way an edit is (Codex review of 81adcc7, R1). The original-test copy
    leaves it out, as it leaves out every added support file."""
    return sorted(name for name in current if name not in originals
                  and (name.rsplit('/', 1)[-1] in _TEST_SUPPORT_NAMES or _TEST_CONFIG.search(name)))


def original_tests(contents: dict) -> dict:
    """The run-start bytes of every test and test-support file."""
    return {name: data for name, data in contents.items() if is_test_support(name)}


def changed_originals(originals: dict, current: dict) -> List[str]:
    """Run-start test or support files whose bytes changed or that are gone."""
    return sorted(name for name, data in originals.items() if current.get(name) != data)


def _gate_commands(gate, root: Path):
    """The gate's commands with cwd relative to ``root``, or None when they
    cannot be read (a gate other than these two, or one outside the project)."""
    if isinstance(gate, GateSuite):
        base = gate.cwd
        commands = list(gate.commands)
    elif isinstance(gate, IntegrationGate):
        base = Path(gate.cwd or root).resolve()
        commands = [GateCommand(id='check', argv=tuple(str(a) for a in gate.command),
                                timeout=gate.timeout, minimum_tests=gate.minimum_tests)]
    else:
        return None
    if not base.is_relative_to(root):
        return None
    prefix = base.relative_to(root)
    return [replace(c, cwd=(prefix / c.cwd).as_posix()) for c in commands]


#: A node:test / mocha-style case declared with a literal name.
_JS_CASE = re.compile(r"""(?:^|[^\w.$])(?:test|it)\s*\(\s*(["'])((?:\\.|(?!\1).)*?)\1""", re.MULTILINE)
_JS_DYNAMIC = re.compile(r"(?:^|[^\w.$])(?:test|it)\s*\(\s*(?:`|[A-Za-z_$])", re.MULTILINE)
_PY_CASE = re.compile(r"^\s*(?:async\s+)?def\s+(test_\w+)\s*\(", re.MULTILINE)


def declared_cases(name: str, data: bytes) -> Optional[List[str]]:
    """The case names a test file declares literally, or None when they
    cannot all be read (a computed or template-literal name, or none)."""
    try:
        text = data.decode('utf-8')
    except UnicodeDecodeError:
        return None
    family = _test_family(name)
    if family is None:
        return None
    if family[1] == 'py':
        names = _PY_CASE.findall(text)
    else:
        if _JS_DYNAMIC.search(text):
            return None
        names = [m.group(2) for m in _JS_CASE.finditer(text)]
    return names or None


def repair_problems(name: str, original: bytes, current: dict, receipts) -> List[str]:
    """Why an operator-authorized test repair is not shown to keep the test
    it replaced, or [] when it is.

    The repaired file must still declare every case the run-start version
    declared, each by a name no other test file in the project uses (TAP
    case names carry no file), and each must have executed, not skipped, in
    a passed required receipt of this check. A case that cannot be named,
    attributed or seen executing is not shown to run: the check fails rather
    than trusting the rewritten file (Codex/Davis, run 20261010T225947Z-
    fec6da52: the authorized repair was replaced by the broken original in
    the original-test copy)."""
    wanted = declared_cases(name, original)
    if wanted is None:
        return [f'{name}: the run-start version declares no case names that can be read, so its '
                'cases cannot be shown to survive the repair']
    data = current.get(name)
    if data is None:
        return [f'{name}: the repaired file is gone']
    now = declared_cases(name, data) or []
    problems = [f'{name}: case "{c}" was removed by the repair' for c in sorted(set(wanted) - set(now))]
    py = _test_family(name)[1] == 'py'
    if not py:
        for other, body in current.items():
            if other == name or _test_family(other) is None or _test_family(other)[1] == 'py':
                continue
            clash = set(wanted) & set(declared_cases(other, body) or [])
            if clash:
                problems.append(f'{name}: case name(s) {", ".join(sorted(clash))} also declared in {other}, '
                                'so their execution cannot be attributed to the repaired file')
    executed, skipped = set(), set()
    for r in receipts:
        get = (lambda k, r=r: r.get(k)) if isinstance(r, dict) else (lambda k, r=r: getattr(r, k, None))
        cases = get('cases') or {}
        skipped |= set(cases.get('skipped') or {})
        if get('status') == 'passed' and get('required') is not False:
            executed |= set(cases.get('executed') or {})

    def seen(case, pool):
        return any(k == case or (py and k.endswith('::' + case) and k.split('::')[0] == name) for k in pool)
    for case in wanted:
        if seen(case, skipped):
            problems.append(f'{name}: case "{case}" was skipped')
        elif not seen(case, executed):
            problems.append(f'{name}: case "{case}" is not shown executing in a passed required check')
    return problems


def run_original_tests(gate, root, originals: dict, current: dict, keep=()):
    """Run the gate's own commands against the current source with every
    run-start test and support file restored to its run-start bytes.

    A copy is built from ``current`` (the project as it is now): test and
    support files added during the run are left out, changed or removed ones
    are written back from ``originals``, and everything else is the current
    source. Nothing is written to the project. A command that names a test
    file added during the run cannot run there and is skipped with that
    reason; tests the run added are checked by the ordinary gate, not here.

    ``keep`` names run-start test files the operator authorized this run to
    repair: their current bytes stand in the copy instead of the run-start
    ones, which stay recorded as evidence. Every other original is restored.

    Returns ``(result, problem)``: a :class:`GateResult` whose receipts are
    named ``original-tests:<id>``, or ``None`` and why nothing was checked.
    """
    keep = {k for k in keep if k in originals and k in current}
    root = Path(root).resolve()
    commands = _gate_commands(gate, root)
    if commands is None:
        return None, 'the check commands could not be read for an original-test run'
    with tempfile.TemporaryDirectory(prefix='quadratus-original-tests-') as directory:
        copy = Path(directory).resolve()
        for name, data in current.items():
            if is_test_support(name) and name not in originals:
                continue
            target = copy / name
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(data)
            try:
                shutil.copymode(root / name, target)
            except OSError:
                pass
        for name, data in originals.items():
            if name in keep:
                continue
            target = copy / name
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(data)
        for name in _DEPENDENCY_DIRS:
            source = root / name
            if source.is_dir() and not source.is_symlink():
                (copy / name).symlink_to(source, target_is_directory=True)
        inside = re.compile(re.escape(str(root)) + r'(?=/|$)')
        mapped = []
        for command in commands:
            argv = tuple(inside.sub(str(copy), a) for a in command.argv)
            # A pytest node id (file::test) names its file (Codex preliminary
            # review of b6ba3ba: the selector form escaped this check and ran
            # against a file the copy does not hold).
            added = [a for a in argv[1:] if not a.startswith('-') and _test_family(a.split('::')[0])
                     and _relative(command.cwd, a.split('::')[0], copy) not in originals]
            mapped.append(replace(command, argv=argv, skip_reason=command.skip_reason or (
                f"names test file(s) added during the run: {', '.join(added)}" if added else '')))
        suite = GateSuite(mapped, cwd=copy)
        result = suite.run()
    receipts = tuple(replace(r, id=f'original-tests:{r.id}', command=inside.sub('<project>', r.command)
                             .replace(str(copy), '<original-tests>'),
                             output=r.output.replace(str(copy), '<original-tests>'))
                     for r in result.receipts)
    if not any(r.status != 'skipped' for r in receipts):
        reasons = '; '.join(f'{r.id}: {r.reason}' for r in receipts) or 'no command'
        return None, f'no check command could run against the original tests ({reasons})'
    output = '\n'.join(f'{r.id}: {r.status}: {r.reason}\n{r.output}' for r in receipts)
    passed = all(r.status in ('passed', 'skipped') for r in receipts if r.required) and any(
        r.status == 'passed' for r in receipts)
    return GateResult(passed, 'original tests', 0 if passed else 1, output, receipts), ''


def scratch_report_env(argv) -> dict:
    """The reporting-only environment under which a pytest command prints
    one ``path::name OUTCOME`` line per case whatever ``-q`` its argv
    carries, so the harness can name the scratch cases it executed. Empty
    for any other runner, whose case lines already carry names."""
    if runner_of(argv) != 'pytest':
        return {}
    level = 0
    for arg in (str(a) for a in argv):
        if arg in ('--verbose',):
            level += 1
        elif arg in ('--quiet',):
            level -= 1
        elif re.fullmatch(r'-v+', arg):
            level += len(arg) - 1
        elif re.fullmatch(r'-q+', arg):
            level -= len(arg) - 1
    if level >= 1:
        return {}
    extra = '-' + 'v' * (1 - level)
    inherited = os.environ.get('PYTEST_ADDOPTS', '')
    return {'PYTEST_ADDOPTS': f'{inherited} {extra}'.strip()}


def scratch_receipt(results, scratch: dict, others: dict) -> dict:
    """Whether the harness's own runs of approved commands executed every
    case each scratch test file declares, and each passed (r09 root
    decision on 40b2133: a new file name alone is not execution).

    ``results`` are GateResults from the disposable copy; ``scratch`` and
    ``others`` map copy-relative test paths to bytes, the scratch files and
    the handed test files. A case counts only by identity: a pytest line
    naming the scratch file, or a node case name the scratch file declares
    and no handed test file does. Each instance a runner printed is kept
    with its own id and count (root review of 1c14ea9: a parametrized case
    whose True instance passed and False instance skipped was collapsed to
    its function and passed). A declared case is passed only when it has at
    least one instance and every instance executed and passed; one failed
    instance makes it failed, and a skipped or otherwise unexecuted one
    makes it not executed. ``status`` is ``passed`` only when every declared
    case passed, ``failed`` when any failed, ``not run`` otherwise. Nothing
    here is a verdict on a requirement. No command text or path outside the
    copy is returned."""
    out = dict(files=sorted(scratch), cases=[], receipts=[])
    declared = {}
    for path, data in sorted(scratch.items()):
        names = declared_cases(path, data)
        if not names:
            return dict(out, status='not run', reason=f'{path}: its case names could not all be read')
        declared[path] = set(names)
    taken = set()
    for path, data in others.items():
        taken |= set(declared_cases(path, data) or ())
    instances = {}
    for result in results:
        for receipt in result.receipts:
            out['receipts'].append(dict(id=receipt.id, status=receipt.status, reason=receipt.reason,
                                        tests=receipt.tests, source_hash=receipt.source_hash))
            if receipt.status in ('blocked', 'error', 'skipped') or not isinstance(receipt.cases, dict):
                continue
            cases = receipt.cases
            bad = cases.get('failed') or {}
            qualified = set(cases.get('qualified') or ())
            for kind in ('executed', 'skipped', 'other'):
                for key, count in (cases.get(kind) or {}).items():
                    if key in qualified:
                        file, _, rest = key.partition('::')
                        name = rest.split('::')[-1].split('[', 1)[0]
                        paths = [p for p in declared
                                 if p in (file, posixpath.normpath(posixpath.join(receipt.cwd, file)))]
                    else:
                        name = key
                        paths = [p for p in declared if key in declared[p]] if key not in taken else []
                    if len(paths) != 1 or name not in declared[paths[0]]:
                        continue
                    seen = instances.setdefault((paths[0], name), {})
                    if kind == 'executed':
                        failed = min(int(bad.get(key, 0)), int(count))
                        outcomes = ['failed'] * failed + ['passed'] * (int(count) - failed)
                    else:
                        outcomes = ['skipped' if kind == 'skipped' else 'not executed'] * int(count)
                    seen.setdefault(key, []).extend(outcomes)
    verdicts = {}
    rows = []
    for path, names in declared.items():
        for name in names:
            seen = instances.get((path, name), {})
            outcomes = [o for runs in seen.values() for o in runs]
            verdicts[(path, name)] = ('failed' if 'failed' in outcomes
                                      else 'passed' if outcomes and set(outcomes) == {'passed'}
                                      else 'not executed')
            if not seen:
                rows.append(f'{path}::{name}: not executed')
            for key, runs in seen.items():
                label = key if '::' in key else f'{path}::{key}'
                rows.extend(f'{label}: {o}' for o in runs)
    out['cases'] = sorted(rows)
    if 'failed' in verdicts.values():
        return dict(out, status='failed', reason='a scratch case instance executed and failed')
    missing = [k for k, v in verdicts.items() if v != 'passed']
    if missing:
        return dict(out, status='not run',
                    reason=f'{len(missing)} declared scratch case(s) with an instance not shown executed and passed')
    return dict(out, status='passed', reason='every instance of every declared scratch case executed and passed')


def _relative(cwd: str, operand: str, copy: Path) -> str:
    """``operand`` as a project-relative path, for a command run in ``cwd``."""
    path = Path(operand)
    if path.is_absolute():
        try:
            return path.resolve().relative_to(copy).as_posix()
        except ValueError:
            return operand
    import posixpath
    return posixpath.normpath(posixpath.join(cwd, operand))


def skipped_count(receipt) -> Optional[int]:
    """Cases a check skipped: the structured report's count when it parsed,
    else the runner's own summary line, else None (unknown, never zero)."""
    report = getattr(receipt, 'report', None) or {}
    if isinstance(report, dict) and report.get('state') == 'parsed':
        counts = report.get('counts')
        value = counts.get('skipped') if isinstance(counts, dict) else None
        # A parsed report that lost its count is an incomplete record:
        # unknown (None), never zero and never a crash in a consumer that
        # is deciding whether to refuse it (root's control on d88d8c0).
        return value if isinstance(value, int) and not isinstance(value, bool) else None
    output = getattr(receipt, 'output', '') or ''
    node = re.findall(r'(?m)^(?:#|\u2139)\s*(?:skipped|skip) (\d+)\s*$', output)
    if node:
        return int(node[-1])
    for line in reversed(output.splitlines()):
        found = re.findall(r'\b(\d+) (passed|failed|skipped|deselected|errors?)\b', line)
        if found:
            return sum(int(n) for n, kind in found if kind == 'skipped')
    unittest = re.findall(r'skipped=(\d+)', output)
    return int(unittest[-1]) if unittest else None

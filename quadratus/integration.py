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
           "CheckUnattributable", "attribute", "read_report", "model_facing",
           "UnsafeWritePath", "write_bound", "remove_bound", "sha256_capped"]

#: The argv token a check uses to declare the harness's structured report.
#: The only supported producer is the harness-owned pytest plugin
#: (``_gate_producer``), declared as ``--quadratus-report={report}``. The
#: harness replaces the token with a path it owns, outside the project, loads
#: the plugin for that one invocation, and removes the path after reading it
#: (phase 3, #25). Other runners have no producer: their failures are never
#: attributable, so never repaired.
REPORT_TOKEN = "{report}"
PRODUCER = "quadratus-pytest/5"
#: The pytest producer before it named its passed cases (review 2026-11-10,
#: checks 13). Its reports stay readable; they carry no passed cases.
_LEGACY_PRODUCERS = ("quadratus-pytest/4",)
#: The node:test producer (series rule-58a4625 f3: the second cycle in a row
#: ended on a failing node UI test with the work done and the graders
#: passing, because a node check had no producer and so no fix call). A
#: ``node --test`` check declares ``--test-reporter-destination={report}``;
#: the harness copies the shipped reporter and passes it as a second
#: reporter beside TAP on stdout, so the text output the count reader needs
#: is unchanged.
PRODUCER_NODE = "quadratus-node/1"
PRODUCERS = (PRODUCER, PRODUCER_NODE) + _LEGACY_PRODUCERS
_PRODUCER_FILE = Path(__file__).resolve().parent / "_gate_producer" / "quadratus_gate_report.py"
_NODE_PRODUCER_FILE = _PRODUCER_FILE.with_suffix(".mjs")
#: Kept for the install-layout proof: where the shipped producer lives.
_PRODUCER_DIR = str(_PRODUCER_FILE.parent)
_REPORT_FAILURES = 200
#: Bounds on the passed cases a report may name; the producer holds the same.
_REPORT_PASSED = 20_000
_REPORT_CASE_CHARS = 400
_WHEN = ("setup", "call", "teardown")
_COUNTS = ("passed", "failed", "errors", "skipped")
_REPORT_MAX_BYTES = 1024 * 1024


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
    if _is_node_test(argv):
        source = _NODE_PRODUCER_FILE.read_bytes()
        copy = os.path.join(owned, f"quadratus_gate_report_{secrets.token_hex(8)}.mjs")
        with open(copy, "wb") as out:
            out.write(source)
        expected = dict(nonce=nonce, module_file=os.path.realpath(copy),
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
    expected = dict(nonce=nonce, module_file=os.path.realpath(copy),
                    module_sha256=hashlib.sha256(source).hexdigest())
    plugins = [p for p in env.get("PYTEST_PLUGINS", "").split(",") if p.strip()]
    env = dict(env, QUADRATUS_GATE_NONCE=nonce, PYTEST_PLUGINS=",".join(plugins + [module]),
               PYTHONPATH=os.pathsep.join([owned] + [p for p in [env.get("PYTHONPATH")] if p]))
    try:
        yield [a.replace(REPORT_TOKEN, path) for a in argv], env, path, expected
    finally:
        shutil.rmtree(owned, ignore_errors=True)


def _is_node_test(argv) -> bool:
    """A ``node --test`` invocation (the runner is node and ``--test`` is an
    argument of its own), the only non-pytest runner with a producer."""
    head = Path(argv[0]).name if argv else ""
    return head in ("node", "nodejs") and "--test" in argv[1:]


def model_facing(argv):
    """The check as a model may run it: the harness's report declaration
    removed. The token names nothing outside the harness's own run (series
    rule-58a4625 f3: two leads ran the pytest check with the literal token
    and got exit 4, unrecognized arguments)."""
    return [a for a in argv if REPORT_TOKEN not in a]


def _open_regular(path):
    """A read descriptor for ``path`` when it is a regular file: opened
    without following a link and without blocking on a FIFO or device, and
    confirmed to be the file that was stat'ed. Returns (fd, None) or
    (None, (state, detail)); the caller closes the descriptor."""
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
    except OSError as exc:
        os.close(fd)
        return None, ("unparsable", f"{type(exc).__name__}: {str(exc)[:120]}")
    if not st.S_ISREG(opened.st_mode) or (opened.st_dev, opened.st_ino) != (before.st_dev, before.st_ino):
        os.close(fd)
        return None, ("unparsable", "replaced while being opened")
    return fd, None


def _read_capped(path, limit: int = _REPORT_MAX_BYTES):
    """A file's bytes, read without following a link, without blocking on a
    FIFO or device, and never more than ``limit`` plus one byte: from a
    descriptor whose regular-file identity matches what was stat'ed.
    Returns (bytes, None) or (None, (state, detail)). The one reader for
    files a project could have planted: gate reports and capture evidence
    (review 2026-11-10, checks 11)."""
    fd, problem = _open_regular(path)
    if problem:
        return None, problem
    try:
        chunks, total = [], 0
        while total <= limit:
            chunk = os.read(fd, min(65536, limit + 1 - total))
            if not chunk:
                break
            chunks.append(chunk)
            total += len(chunk)
        if total > limit:
            return None, ("unparsable", f"more than {limit} bytes")
        return b"".join(chunks), None
    except OSError as exc:
        return None, ("unparsable", f"{type(exc).__name__}: {str(exc)[:120]}")
    finally:
        os.close(fd)


def sha256_capped(path, limit: int) -> Optional[str]:
    """The SHA256 of a regular file of at most ``limit`` bytes, read as
    :func:`_read_capped` reads but hashed as it goes, so a large render is
    never held in memory. None for anything else (a link, a FIFO, a device,
    a missing, oversized or unreadable file)."""
    fd, problem = _open_regular(path)
    if problem:
        return None
    try:
        digest, total = hashlib.sha256(), 0
        while True:
            chunk = os.read(fd, 1 << 20)
            if not chunk:
                return digest.hexdigest()
            total += len(chunk)
            if total > limit:
                return None
            digest.update(chunk)
    except OSError:
        return None
    finally:
        os.close(fd)


class UnsafeWritePath(OSError):
    """A harness write was refused at its path: a component is a symlink or
    not a folder, or a name would leave the folder the write is bound to.
    Nothing was written through it."""


_DIR_FLAGS = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) | getattr(os, "O_NOFOLLOW", 0)


def _plain_parts(relative) -> tuple:
    parts = (tuple(PurePosixPath(os.fspath(relative)).parts) if isinstance(relative, (str, os.PathLike))
             else tuple(relative))
    if not parts or any(not isinstance(p, str) or p in ("", ".", "..") or "/" in p or "\0" in p for p in parts):
        raise UnsafeWritePath(f"not a plain path inside its folder: {str(relative)[:120]!r}")
    return parts


@contextmanager
def _bound_folder(root, parts, *, create: bool = True):
    """A descriptor for the folder ``root/parts...``, reached one component
    at a time, each opened without following a link relative to the handle
    before it (as ``session._write_fixture_bound`` does). A link at any
    component, checked earlier or swapped in since, is refused at the
    operation itself, so a write can never land outside ``root``. ``root``
    is the operator's own folder and is opened as named. Missing folders
    are made when ``create``; without it a missing one yields None."""
    fds: List[int] = []
    try:
        try:
            fds.append(os.open(os.fspath(root), os.O_RDONLY | getattr(os, "O_DIRECTORY", 0)))
        except OSError as exc:
            raise UnsafeWritePath(f"{root} is not a folder that can be opened ({type(exc).__name__})") from exc
        missing = False
        for index, part in enumerate(parts):
            try:
                try:
                    fd = os.open(part, _DIR_FLAGS, dir_fd=fds[-1])
                except FileNotFoundError:
                    if not create:
                        missing = True
                        break
                    try:
                        os.mkdir(part, 0o755, dir_fd=fds[-1])
                    except FileExistsError:
                        pass    # made meanwhile; the open below decides what it is
                    fd = os.open(part, _DIR_FLAGS, dir_fd=fds[-1])
            except OSError as exc:
                raise UnsafeWritePath(f"{'/'.join(parts[:index + 1])} is a symlink or not a folder; nothing "
                                      f"was written ({type(exc).__name__})") from exc
            fds.append(fd)
        yield None if missing else fds[-1]
    finally:
        for fd in reversed(fds):
            try:
                os.close(fd)
            except OSError:
                pass


def write_bound(root, relative, data: bytes) -> None:
    """Write ``data`` at ``root/relative`` through folder handles
    (:func:`_bound_folder`), replacing what is there.

    Every harness write under a project's ``.quadratus`` goes through this
    (review 2026-11-10, checks 5: a committed link at
    ``.quadratus/source-excludes.json`` or a linked evidence folder took the
    write outside the project). The bytes go to a private name made
    exclusively in the bound folder and are then renamed over the target, so
    a link standing at the target is replaced, never written through, and a
    reader never sees a partial file. Raises :class:`UnsafeWritePath` when
    the path is refused, ``OSError`` when the write itself fails."""
    import secrets
    parts = _plain_parts(relative)
    with _bound_folder(root, parts[:-1]) as folder:
        temp = f".{parts[-1][:80]}.{os.getpid()}.{secrets.token_hex(6)}.tmp"
        fd = os.open(temp, os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0), 0o644,
                     dir_fd=folder)
        try:
            try:
                view = memoryview(data)
                while view:
                    view = view[os.write(fd, view):]
            finally:
                os.close(fd)
            try:
                os.replace(temp, parts[-1], src_dir_fd=folder, dst_dir_fd=folder)
            except OSError as exc:
                raise UnsafeWritePath(f"{'/'.join(parts)} could not be replaced; nothing was written there "
                                      f"({type(exc).__name__})") from exc
        except BaseException:
            try:
                os.unlink(temp, dir_fd=folder)    # this call's own name, inside the bound folder
            except OSError:
                pass
            raise


def remove_bound(root, relative) -> None:
    """Remove the file at ``root/relative`` through folder handles, if it is
    there. A link at the name is removed itself, never followed; a folder
    there is refused."""
    parts = _plain_parts(relative)
    with _bound_folder(root, parts[:-1], create=False) as folder:
        if folder is None:
            return
        try:
            os.unlink(parts[-1], dir_fd=folder)
        except FileNotFoundError:
            pass
        except OSError as exc:
            raise UnsafeWritePath(f"{'/'.join(parts)} is not a file that can be replaced "
                                  f"({type(exc).__name__})") from exc


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
    # The cases the producer names as passed (quadratus-pytest/5). An older
    # or node report names none; the current pytest producer always does.
    passed = data.get("passed_cases")
    if passed is None and "passed_truncated" not in data and data.get("producer") != PRODUCER:
        return ""
    if (not isinstance(passed, list) or len(passed) > _REPORT_PASSED
            or any(not isinstance(p, str) or not p or len(p) > _REPORT_CASE_CHARS for p in passed)
            or type(data.get("passed_truncated")) is not bool):
        return "passed cases"
    if len(passed) > counts["passed"] or (not data["passed_truncated"] and len(passed) != counts["passed"]):
        return "passed cases do not match the counts"
    if "xfailed" in data and not (_is_count(data["xfailed"]) and data["xfailed"] <= counts["skipped"]):
        return "xfailed"
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
    if (data.get("producer") not in PRODUCERS or data.get("nonce") != expected.get("nonce")
            or data.get("module_file") != expected.get("module_file")
            or data.get("module_sha256") != expected.get("module_sha256")):
        return dict(state="foreign", detail=f"producer {str(data.get('producer'))[:40]!r}, module "
                                            f"{str(data.get('module_file'))[-60:]!r}")
    problem = _malformed(data)
    if problem:
        return dict(state="unparsable", detail=f"malformed or impossible: {problem}")
    report = dict(state="parsed", exitstatus=data["exitstatus"], collected=data["collected"],
                  collect_errors=data["collect_errors"], counts={k: data["counts"][k] for k in _COUNTS},
                  failures=[dict(nodeid=f["nodeid"][:300], when=f["when"], exc_type=(f["exc_type"] or "")[:120],
                                 assertion=f["assertion"], body=f.get("body"))
                            for f in data["failures"]],
                  truncated=data["truncated"])
    if isinstance(data.get("passed_cases"), list):
        # Named passes, so a quiet run (-q prints no case lines) still shows
        # which cases executed; ``passed_truncated`` says the list is short.
        report.update(passed_cases=list(data["passed_cases"]), passed_truncated=data["passed_truncated"])
    if "xfailed" in data:
        # Expected failures, counted among the skips: the runner's own
        # summary counts them as executed, and so does the gate.
        report["xfailed"] = data["xfailed"]
    return report


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


#: What one stream of a gate command may leave in memory: its first bytes
#: (where a runner prints its first failures) and its last (its summary).
#: What falls between is counted and dropped as it arrives, so a command
#: that prints for its whole timeout cannot grow the harness (review
#: 2026-11-10, checks 9).
_KEEP_HEAD_BYTES = 64 * 1024
_KEEP_TAIL_BYTES = 256 * 1024
#: A line longer than this is never a case line or a summary line and is
#: not scanned as one.
_MAX_SCAN_LINE = 4096
#: Of a receipt's kept output, what the lesser stream keeps when both do
#: not fit: stderr's end beside a long stdout, or stdout's end (where a
#: runner's summary is) beside the stderr of a command that failed there.
_STDERR_SHARE = 600


class _Stream:
    """One output stream of a gate command, bounded as it arrives. Each
    whole line is offered to ``scan`` first, so case lines are read from
    everything the command printed and not only from what is kept."""

    def __init__(self, scan=None):
        self.head, self.tail, self.dropped = bytearray(), bytearray(), 0
        self._scan = scan
        self._line, self._skipping = bytearray(), False

    def feed(self, block: bytes) -> None:
        if self._scan is not None:
            self._lines(block)
        room = _KEEP_HEAD_BYTES - len(self.head)
        if room > 0:
            self.head += block[:room]
            block = block[room:]
        if block:
            self.tail += block
            over = len(self.tail) - _KEEP_TAIL_BYTES
            if over > 0:
                del self.tail[:over]
                self.dropped += over

    def _lines(self, block: bytes) -> None:
        # Four bytes a character at most; the scanner holds the character bound.
        limit, start = 4 * _MAX_SCAN_LINE, 0
        while True:
            end = block.find(b'\n', start)
            if end < 0:
                break
            if not self._skipping and len(self._line) + end - start <= limit:
                self._offer(bytes(self._line) + block[start:end])
            self._line.clear()
            self._skipping = False
            start = end + 1
        if not self._skipping:
            if len(self._line) + len(block) - start > limit:
                self._line.clear()
                self._skipping = True       # over-long: dropped up to its newline
            else:
                self._line += block[start:]

    def _offer(self, raw: bytes) -> None:
        for line in raw.decode('utf-8', errors='replace').splitlines():
            self._scan(line)

    def finish(self) -> None:
        """The last line, when the stream ended without a newline."""
        if self._scan is not None and self._line and not self._skipping:
            self._offer(bytes(self._line))
        self._line.clear()

    def text(self) -> str:
        if not self.dropped:
            return bytes(self.head + self.tail).decode('utf-8', errors='replace')
        return (bytes(self.head).decode('utf-8', errors='replace')
                + f'\n[... {self.dropped} bytes of output not kept ...]\n'
                + bytes(self.tail).decode('utf-8', errors='replace'))


class _Output(str):
    """A gate command's retained output: stderr's text, then stdout's, so a
    reader that scans from the end meets the runner's own summary before
    anything written to stderr. ``stdout`` and ``stderr`` are the two
    streams apart; ``cases`` holds the case lines stdout printed, read as
    they streamed (:class:`_CaseScan`)."""

    stdout: str = ''
    stderr: str = ''
    cases = None

    def __new__(cls, stdout: str, stderr: str, cases=None):
        value = super().__new__(cls, '\n'.join(x for x in (stderr.strip(), stdout.strip()) if x))
        value.stdout, value.stderr, value.cases = stdout, stderr, cases
        return value


def _streams(output) -> tuple:
    """``(stdout, stderr)`` of a command's output. A plain string (a
    replaced runner) is all stdout."""
    return getattr(output, 'stdout', output) or '', getattr(output, 'stderr', '') or ''


def _reports_on_stderr(runner, passed: bool) -> bool:
    """Whether a command's kept output should favour stderr: it did not
    pass, and its runner is not one known to report on stdout."""
    return not passed and runner not in _STDOUT_RUNNERS


def _kept(output, *, lead: str = '', trail: str = '', favour_stderr: bool = False) -> str:
    """What a receipt keeps of a command's output, at most ``_TAIL_CHARS``:
    ``lead`` (a note the harness adds), the end of stderr, the end of
    stdout, then ``trail``. Stdout is last because the readers of a kept
    tail (``skipped_count``, ``deselected_count``, ``_test_count``) take
    the last summary they meet, and that must be the runner's own on
    stdout, not something a subprocess or an exit handler wrote to stderr
    (review 2026-11-10, checks 15). Stderr still keeps a share, so a crash
    it reported is not pushed out by a long stdout.

    ``favour_stderr`` (:func:`_reports_on_stderr`) turns the shares round
    for a command that failed and may have said why on stderr: jest and
    unittest report there, and behind a long stdout (a coverage table, a
    printing suite) the receipt kept 600 characters of npm's error lines
    and lost the assertion (final review 2026-11-11, gate 5). The order is
    the same, and stdout keeps its end, so a summary printed there is
    still the last one the readers meet."""
    stdout, stderr = (s.strip() for s in _streams(output))
    notes = [n.strip() for n in (lead, trail)]
    budget = max(0, _TAIL_CHARS - sum(len(n) + 1 for n in notes if n))
    if len(stdout) + len(stderr) + 1 > budget:
        major, minor = (stderr, stdout) if favour_stderr else (stdout, stderr)
        share = min(len(minor), max(_STDERR_SHARE, budget - len(major) - 1), budget)
        minor = minor[-share:] if share else ''
        room = budget - (len(minor) + 1 if minor else 0)
        major = major[-room:] if room > 0 else ''
        stderr, stdout = (major, minor) if favour_stderr else (minor, major)
    return '\n'.join(x for x in (notes[0], stderr, stdout, notes[1]) if x)[-_TAIL_CHARS:]


def _run_owned(argv, *, cwd, timeout, env):
    """Run one gate command and return ``(returncode, output, cleanup)``;
    ``returncode`` is None when it timed out.

    The command runs in a new session, so the group holds it and every
    descendant that did not leave on its own. Output is read as it arrives,
    so a timeout keeps what was printed before it; only a bounded head and
    tail of each stream are kept (:class:`_Stream`), and stdout's case
    lines are read as they pass (:class:`_Output`). When the command times
    out, or exits leaving its own processes behind (a preview server holding
    its pipes open), those processes are stopped by :func:`_stop_owned` and
    ``cleanup`` says which. Nothing outside the group is signalled."""
    import threading
    import time
    proc = subprocess.Popen(argv, cwd=cwd, env=env, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                            stderr=subprocess.PIPE, start_new_session=True)
    scan = _CaseScan()
    streams = {'out': _Stream(scan.line), 'err': _Stream()}

    def drain(stream, key):
        sink = streams[key]
        for block in iter(lambda: stream.read1(65536) if hasattr(stream, 'read1') else stream.read(65536), b''):
            sink.feed(block)
        sink.finish()
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
    output = _Output(streams['out'].text(), streams['err'].text(), scan)
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

    The environment is the check profile (``child_env.check_env``): a check
    runs project code, so it inherits no credential-shaped name from the
    harness. ``QUADRATUS_ENV_PASS`` keeps one a check really needs.
    """
    from .child_env import check_env
    prefix = tempfile.mkdtemp(prefix="quadratus-gate-pyc-")
    try:
        env, _ = check_env()
        env.update(PYTHONPYCACHEPREFIX=prefix, PYTHONDONTWRITEBYTECODE="1")
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
        shown = shlex.join(str(a) for a in self.command)
        try:
            with _fresh_bytecode_env() as fresh, _report_slot(self.command, fresh) as (argv, env, path, expected):
                returncode, combined, cleanup = _run_owned(argv, cwd=self.cwd, timeout=self.timeout, env=env)
                report = read_report(path, expected) if returncode is not None else None
        except FileNotFoundError as exc:
            return GateResult(
                passed=False, command=shown, returncode=None,
                output=f"(command not found: {exc})",
            )
        except (OSError, ValueError) as exc:
            # A command that cannot be started is a failed result like any
            # other, never an exception out of the gate (review 2026-11-10,
            # checks 8: a file that is not executable, a cwd that is gone).
            return GateResult(
                passed=False, command=shown, returncode=None,
                output=f"(command could not start: {type(exc).__name__}: {str(exc)[:300]})",
            )
        if returncode is None:
            # The partial output is kept: a hang must name what it was doing.
            return GateResult(
                passed=False, command=shown, returncode=None,
                output=_kept(combined, lead=f"(timed out after {self.timeout}s) {_cleanup_note(cleanup)}",
                             favour_stderr=_reports_on_stderr(runner_of(self.command), False)),
            )
        runner = runner_of(self.command)
        passed, lead = returncode == 0, ""
        if passed and self.minimum_tests is not None:
            count = _executed(combined, runner, report)
            if count is None:
                passed, lead = False, "(test count unavailable)"
            elif count < self.minimum_tests:
                passed, lead = False, f"(zero tests executed: {count} ran)"
        return GateResult(
            passed=passed,
            command=shown,
            returncode=returncode,
            output=_kept(combined, lead=lead, trail=_cleanup_note(cleanup),
                         favour_stderr=_reports_on_stderr(runner, passed)),
            report=report,
            cases=case_record(combined, runner),
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


_ANSI = re.compile(r'\x1b\[[0-9;?]*[ -/]*[@-~]')
_OUTCOMES = 'passed|failed|xfailed|xpassed|skipped|deselected|errors?'
#: "todo" is read as an item (in a runner's named row only) so that a row
#: of nothing but todo cases ("Tests:  2 todo, 2 total") is a summary of
#: zero executed tests; it is never counted as executed or as a skip.
_SUMMARY_ITEM = re.compile(rf'\b(\d+) ({_OUTCOMES}|todo)\b')
#: A runner's summary line starts with its first count: pytest's "3 failed,
#: 6 passed in 0.11s" (bare under -q, inside a rule of "=" otherwise),
#: jest's "Tests:  1 failed, 2 passed, 3 total", vitest's "Tests  2 passed
#: (2)", deno's "ok | 3 passed | 0 failed", nextest's "Summary [0.1s] 3
#: tests run: 3 passed". A count somewhere inside another sentence is not a
#: summary: tsc's "Found 0 errors." and eslint's "5 problems (0 errors, 5
#: warnings)" read as zero executed tests and failed a passing lint command
#: (review 2026-11-10, checks 6).
#: Jest puts its todo count before its passes ("Tests:  1 todo, 2 passed, 3
#: total"), so "todo" may open a summary too.
_SUMMARY_HEAD = re.compile(
    r'[ \t=]*(?P<row>(?:Tests?:?|ok \||FAILED \||Summary[ \t]+\[[^\]\n]{0,40}\][ \t]+\d+ tests? run:)[ \t]+)?'
    rf'\d+ (?:{_OUTCOMES}|warnings?|rerun|todo)\b')
_PYTEST_TIMED = re.compile(r'\bin \d[\d.]*s\b')
#: "no tests ran in 0.01s" (pytest), "No tests found, exiting with code 1"
#: (jest), "NO TESTS RAN" (unittest): the line itself, not the words inside
#: a test's name.
_NO_TESTS = re.compile(r'(?im)^[ \t=*]*no tests (?:ran|collected|found)\b')
_UNITTEST_RAN = re.compile(r'(?m)^Ran (\d+) tests?\b')
_UNITTEST_SKIPPED = re.compile(r'(?m)^(?:OK|FAILED) \([^)\n]*\bskipped=(\d+)')
#: Something often stands in front of a runner's line: the label of a tool
#: that runs several packages (turbo's "web:test: ", compose's "svc-1  | ",
#: yarn's "➤ YN0000: [pkg]: "), a build step's number and clock ("#12 0.532
#: "), a timestamp, an indent. Where the runner's own summary may begin
#: behind it: after whitespace or a rule, at a count, a row's name or the
#: "no tests" words.
_SUMMARY_START = re.compile(
    rf'(?<![^\s=*|])(?:\d+ (?:{_OUTCOMES}|warnings?|rerun|todo)\b|Tests?:?[ \t]+\d|ok \||FAILED \|'
    r'|Summary[ \t]+\[|Ran \d+ tests?\b|(?:OK|FAILED) \(|test result: (?:ok|FAILED)\.'
    r'|(?i:no tests (?:ran|collected|found)\b))')
#: How many such places one line is tried at.
_SUMMARY_STARTS = 4
#: What marks the text in front as a case's own line, whose name can hold
#: any words: a pytest node id, TAP's "ok N", a reporter's mark.
_CASE_PREFIX = re.compile('::|(?<!\\S)(?:not ok|ok)[ \t]+\\d'
                          '|[✓✔✕✖✗✘×√↓○✎﹣]')
#: What is read from behind a prefix: a runner's own line in its full form,
#: running to the end of the line. Bare words can be anything, a test's
#: description included ("List: no tests found ... ok" is how unittest -v
#: prints a docstring), and jest's "Snapshots: 3 passed, 3 total" counts no
#: cases.
_NO_TESTS_FULL = re.compile(r'(?i)no tests (?:ran|collected) in \d|no tests found, exiting with code \d'
                            r'|no tests (?:ran|collected|found)[.!]?[ \t=*]*$')
_UNITTEST_ROW = re.compile(r'Ran \d+ tests?\b|(?:OK|FAILED) \([^)\n]*\bskipped=\d')
_CARGO_ROW = re.compile(r'test result: (?:ok|FAILED)\. \d+ passed; \d+ failed;')
_TIMED_END = re.compile(r'\bin \d[\d.]*s(?: \([\d:]+\))?[ \t=]*$|\(\d[\d.]*m?s\)[ \t=]*$')
#: A wrapper's label: one to three words, the first starting like a name,
#: the last ending in ":", "|" or "]" (turbo's "web:test: ", pnpm's
#: "packages/a test: ", concurrently's "[0] "); up to three in a row.
_LABEL = r'(?=[A-Za-z0-9_@\[])(?:\S{1,80}[ \t]+){0,2}?\S{0,79}[:|\]][ \t]+'
_LABELS = re.compile(rf'(?:{_LABEL}){{1,3}}')
#: A bare count of cases that did not run, the whole of what a label
#: fronts: Playwright prints "  3 skipped" with no time when nothing ran.
#: Only this stricter reading is taken from a bare count; "Test Suites: 1
#: passed, 1 total" has the same shape and counts no cases.
_ONLY_UNEXECUTED = re.compile(r'\d+ (?:skipped|deselected)(?:,? \d+ (?:skipped|deselected))*[ \t=]*')


#: Runners whose summary does not start with a count. Each has a row of its
#: own shape, read only as the whole line it is, written here from the
#: runner's real output (cucumber-js 13, Robot Framework 7, CodeceptJS 4,
#: @web/test-runner 0.20):
#:   3 scenarios (1 passed, 1 skipped, 1 failed)
#:   3 tests, 1 passed, 1 failed, 1 skipped
#:     OK  | 3 passed, 1 skipped   // 23ms
#:   Chromium: |██████| 1/1 test files | 3 passed, 0 failed, 1 skipped
#: When only a line starting with a count was a summary these read as no
#: summary at all: an all-skipped cucumber or Robot run passed with nothing
#: executed and its skips unseen, and the same suites under "npm test" were
#: blocked for want of a count (final review 2026-11-11, gate 1). The count
#: inside the parentheses is admitted for cucumber's row alone; eslint's "5
#: problems (0 errors, 5 warnings)" is still no summary.
#: Cucumber's cases are its scenarios. Its "8 steps (...)" row is not read:
#: "3 steps (1 passed, 1 pending, 1 undefined)" belongs to a run in which no
#: scenario passed. WebdriverIO's "Spec Files: 1 passed, 1 total" counts
#: files, as jest's "Test Suites:" row does, and a file whose every case was
#: skipped passes: it is no count of cases and stays unread.
#: @web/test-runner's row: one a browser, on adjacent lines.
_BROWSER_ROW = re.compile(r'[ \t]*[A-Z][A-Za-z ]{1,30}:[ \t]+\|[^|\n]{0,200}\|[ \t]+\d+/\d+ test files[ \t]+\|'
                          r'[ \t]+(\d+ passed, \d+ failed(?:, \d+ skipped)?)[ \t]*')
_RUNNER_ROWS = (
    re.compile(r'[ \t]*(?:0 scenarios|\d+ scenarios? \((\d+ [a-z]+(?:, \d+ [a-z]+){0,6})\))[ \t]*'),
    re.compile(r'[ \t]*\d+ tests?(?: total)?, (\d+ passed, \d+ failed(?:, \d+ skipped)?)[ \t]*'),
    re.compile(r'[ \t]*(?:OK|FAIL)[ \t]+\|[ \t]+(\d+ passed(?:, \d+ (?:failed|failedHooks|skipped)){0,3})'
               r'[ \t]*(?://.{0,80})?'),
    _BROWSER_ROW,
)
_ROW_ITEM = re.compile(r'(\d+) ([A-Za-z]+)')
#: Where one of those rows may begin behind a wrapper's label. Tried only
#: on a line the count lines' own search read nothing from, so it adds
#: readings and changes none.
_ROW_START = re.compile(r'(?<![^\s=*|])(?:\d+ scenarios?\b|\d+ tests?(?: total)?, \d|(?:OK|FAIL)[ \t]+\|'
                        r'|[A-Z][A-Za-z]{1,30}:[ \t]+\|)')
#: What a row's word means. A case that reached no result (cucumber's
#: pending, undefined, ambiguous and unknown, or a word not known here) did
#: not run: a skip, never an execution. A failed hook is not a case.
_ROW_WORDS = {'passed': 'passed', 'failed': 'failed', 'failedHooks': None}


def _runner_row(line: str, rows=_RUNNER_ROWS) -> Optional[list]:
    """The ``(count, outcome)`` pairs of one of the named rows above when
    ``line`` is one, whole; None when it is not."""
    if len(line) > _MAX_SCAN_LINE or not (' passed' in line or ' scenario' in line):
        return None
    for row in rows:
        found = row.fullmatch(line)
        if found:
            pairs = [(count, _ROW_WORDS.get(word, 'skipped')) for count, word in _ROW_ITEM.findall(found.group(1) or '')]
            return [(count, outcome) for count, outcome in pairs if outcome]
    return None


def _own_summary(rest: str) -> bool:
    """Whether ``rest`` is a runner's own summary in full form: a row with
    its name ("Tests: ..."), a count line that ends with its time ("3
    skipped in 0.01s", "3 passed (2.1s)"), unittest's and cargo's rows, or a
    runner's whole "no tests" line."""
    head = _SUMMARY_HEAD.match(rest)
    if head:
        return bool(head.group('row') or _TIMED_END.search(rest))
    return bool(_NO_TESTS_FULL.match(rest) or _UNITTEST_ROW.match(rest) or _CARGO_ROW.match(rest))


def _behind(line: str, starts, accepted) -> Optional[str]:
    """The first place ``starts`` finds in ``line``, behind something else,
    whose text to the end of the line ``accepted(rest, start)`` takes."""
    for count, found in enumerate(starts.finditer(line)):
        start = found.start()
        if not start or count >= _SUMMARY_STARTS or _CASE_PREFIX.search(line, 0, start):
            return None     # the line's own start, or text inside a case's name
        rest = line[start:]
        if accepted(rest, start):
            return rest
    return None


def _prefixed_summary(line: str) -> Optional[str]:
    """The runner's own summary inside ``line`` when something stands in
    front of it, or None: a full-form count line first, as before, and
    failing that a named row of another runner (:func:`_runner_row`) that
    a wrapper's label fronts. Behind any other words a row is not read:
    "loaded 0 scenarios" is somebody's log line, not a run of nothing."""
    found = _behind(line, _SUMMARY_START, lambda rest, start: _own_summary(rest) or (
        _ONLY_UNEXECUTED.fullmatch(rest) and _LABELS.fullmatch(line, 0, start)))
    if found is None and (' passed' in line or ' scenario' in line):
        found = _behind(line, _ROW_START, lambda rest, start: (
            _LABELS.fullmatch(line, 0, start) and _runner_row(rest) is not None))
    return found


def _summary_text(output) -> str:
    """What the summary readers read of a command's output: colour codes
    removed, and a runner's own summary that something stood in front of
    repeated without it.

    The summary patterns are anchored to the start of a line, so a count
    inside a sentence or a test's own name is not a summary (checks 6).
    Anything in front hid the runner's own summary from them as well:
    "web:test: Tests: 3 skipped, 3 total" (turbo) and "#12 0.532 no tests
    ran in 0.01s" (a build step) read as no summary at all, so a wrapped
    run of zero tests passed and its skips and deselections went unseen,
    where the same lines unwrapped fail the command. Only a full-form
    runner line is read from behind a prefix (:func:`_own_summary`), and
    never from inside a case's own line, whose name can hold any text."""
    lines = []
    for line in _ANSI.sub('', output or '').splitlines():
        lines.append(line)
        if (len(line) > _MAX_SCAN_LINE or _SUMMARY_HEAD.match(line) or _NO_TESTS.match(line)
                or _runner_row(line) is not None):
            continue    # too long to be one, or read where it stands
        behind = _prefixed_summary(line)
        if behind is not None:
            lines.append(behind)
    return '\n'.join(lines)


def _summary_outcomes(text) -> Optional[list]:
    """The ``(count, outcome)`` pairs of the last line a runner printed as
    its own summary, or None when no line is one. ``text`` is what
    :func:`_summary_text` made of the output. One reading for executed,
    skipped and deselected counts, which each used to scan on their own and
    had drifted. A line with only an error count is a summary when it ends
    the way pytest's does ("1 error in 0.05s"). A named row of a runner
    whose summary starts some other way is one too (:func:`_runner_row`); it
    may hold no pair at all ("0 scenarios"), which is a summary of nothing
    executed and not the absence of one."""
    lines = (text or '').splitlines()
    for index in range(len(lines) - 1, -1, -1):
        line = lines[index]
        head = _SUMMARY_HEAD.match(line) if len(line) <= _MAX_SCAN_LINE else None
        if not head:
            row = _runner_row(line)
            if row is None:
                continue
            if _BROWSER_ROW.fullmatch(line):
                # A row for each browser: all of them, so one browser's
                # skips are not lost behind another's passes.
                for earlier in reversed(lines[:index]):
                    more = _runner_row(earlier, (_BROWSER_ROW,))
                    if more is None:
                        break
                    row += more
            return row
        found = _SUMMARY_ITEM.findall(line)
        if not head.group('row'):
            # A todo count is a runner's only in its named row; "3 todo
            # comments left" is some other tool's line.
            found = [(count, kind) for count, kind in found if kind != 'todo']
        if any(not kind.startswith('error') for _, kind in found) or (found and _PYTEST_TIMED.search(line)):
            return found
    return None


#: Runners that print their summary and case lines on stdout; for any
#: other, stderr is read when stdout says nothing (unittest writes there).
_STDOUT_RUNNERS = ('pytest', 'node')


def _executed(output, runner, report=None) -> Optional[int]:
    """How many cases a command executed, or None when nothing says.

    The structured report's count when it parsed and saw cases: its
    producer watched every one, where the text is scraped. Otherwise the
    runner's summary in stdout, and stderr's only for a runner not known to
    print its summary on stdout and only when stdout carries none: "3
    skipped" on stdout followed by "5 passed" from an exit handler on
    stderr counted five executed (review 2026-11-10, checks 15)."""
    if isinstance(report, dict) and report.get('state') == 'parsed' and report.get('collected'):
        # The same total the summary line gives: passes, failures and
        # expected failures (which the report counts among its skips).
        return report['counts']['passed'] + report['counts']['failed'] + (report.get('xfailed') or 0)
    stdout, stderr = _streams(output)
    count = _test_count(stdout)
    if count is None and runner not in _STDOUT_RUNNERS:
        count = _test_count(stderr)
    return count


def _test_count(output):
    """Recognize runner summaries only; absent evidence is not zero or success."""
    # Colour codes and a wrapper's line labels aside: the summary is still
    # the runner's.
    output = _summary_text(output)
    if _NO_TESTS.search(output):
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
    unittest = _UNITTEST_RAN.findall(output)
    if unittest:
        skipped = _UNITTEST_SKIPPED.findall(output)
        return max(0, int(unittest[-1]) - (int(skipped[-1]) if skipped else 0))
    # Cargo emits one summary per test binary, then often an empty doc-test
    # suite. The last summary alone would erase the cases already executed.
    rust = re.findall(r'(?m)^test result: (?:ok|FAILED)\. (\d+) passed; (\d+) failed;', output)
    if rust:
        return sum(int(passed) + int(failed) for passed, failed in rust)
    # Pytest (and compact JS summaries) can report failed executions alongside
    # passes. Use one final summary line, excluding cases whose body did not run.
    outcomes = _summary_outcomes(output)
    if outcomes is not None:
        return sum(int(count) for count, outcome in outcomes
                   if outcome in {'passed', 'failed', 'xfailed', 'xpassed'})
    return None


class GateSuite:
    """Ordered commands behind the existing run() seam, with same-tree receipts.

    Cache only explicitly marked, deterministic source-only gates. Database,
    network and other external-state gates must leave cacheable=False. Cache
    entries live for this runner instance and only successful receipts are kept.
    """

    def __init__(self, commands: Sequence[GateCommand], *, cwd, exclude=()):
        from .project import Project

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
                         exclude=self.project.exclude)
        view._cache = self._cache
        return view

    def _runner_hash(self, command, cwd):
        executable = command.argv[0]
        path = (cwd / executable) if '/' in executable else shutil.which(executable)
        if path is None:
            return ''
        files = [Path(__file__), Path(path)]
        # Include explicit runner scripts outside the project as well.
        files += [cwd / a for a in command.argv[1:] if (cwd / a).is_file()]
        digest = hashlib.sha256()
        for file in files:
            digest.update(str(file.resolve()).encode() + b'\0' + file.read_bytes())
        return digest.hexdigest()

    def _digests(self) -> dict:
        """The selected source by file, name to SHA256: the listing and the
        hash ``Project.fingerprint`` is built from, kept apart so a change
        can be named file by file."""
        return {name: hashlib.sha256(data).digest() for name, data in self.project.contents().items()}

    @staticmethod
    def _fingerprint(digests: dict) -> str:
        """``Project.fingerprint`` of the tree ``digests`` describes (a test
        holds the two equal)."""
        digest = hashlib.sha256()
        for name in sorted(digests):
            digest.update(name.encode() + b'\0' + digests[name])
        return digest.hexdigest()

    def _unreadable(self, exc) -> GateResult:
        """The whole gate as error receipts, when the source cannot be read
        before any command runs: nothing a command did could be told apart
        from what was already there, so none is run."""
        why = f'source could not be read before the gate: {type(exc).__name__}: {str(exc)[:160]}'
        receipts = tuple(GateReceipt(id=c.id, required=c.required, command=shlex.join(c.argv), cwd=c.cwd,
                                     status='skipped' if c.skip_reason else 'error',
                                     reason=c.skip_reason or why) for c in self.commands)
        output = '\n'.join(f'{r.id}: {r.status}: {r.reason}\n{r.output}' for r in receipts)
        return GateResult(False, 'gate suite', 1, output, receipts)

    def run(self) -> GateResult:
        receipts = []
        # Every failure mode is a receipt, never an exception (the module's
        # contract; review 2026-11-10, checks 8: a test that left a 101 MiB
        # file, or a file removed mid-walk, raised out of the gate).
        try:
            before = self._digests()
            source = self._fingerprint(before)
        except Exception as exc:  # noqa: BLE001 -- unreadable source is an error receipt
            return self._unreadable(exc)
        stopped = ''
        for command in self.commands:
            config = hashlib.sha256(json.dumps(asdict(command), sort_keys=True).encode()).hexdigest()
            base = dict(id=command.id, required=command.required, command=shlex.join(command.argv),
                        source_hash=source, config_hash=config, cwd=command.cwd,
                        selection=_selection(command.argv, self.cwd / command.cwd, self.cwd))
            if stopped or command.skip_reason:
                receipts.append(GateReceipt(**base, status='skipped', reason=stopped or command.skip_reason))
                continue
            if not command.argv:
                receipts.append(GateReceipt(**base, status='blocked', reason='command not configured'))
                continue
            try:
                cwd = (self.cwd / command.cwd).resolve()
                if not cwd.is_relative_to(self.cwd) or not cwd.is_dir():
                    receipts.append(GateReceipt(**base, status='blocked',
                                                reason='cwd unavailable or outside project'))
                    continue
                runner = self._runner_hash(command, cwd)
                base['runner_hash'] = runner
                key = (source, config, runner)
                if command.cacheable and runner and key in self._cache:
                    receipts.append(replace(self._cache[key], cached=True))
                    continue
                with _fresh_bytecode_env() as fresh, _report_slot(command.argv, fresh) as (argv, env, path, expected):
                    returncode, output, cleanup = _run_owned(argv, cwd=cwd, timeout=command.timeout, env=env)
                    report = read_report(path, expected) if returncode is not None else None
                if returncode is None:
                    raise _GateTimeout(output, cleanup)
                named = runner_of(command.argv)
                count = _executed(output, named, report)
                status, reason = ('passed', 'exit 0') if returncode == 0 else ('failed', 'nonzero exit')
                # A runner's own summary of zero executed cases fails the
                # command whatever it exited with. The count is read only
                # from lines a runner prints as its summary (checks 6), so a
                # lint or type check has none: it passes on exit 0 with its
                # count unknown, where "Found 0 errors." used to fail it.
                if count == 0:
                    status, reason = 'failed', 'zero tests executed'
                elif returncode == 0 and command.minimum_tests is not None:
                    if count is None:
                        status, reason = 'blocked', 'test count unavailable'
                    elif count < command.minimum_tests:
                        status, reason = 'failed', 'fewer tests than required'
                receipt = GateReceipt(**base, status=status, reason=reason, returncode=returncode,
                                      output=_kept(output, trail=_cleanup_note(cleanup),
                                                   favour_stderr=_reports_on_stderr(named, status == 'passed')),
                                      tests=count, report=report, cases=case_record(output, named))
            except _GateTimeout as exc:
                # Still an error, never a product failure: what was printed
                # before the deadline is kept so the hang can be diagnosed.
                receipt = GateReceipt(**base, status='error', reason=f'timed out after {command.timeout}s',
                                      output=_kept(exc.output, lead=_cleanup_note(exc.cleanup),
                                                   favour_stderr=_reports_on_stderr(runner_of(command.argv), False)))
            except (OSError, ValueError) as exc:
                receipt = GateReceipt(**base, status='blocked', reason=f'runner unavailable: {exc}')
            try:
                after = self._digests()
            except Exception as exc:  # noqa: BLE001 -- unreadable source is an error receipt
                stopped = 'source could not be read after a prior gate'
                receipt = replace(receipt, status='error', reason='source could not be read after the command: '
                                                                  f'{type(exc).__name__}: {str(exc)[:160]}')
                self._cache.clear()
            else:
                if after != before:
                    stopped = 'source changed during a prior gate'
                    # The paths, so the operator can see which file the check
                    # wrote (checks 7: "source changed during gate" alone).
                    receipt = replace(receipt, status='error',
                                      reason='source changed during gate: ' + _changed_paths(before, after))
                    self._cache.clear()
            if command.cacheable and receipt.status == 'passed' and receipt.runner_hash:
                self._cache[key] = receipt
            receipts.append(receipt)
        receipts = _within_name_bound(receipts)
        passed = not stopped and all(r.status == 'passed' for r in receipts if r.required)
        output = '\n'.join(f'{r.id}: {r.status}: {r.reason}\n{r.output}' for r in receipts)
        return GateResult(passed, 'gate suite', 0 if passed else 1, output, tuple(receipts))


#: How many changed paths a receipt names, and how much of each.
_CHANGED_PATHS, _CHANGED_PATH_CHARS = 6, 120


def _changed_paths(before: dict, after: dict) -> str:
    """The files that differ between two source listings, by what happened
    to each, bounded: "added .hypothesis/db; changed app.py (+3 more)"."""
    groups = (('added', sorted(after.keys() - before.keys())),
              ('changed', sorted(n for n in before.keys() & after.keys() if before[n] != after[n])),
              ('removed', sorted(before.keys() - after.keys())))
    total = sum(len(names) for _, names in groups)
    shown, left = [], _CHANGED_PATHS

    def plain(name: str) -> str:
        # The name is whatever the check wrote: a line break or a control
        # character in it must not put a second line into a receipt's
        # reason, which is shown to seats as the harness's own record.
        return ''.join(c if c.isprintable() else '?' for c in name[:_CHANGED_PATH_CHARS])
    for label, names in groups:
        take = names[:left]
        left -= len(take)
        if take:
            shown.append(label + ' ' + ', '.join(plain(n) for n in take))
    listed = _CHANGED_PATHS - left
    return '; '.join(shown) + (f' (+{total - listed} more)' if total > listed else '')


def _selection(argv, cwd, root) -> str:
    """:func:`config_selection`, total: a selection that cannot be read is
    not established, which only ever withholds a "whole file ran" reading."""
    try:
        return config_selection(argv, cwd, root)
    except Exception:  # noqa: BLE001 -- unknown selection is a filter, never a crash
        return 'test selection could not be read'


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
    for arg in _runner_args(argv):
        arg = str(arg)
        if arg.startswith('-') or '::' in arg:
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
    if runner_of(argv) is None:
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
    found = _summary_outcomes(_summary_text(output))
    return sum(int(n) for n, kind in found if kind == 'deselected') if found else 0


# Case lines are read by three small left-to-right parsers. The patterns
# they replace matched the name lazily before optional trailing parts, and
# one output line of 20,000 spaces took over a second to reject: quadratic,
# and run after the command's own timeout had ended (review 2026-11-10,
# checks 9). Each parser reads a line once; a line over _MAX_SCAN_LINE is
# never a case line.
_WS = re.compile(r'\s*')
_NONWS = re.compile(r'\S+')
_SPEC_MARKS = ('\u2714', '\u2716', '\ufe63')
_SPEC_DURATION = re.compile(r'\([\d.]+m?s\)')
_TAP_HEAD = re.compile(r'\s*(?:not ok|ok)\s+\d+\s+-\s+', re.I)
_TAP_DIRECTIVE = re.compile(r'(SKIP|TODO)\b', re.I)
_PYTEST_OUTCOME = re.compile(r'(PASSED|FAILED|SKIPPED|XFAIL|XPASS|ERROR)\b')


def _spec_case(line: str):
    """Node's spec reporter: ``<mark> name (1.2ms)``, then nothing or
    ``# directive``. The name ends at the first duration, after some
    whitespace, that is followed by one of those; a duration standing where
    the name would start (an empty name) is read only when no later one
    fits. ``(name, kind)`` or None."""
    body = line.lstrip()
    if not body or body[0] not in _SPEC_MARKS:
        return None
    after_mark = len(line) - len(body) + 1
    start = _WS.match(line, after_mark).end()
    if start == after_mark:
        return None

    def ends_at(at):
        """The directive after the duration at ``at``, or None when what
        follows is neither nothing nor a directive."""
        duration = _SPEC_DURATION.match(line, at)
        if not duration:
            return None
        rest = _WS.match(line, duration.end()).end()
        if rest == len(line):
            return ''
        return line[rest + 1:].strip() if rest > duration.end() and line[rest] == '#' else None

    found = None
    at = line.find('(', start + 1)
    while at >= 0 and found is None:
        if line[at - 1].isspace():
            directive = ends_at(at)
            if directive is not None:
                found = line[start:at].rstrip(), directive
        at = line.find('(', at + 1)
    if found is None and start - after_mark >= 2 and line.startswith('(', start):
        directive = ends_at(start)
        if directive is not None:
            found = '', directive
    if found is None:
        return None
    kind = ('other' if found[1].upper().startswith('TODO')
            else 'skipped' if body[0] == '\ufe63' else 'executed')
    return found[0], kind


def _tap_case(line: str):
    """TAP: ``ok N - name``, then nothing or ``# SKIP`` / ``# TODO``. The
    name ends at the first such directive. ``(name, kind)`` or None."""
    head = _TAP_HEAD.match(line)
    if not head:
        return None
    start = head.end()
    at = line.find('#', start + 1)
    while at >= 0:
        # "\s+#": whitespace of the name's own before it, so never a "#"
        # standing where the name starts.
        if line[at - 1].isspace():
            # Matched as the pattern this replaces matched it, case rules
            # included: a directive that is not exactly SKIP once upper-cased
            # (TODO, or a spelling only case folding equates) is "other".
            directive = _TAP_DIRECTIVE.match(line, _WS.match(line, at + 1).end())
            if directive:
                return line[start:at].rstrip(), ('skipped' if directive.group(1).upper() == 'SKIP' else 'other')
        at = line.find('#', at + 1)
    return line[start:].rstrip(), 'executed'


def _pytest_case(line: str):
    """Pytest's verbose line: ``path::name OUTCOME`` from the first column.
    ``(node id, kind)`` or None."""
    token = _NONWS.match(line)
    if not token:
        return None
    nodeid = token.group()
    sep = nodeid.find('::', 1)
    if sep < 0 or sep + 2 >= len(nodeid):
        return None
    rest = _WS.match(line, token.end()).end()
    outcome = _PYTEST_OUTCOME.match(line, rest) if rest > token.end() else None
    if not outcome:
        return None
    return nodeid, {'SKIPPED': 'skipped', 'XFAIL': 'other'}.get(outcome.group(1), 'executed')


_CASE_PARSERS = (('spec', _spec_case), ('tap', _tap_case), ('pytest', _pytest_case))
#: Which line families a command's runner prints.
_RUNNER_FAMILIES = {'pytest': ('pytest',), 'node': ('spec', 'tap'), None: ('spec', 'tap', 'pytest')}


def _parse_case(line: str):
    """``(family, name, kind)`` when ``line`` is a case line of some runner,
    else None. The three forms cannot overlap (a mark, ``ok N -`` or a
    ``path::name`` token starts the line), so the family is the line's own
    and the runner only decides which families count."""
    if len(line) > _MAX_SCAN_LINE:
        return None
    for family, parse in _CASE_PARSERS:
        found = parse(line)
        if found:
            return family, found[0], found[1]
    return None


def case_key(name: str) -> str:
    """A case's identity: its full name, or for a name over 300 characters a
    readable prefix plus the SHA256 of the whole name, so two distinct long
    names never share a key (Codex review of d157378, T1)."""
    if len(name) <= 300:
        return name
    return f"{name[:200]}\u2026sha256:{hashlib.sha256(name.encode('utf-8', 'surrogatepass')).hexdigest()}"


def runner_of(argv) -> Optional[str]:
    """Which test runner a command starts: 'pytest', 'node' (``node
    --test``) or None when neither can be told from its argv."""
    argv = [str(a) for a in argv]
    if _is_pytest(argv):
        return 'pytest'
    if argv and Path(argv[0]).name.startswith('node') and '--test' in argv:
        return 'node'
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
    families = _RUNNER_FAMILIES.get(runner, _RUNNER_FAMILIES[None])
    for line in (output or '').splitlines():
        parsed = _parse_case(line)
        if parsed and parsed[0] in families:
            yield parsed[1], parsed[2], runner == 'pytest' and parsed[0] == 'pytest'


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


class _CaseScan:
    """Case lines read one at a time, whatever runner printed them, so the
    record for the command's own runner can be built afterwards without the
    output being kept. Bounded: a family past ``_MAX_CASES`` lines stores
    nothing more and its record is None."""

    def __init__(self):
        self.seen = {family: 0 for family, _ in _CASE_PARSERS}
        self.counts = {family: dict(skipped={}, executed={}, other={}) for family, _ in _CASE_PARSERS}
        self.broken = False

    def line(self, text: str) -> None:
        try:
            parsed = _parse_case(text)
        except Exception:  # noqa: BLE001 -- never stop the thread draining the command's pipe
            self.broken = True      # a line that could not be read: the record is unknown
            return
        if parsed is None:
            return
        family, name, kind = parsed
        self.seen[family] += 1
        if self.seen[family] > _MAX_CASES:
            return
        key = case_key(name)
        kinds = self.counts[family][kind]
        kinds[key] = kinds.get(key, 0) + 1

    def record(self, runner: Optional[str] = None) -> Optional[dict]:
        families = _RUNNER_FAMILIES.get(runner, _RUNNER_FAMILIES[None])
        if self.broken or sum(self.seen[family] for family in families) > _MAX_CASES:
            return None
        record = dict(skipped={}, executed={}, other={})
        for family in families:
            for kind, counts in self.counts[family].items():
                for key, count in counts.items():
                    record[kind][key] = record[kind].get(key, 0) + count
        # Keys a pytest verbose line printed: only these carry their file (V1).
        record['qualified'] = sorted({key for counts in self.counts['pytest'].values() for key in counts}
                                     if runner == 'pytest' else ())
        return record


def case_record(output: str, runner: Optional[str] = None) -> Optional[dict]:
    """Every case the whole output prints, by kind (skipped, executed,
    other), as ``case_key``-to-count maps; None past ``_MAX_CASES`` lines.
    None is unknown identity, which callers never rebuild from the output
    tail (Codex review of d157378, T2). A gate command's output carries the
    lines its stdout printed, read as they streamed, so the record covers
    the whole output even though only a head and a tail of it are kept."""
    scan = getattr(output, 'cases', None)
    if scan is None:
        scan = _CaseScan()
        for line in (output or '').splitlines():
            scan.line(line)
    return scan.record(runner)


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


def _test_change_path(value, root, forbid, *, existing=False):
    from .scope import _matches
    if (not isinstance(value, str) or not value or value != value.strip()
            or value.startswith(('/', '~')) or any(c in value for c in '\\*?[]\0\n\r\t')
            or any(p in ('', '.', '..') for p in value.split('/')) or _test_family(value) is None):
        raise ValueError('Test changes must name one exact project-relative test file')
    if any(_matches(value, pattern) for pattern in forbid or ()):
        raise ValueError(f'Test changes name a path the operator forbade: {value}')
    current = Path(root).resolve()
    for part in PurePosixPath(value).parts:
        current = current / part
        if current.is_symlink():
            raise ValueError(f'Test changes cannot go through a symlink: {value}')
    if not current.resolve().is_relative_to(Path(root).resolve()):
        raise ValueError(f'Test changes name a path outside the project: {value}')
    if existing and not current.is_file():
        raise ValueError(f'Test changes name no existing test file: {value}')
    return value


def test_change_authorizations(raw, *, goal, root, forbid=(), originals=None):
    """Validate operator-authored migrations against exact run-start tests.

    ``originals`` is the harness's captured baseline for approval requested
    after editing. Without it the current project is the run-start baseline.
    A missing current digest authorizes a plan, never its eventual bytes.
    """
    if raw is None or raw == () or raw == []:
        return ()
    if (not isinstance(raw, dict) or set(raw) != {'schema_version', 'goal', 'changes'}
            or type(raw['schema_version']) is not int or raw['schema_version'] != 1):
        raise ValueError('Test changes must contain schema_version 1, goal and changes')
    if not isinstance(raw['goal'], str) or not raw['goal'].strip() or raw['goal'] != goal:
        raise ValueError('Test changes goal does not match this run')
    if not isinstance(raw['changes'], list) or not raw['changes']:
        raise ValueError('Test changes must name at least one exact test migration')
    records, seen = [], set()
    for change in raw['changes']:
        fields = {'path', 'original_sha256', 'reason', 'cases'}
        if (not isinstance(change, dict) or not fields <= set(change)
                or set(change) - fields - {'current_sha256'}):
            raise ValueError('Each test change must name path, original_sha256, reason and cases')
        name = _test_change_path(change['path'], root, forbid, existing=True)
        if name in seen:
            raise ValueError(f'Test changes repeat a file: {name}')
        seen.add(name)
        data = originals.get(name) if originals is not None else (Path(root) / name).read_bytes()
        if not isinstance(data, bytes):
            raise ValueError(f'Test changes name no run-start test file: {name}')
        digest = change['original_sha256']
        if (not isinstance(digest, str) or not re.fullmatch(r'[0-9a-f]{64}', digest)
                or hashlib.sha256(data).hexdigest() != digest):
            raise ValueError(f'Test changes original_sha256 does not match run-start bytes: {name}')
        current_digest = change.get('current_sha256')
        if current_digest is not None and (
                not isinstance(current_digest, str) or not re.fullmatch(r'[0-9a-f]{64}', current_digest)):
            raise ValueError(f'Test changes current_sha256 must be a SHA-256 digest: {name}')
        reason = change['reason']
        if not isinstance(reason, str) or not reason.strip():
            raise ValueError(f'Test changes need an operator reason: {name}')
        known = declared_cases(name, data)
        if not known or len(known) != len(set(known)):
            raise ValueError(f'Test changes need unambiguous literal run-start case names: {name}')
        if not isinstance(change['cases'], list) or not change['cases']:
            raise ValueError(f'Test changes must name the cases being replaced or retired: {name}')
        cases, named = [], set()
        for case in change['cases']:
            if (not isinstance(case, dict) or set(case) != {'case', 'action', 'replacements'}
                    or not isinstance(case['case'], str) or case['case'] not in known
                    or case['case'] in named or case['action'] not in ('replace', 'retire')
                    or not isinstance(case['replacements'], list)):
                raise ValueError(f'Test changes name an invalid or repeated original case: {name}')
            named.add(case['case'])
            if bool(case['replacements']) != (case['action'] == 'replace'):
                raise ValueError('A replacement needs replacement cases; a retirement must name none')
            replacements, targets = [], set()
            for target in case['replacements']:
                if (not isinstance(target, dict) or set(target) != {'path', 'case'}
                        or not isinstance(target['case'], str) or not target['case'].strip()
                        or any(c in target['case'] for c in '\0\n\r')):
                    raise ValueError('A replacement must name an exact test path and literal case')
                path = _test_change_path(target['path'], root, forbid)
                identity = (path, target['case'])
                if identity in targets:
                    raise ValueError('Test changes repeat a replacement case')
                targets.add(identity)
                replacements.append(dict(path=path, case=target['case']))
            cases.append(dict(case=case['case'], action=case['action'], replacements=replacements))
        records.append(dict(goal=goal, path=name, original_sha256=digest, current_sha256=current_digest,
                            reason=reason.strip(), cases=cases))
    return tuple(records)


#: How much one gate run's receipts keep of the names of passed cases, in
#: characters. The producer bounds one report; this bounds a run of many
#: commands, so the one check record that keeps the names stays a known
#: size (the monitor reads result.json only up to its own bound).
_MAX_RUN_NAME_CHARS = 1_500_000


def without_names(report):
    """``report`` without the names of its passed cases, for a record no
    reader of those names uses; ``passed_cases_omitted`` says how many were
    left out. A report that names none is returned as it is.

    A quiet run's report names every pass (``read_report``), and every copy
    of a receipt carried the whole list: a 5,000-case suite put 480,000
    bytes into result.json for each check, twice, and nine checks took the
    file past what the monitor reads (final review 2026-11-11, gate 4). The
    names are read from the run's newest check only (the test-repair and
    test-change rules, ``tools/truth_check.py``), so that record keeps them
    and every other one keeps the counts."""
    if not isinstance(report, dict) or not isinstance(report.get('passed_cases'), list):
        return report
    slim = {key: value for key, value in report.items() if key != 'passed_cases'}
    slim['passed_cases_omitted'] = len(report['passed_cases'])
    return slim


def _within_name_bound(receipts: list) -> list:
    """``receipts`` with the names of passed cases kept only up to
    ``_MAX_RUN_NAME_CHARS`` in all. A report past it keeps its counts and is
    marked ``passed_truncated``, which every reader of names takes as "not
    every pass is named": stricter, never a pass that was not shown."""
    used, out = 0, []
    for receipt in receipts:
        names = receipt.report.get('passed_cases') if isinstance(receipt.report, dict) else None
        if isinstance(names, list):
            used += sum(len(name) + 4 for name in names if isinstance(name, str))
            if used > _MAX_RUN_NAME_CHARS:
                receipt = replace(receipt, report=dict(without_names(receipt.report), passed_truncated=True))
        out.append(receipt)
    return out


def _named_passes(report) -> set:
    """The cases a harness-validated report names as passed, when it names
    every one; empty otherwise.

    Quiet pytest output (``-q``) has no verbose case lines; the report the
    producer writes still names every pass exactly. An incomplete, failed or
    skipped report cannot establish the full execution of a case (all its
    parameter instances included), so it names nothing here."""
    if not isinstance(report, dict):
        return set()
    counts, passed = report.get('counts'), report.get('passed_cases')
    if (report.get('state') == 'parsed' and type(report.get('exitstatus')) is int
            and report['exitstatus'] == 0 and report.get('passed_truncated') is False
            and report.get('truncated') is False and report.get('collect_errors') == 0
            and isinstance(counts, dict) and all(type(counts.get(k)) is int and counts[k] == 0
                                               for k in ('failed', 'errors', 'skipped'))
            and isinstance(passed, list) and all(isinstance(k, str) and k for k in passed)
            and type(counts.get('passed')) is int and counts['passed'] == len(passed)
            and len(set(passed)) == len(passed)):
        return set(passed)
    return set()


def _python_test_bodies(data):
    import ast
    try:
        tree = ast.parse(data.decode('utf-8'))
    except (UnicodeError, SyntaxError):
        return None
    return {node.name: ast.dump(node, include_attributes=False) for node in ast.walk(tree)
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name.startswith('test_')}


def test_change_problems(record, original, current, receipts):
    """Whether approved case migrations preserve and execute all owed tests.

    This checks evidence, not approval provenance. The caller must separately
    establish operator approval of the exact resulting file bytes.
    """
    name = record['path']
    data = current.get(name)
    if data is None:
        return [f'{name}: whole test-file deletion is not authorized; retire exact cases inside the file']
    if hashlib.sha256(original).hexdigest() != record['original_sha256']:
        return [f'{name}: approval does not bind these run-start test bytes']
    if record.get('current_sha256') and hashlib.sha256(data).hexdigest() != record['current_sha256']:
        return [f'{name}: approval does not bind these resulting test bytes']
    old, now = declared_cases(name, original) or [], declared_cases(name, data) or []
    if not old or len(now) != len(set(now)):
        return [f'{name}: literal test-case identities cannot be established']
    changes = {c['case']: c for c in record['cases']}
    problems, wanted = [], set()
    before_bodies = _python_test_bodies(original) if _test_family(name)[1] == 'py' else None
    after_bodies = _python_test_bodies(data) if before_bodies is not None else None
    for case in old:
        change = changes.get(case)
        if change is None:
            if case not in now:
                problems.append(f'{name}: unapproved original case "{case}" was removed')
            elif before_bodies is not None and (
                    after_bodies is None or before_bodies.get(case) != after_bodies.get(case)):
                problems.append(f'{name}: unapproved original case "{case}" changed')
            wanted.add((name, case))
        elif change['action'] == 'retire':
            if case in now:
                problems.append(f'{name}: retired original case "{case}" is still declared')
        else:
            wanted.update((r['path'], r['case']) for r in change['replacements'])
    executed, skipped, failed = set(), set(), set()
    for receipt in receipts:
        get = ((lambda k, r=receipt: r.get(k)) if isinstance(receipt, dict)
               else (lambda k, r=receipt: getattr(r, k, None)))
        cases = get('cases') or {}
        if not isinstance(cases, dict):
            cases = {}
        for key, pool in (('skipped', skipped), ('failed', failed), ('executed', executed)):
            if key == 'executed' and (get('status') != 'passed' or get('required') is False):
                continue
            counts = cases.get(key)
            if isinstance(counts, dict):
                pool.update(k for k, count in counts.items()
                            if isinstance(k, str) and type(count) is int and count > 0)
        if get('status') == 'passed' and get('required') is not False:
            executed.update(_named_passes(get('report')))
    for path, case in sorted(wanted):
        declared = declared_cases(path, current.get(path, b'')) or []
        if declared.count(case) != 1:
            problems.append(f'{path}: replacement or retained case "{case}" is not declared exactly once')
            continue
        unqualified = any(case in (declared_cases(other, body) or []) for other, body in current.items()
                          if other != path and _test_family(other) is not None)
        py = _test_family(path)[1] == 'py'

        def seen(pool, path=path, case=case, py=py, unqualified=unqualified):
            return any((key == case and not unqualified) or (
                py and key.split('::')[0] == path and key.split('::')[-1].split('[', 1)[0] == case)
                       for key in pool)

        if not py and unqualified:
            problems.append(f'{path}: case "{case}" has an ambiguous name in another test file')
        elif seen(skipped) or seen(failed):
            problems.append(f'{path}: case "{case}" was skipped or failed')
        elif not seen(executed):
            problems.append(f'{path}: case "{case}" is not shown executing in a passed required check')
    return problems



test_change_authorizations.__test__ = False
test_change_problems.__test__ = False


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
            # A quiet run prints no case lines; its report names the passes.
            executed |= _named_passes(get('report'))

    def seen(case, pool):
        # A parametrized case prints one id per instance, ``name[param]``:
        # the case is the part before the bracket, as test_change_problems
        # reads it (review 2026-11-10, checks 14).
        return any(k == case or (py and k.split('::')[0] == name
                                 and k.split('::')[-1].split('[', 1)[0] == case) for k in pool)
    for case in wanted:
        if seen(case, skipped):
            problems.append(f'{name}: case "{case}" was skipped')
        elif not seen(case, executed):
            problems.append(f'{name}: case "{case}" is not shown executing in a passed required check')
    return problems


def _place_withheld(root: Path, copy: Path, exclude=()) -> tuple:
    """Put the project's withheld files (``project.withheld_files``: the
    ``.env*``, credential and vendor agent files no model copy holds) into
    ``copy``, for a run of the gate's own commands there. Returns ``(files
    placed, folders made for them)``, so a caller whose copy lives on can
    take exactly those out again (:func:`_remove_placed`).

    A file the copy already has is left as it is. Nothing is written
    through a link: a copy a model edited can hold one, and a file placed
    behind it would leave the copy. One that cannot be placed is left out,
    as all of them were before: the check then says what it says without
    it."""
    from .project import withheld_files
    placed, made = [], []
    for name, data in withheld_files(root, exclude).items():
        target = copy / name
        try:
            folder, missing = copy, []
            for part in Path(name).parts[:-1]:
                folder = folder / part
                if folder.is_symlink() or (folder.exists() and not folder.is_dir()):
                    raise OSError('not a folder of the copy')
                if not folder.exists():
                    missing.append(folder)
            if target.exists() or target.is_symlink():
                continue
            for folder in missing:
                folder.mkdir()
                made.append(folder)
            # Owner-only from the first byte; the project's own mode after.
            descriptor = os.open(target, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
            placed.append(target)
            with os.fdopen(descriptor, 'wb') as stream:
                stream.write(data)
            shutil.copymode(root / name, target)
        except OSError:
            continue
    return placed, made


def _remove_placed(placed, made) -> None:
    """Take out what :func:`_place_withheld` put in. Raises RuntimeError
    naming a file that is still there: a copy that keeps one is not a copy
    anything else may read."""
    left = []
    for target in placed:
        try:
            if target.is_dir() and not target.is_symlink():
                shutil.rmtree(target)       # a check replaced the file with a folder
            else:
                target.unlink(missing_ok=True)
        except OSError:
            try:
                os.chmod(target.parent, 0o700)      # a check may have closed the folder
                target.unlink(missing_ok=True)
            except OSError:
                left.append(target.name)
    for folder in reversed(made):
        try:
            folder.rmdir()
        except OSError:
            pass    # a check left something of its own in it
    if left:
        raise RuntimeError(f"withheld file(s) could not be removed from the copy after its check: {', '.join(left)}")


def run_original_tests(gate, root, originals: dict, current: dict, keep=(), *, exclude=()):
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

    ``current`` is the measured source, which holds no ``.env*`` file, no
    credential file and no vendor agent configuration: those are withheld
    from every copy a model reads. This copy is read by no model, only by
    the gate's own commands, and the same commands see those files where
    they run in the project. So they are copied in from the project as they
    are (``project.withheld_files``; ``exclude`` is the operator's excluded
    paths): a suite that reads a fixture named ``.npmrc`` failed here and
    passed in the project, a failure that said nothing about the source.

    Returns ``(result, problem)``: a :class:`GateResult` whose receipts are
    named ``original-tests:<id>``, or ``None`` and why nothing was checked.
    """
    keep = {k for k in keep if k in originals and k in current}
    root = Path(root).resolve()
    commands = _gate_commands(gate, root)
    if commands is None:
        return None, 'the check commands could not be read for an original-test run'
    # Under the private scratch root, as every other copy the harness makes
    # is, and not the system temp folder: this copy holds the project's
    # withheld files, and a vendor sandbox may write under /tmp, where a file
    # dropped into the copy would run with these tests. A scratch root that
    # cannot be used means the original tests were not established.
    from .providers import ProviderError, scratch_root
    try:
        scratch = scratch_root()
    except ProviderError as exc:
        return None, f'the private scratch folder could not be used for an original-test run ({str(exc)[:300]})'
    with tempfile.TemporaryDirectory(prefix='quadratus-original-tests-', dir=scratch) as directory:
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
        # This copy is deleted with its folder, so nothing is taken out again.
        _place_withheld(root, copy, exclude)
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


@contextmanager
def gate_in_copy(gate, root, copy, *, exclude=()):
    """The gate's own commands bound to ``copy``, a private copy of the
    project at ``root``: the same argv, cwd, timeouts and minimums, with a
    path that names the project re-pointed at the copy. Yields a
    :class:`GateSuite`, or None when the commands cannot be read (a gate
    other than the two kinds here, or one outside the project).

    A copy carries source only, so the project's dependency folders are
    linked in for the run, as :func:`run_original_tests` does, and unlinked
    after it. The project's withheld files are placed for the run as they
    are there (``exclude`` is the operator's excluded paths) and taken out
    after it: a suite that reads ``.env`` failed in both drafts of a
    contest and passed in the project, so the drafts were ranked on noise
    (final review 2026-11-11, gate 3). They are in the copy only while the
    gate's commands run; a copy a model reads never holds them. Used to
    run a task's gate on a contest draft as evidence (quadratus.contest);
    nothing is written to the project."""
    root, copy = Path(root).resolve(), Path(copy).resolve()
    commands = _gate_commands(gate, root)
    if commands is None:
        yield None
        return
    linked, placed, made = [], [], []
    try:
        for name in _DEPENDENCY_DIRS:
            source, target = root / name, copy / name
            if source.is_dir() and not source.is_symlink() and not target.exists() and not target.is_symlink():
                target.symlink_to(source, target_is_directory=True)
                linked.append(target)
        placed, made = _place_withheld(root, copy, exclude)
        inside = re.compile(re.escape(str(root)) + r'(?=/|$)')
        yield GateSuite([replace(c, argv=tuple(inside.sub(str(copy), a) for a in c.argv)) for c in commands],
                        cwd=copy)
    finally:
        for target in linked:
            try:
                target.unlink()
            except OSError:
                pass
        _remove_placed(placed, made)


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
    if report.get('state') == 'parsed':
        return int(report['counts']['skipped'])
    output = _summary_text(getattr(receipt, 'output', '') or '')
    node = re.findall(r'(?m)^(?:#|\u2139)\s*(?:skipped|skip) (\d+)\s*$', output)
    if node:
        return int(node[-1])
    found = _summary_outcomes(output)
    # A summary of expected failures alone ("1 xfailed") says nothing of
    # skips either way, as before: unknown, never zero.
    if found and any(kind not in ('xfailed', 'xpassed') for _, kind in found):
        return sum(int(n) for n, kind in found if kind == 'skipped')
    unittest = _UNITTEST_SKIPPED.findall(output)
    return int(unittest[-1]) if unittest else None

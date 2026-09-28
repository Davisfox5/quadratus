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
from pathlib import Path
from typing import Literal, Optional, Sequence

__all__ = ["GateResult", "IntegrationGate", "GateCommand", "GateReceipt", "GateSuite", "REPORT_TOKEN",
           "CheckUnattributable", "attribute", "read_report"]

#: The argv token a check uses to declare the harness's structured report.
#: The only supported producer is the harness-owned pytest plugin
#: (``_gate_producer``), declared as ``--quadratus-report={report}``. The
#: harness replaces the token with a path it owns, outside the project, loads
#: the plugin for that one invocation, and removes the path after reading it
#: (phase 3, #25). Other runners have no producer: their failures are never
#: attributable, so never repaired.
REPORT_TOKEN = "{report}"
PRODUCER = "quadratus-pytest/3"
_PRODUCER_FILE = Path(__file__).resolve().parent / "_gate_producer" / "quadratus_gate_report.py"
#: Kept for the install-layout proof: where the shipped producer lives.
_PRODUCER_DIR = str(_PRODUCER_FILE.parent)
_REPORT_FAILURES = 200
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
    module = f"quadratus_gate_report_{secrets.token_hex(8)}"
    source = _PRODUCER_FILE.read_bytes()
    copy = os.path.join(owned, module + ".py")
    with open(copy, "wb") as out:
        out.write(source)
    expected = dict(nonce=nonce, module_file=os.path.realpath(copy),
                    module_sha256=hashlib.sha256(source).hexdigest())
    path = os.path.join(owned, "report.json")
    plugins = [p for p in env.get("PYTEST_PLUGINS", "").split(",") if p.strip()]
    env = dict(env, QUADRATUS_GATE_NONCE=nonce, PYTEST_PLUGINS=",".join(plugins + [module]),
               PYTHONPATH=os.pathsep.join([owned] + [p for p in [env.get("PYTHONPATH")] if p]))
    try:
        yield [a.replace(REPORT_TOKEN, path) for a in argv], env, path, expected
    finally:
        shutil.rmtree(owned, ignore_errors=True)


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
    if (data.get("producer") != PRODUCER or data.get("nonce") != expected.get("nonce")
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
                               assertion=f["assertion"]) for f in data["failures"]],
                truncated=data["truncated"])


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
    other = sorted({f["exc_type"] for f in failures if f["when"] != "call" or not f["assertion"]})
    if other:
        found.append(f"{gid}: failures that are not assertions: {', '.join(other)[:200]}")
    return found


def attribute(result) -> dict:
    """Whether a failed gate is a product failure a repair may address.

    Only facts decide, never prose, counts alone or message wording: every
    failed required check exited 1 on its own, declared the harness report,
    and that report (this invocation's, by nonce) shows only test-body
    failures raised as a plain ``AssertionError``, with no collection, setup
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
                proc = subprocess.run(
                    argv,
                    capture_output=True,
                    text=True,
                    timeout=self.timeout,
                    cwd=self.cwd,
                    check=False,
                    env=env,
                )
                report = read_report(path, expected)
        except subprocess.TimeoutExpired:
            return GateResult(
                passed=False, command=shown, returncode=None,
                output=f"(timed out after {self.timeout}s)",
            )
        except FileNotFoundError as exc:
            return GateResult(
                passed=False, command=shown, returncode=None,
                output=f"(command not found: {exc})",
            )
        combined = ((proc.stdout or "") + "\n" + (proc.stderr or "")).strip()
        passed = proc.returncode == 0
        if passed and self.minimum_tests is not None:
            count = _test_count(combined)
            if count is None:
                passed, combined = False, "(test count unavailable)\n" + combined
            elif count < self.minimum_tests:
                passed, combined = False, f"(zero tests executed: {count} ran)\n" + combined
        return GateResult(
            passed=passed,
            command=shown,
            returncode=proc.returncode,
            output=combined[-_TAIL_CHARS:],
            report=report,
        )


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

    def run(self) -> GateResult:
        receipts = []
        source = self.project.fingerprint()
        changed = False
        for command in self.commands:
            config = hashlib.sha256(json.dumps(asdict(command), sort_keys=True).encode()).hexdigest()
            base = dict(id=command.id, required=command.required, command=shlex.join(command.argv),
                        source_hash=source, config_hash=config)
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
                with _fresh_bytecode_env() as fresh, _report_slot(command.argv, fresh) as (argv, env, path, expected):
                    proc = subprocess.run(argv, cwd=cwd, capture_output=True, text=True,
                                          timeout=command.timeout, check=False, env=env)
                    report = read_report(path, expected)
                output = ((proc.stdout or '') + '\n' + (proc.stderr or '')).strip()
                count = _test_count(output)
                status, reason = ('passed', 'exit 0') if proc.returncode == 0 else ('failed', 'nonzero exit')
                if count == 0:
                    status, reason = 'failed', 'zero tests executed'
                elif proc.returncode == 0 and command.minimum_tests is not None:
                    if count is None:
                        status, reason = 'blocked', 'test count unavailable'
                    elif count < command.minimum_tests:
                        status, reason = 'failed', 'fewer tests than required'
                receipt = GateReceipt(**base, status=status, reason=reason,
                                      returncode=proc.returncode, output=output[-_TAIL_CHARS:], tests=count,
                                      report=report)
            except subprocess.TimeoutExpired:
                receipt = GateReceipt(**base, status='error', reason=f'timed out after {command.timeout}s')
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

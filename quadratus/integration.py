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
import re
import shutil
import subprocess
from dataclasses import asdict, dataclass, replace
from pathlib import Path
from typing import Literal, Optional, Sequence

__all__ = ["GateResult", "IntegrationGate", "GateCommand", "GateReceipt", "GateSuite"]

#: How much command output a failed gate carries back. The tail, because test
#: runners put the summary and the first failures at the end.
_TAIL_CHARS = 2_000


@dataclass(frozen=True)
class GateResult:
    """One gate run. Facts only."""

    passed: bool
    command: str
    returncode: Optional[int]
    #: The tail of combined stdout/stderr -- what a person would read first.
    output: str
    receipts: tuple[GateReceipt, ...] = ()

    def render(self) -> str:
        status = "PASSED" if self.passed else "FAILED"
        lines = [f"Integration gate {status}: `{self.command}`"]
        if not self.passed and self.output:
            lines.append(self.output)
        return "\n".join(lines)


class IntegrationGate:
    """Runs the project's own check command and reports the outcome.

    Args:
        command: The check, as an argument list (never a shell string).
            Typically the project's test runner or build.
        cwd: Where to run it -- the project checkout.
        timeout: Seconds before the run counts as failed. A hung test suite
            must not hang the whole session.
    """

    def __init__(self, command: Sequence[str], *, cwd=None, timeout: int = 600):
        if not command:
            raise ValueError("the gate needs a command to run")
        self.command = list(command)
        self.cwd = cwd
        self.timeout = timeout

    def run(self) -> GateResult:
        shown = " ".join(self.command)
        try:
            proc = subprocess.run(
                self.command,
                capture_output=True,
                text=True,
                timeout=self.timeout,
                cwd=self.cwd,
                check=False,
            )
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
        return GateResult(
            passed=proc.returncode == 0,
            command=shown,
            returncode=proc.returncode,
            output=combined[-_TAIL_CHARS:],
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


def _test_count(output):
    """Recognize runner summaries only; absent evidence is not zero or success."""
    if re.search(r'\bno tests (?:ran|collected|found)\b', output, re.I):
        return 0
    tap = re.findall(r'(?m)^# tests (\d+)\s*$', output)
    if tap:
        skipped = re.findall(r'(?m)^# (?:skipped|skip|todo) (\d+)\s*$', output)
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
            base = dict(id=command.id, required=command.required, command=' '.join(command.argv),
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
                proc = subprocess.run(command.argv, cwd=cwd, capture_output=True, text=True,
                                      timeout=command.timeout, check=False)
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
                                      returncode=proc.returncode, output=output[-_TAIL_CHARS:], tests=count)
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

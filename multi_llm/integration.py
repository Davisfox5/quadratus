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

import subprocess
from dataclasses import dataclass
from typing import Optional, Sequence

__all__ = ["GateResult", "IntegrationGate"]

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

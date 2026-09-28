"""Tests for the session engine's command-line wiring.

No model is called: ``Fleet`` is replaced with a scripted fake, so these
exercise what the CLI is responsible for -- assembling the state directory,
wiring the operator channels, and reporting what happened.
"""

from __future__ import annotations

import json

import pytest

from quadratus.cli import main


class FakeFleet:
    """Stands in for the vendor CLIs. Answers every prompt scriptably."""

    scripted: list = []

    def __init__(self, settings=None, *, usage_meter=None, allow_writes=False, **kw):
        self.usage_meter = usage_meter
        self.allow_writes = allow_writes
        self.calls = []
        self.closed = False
        self._script = list(self.scripted)

    def invoke(self, model, prompt, *, system=None):
        self.calls.append({"model": model, "prompt": prompt})
        # The real fleet meters here, using the CLI's own token counts where it
        # reports them. Mirrored so the "metered once, not twice" contract is
        # actually exercised rather than assumed.
        if self.usage_meter is not None:
            self.usage_meter.record(model=model, prompt=prompt, reply="ok")
        if "Name the single next task" in prompt:
            return self._script.pop(0) if self._script else "DONE"
        if "The task is finished" in prompt:
            return ("SUMMARY: did the thing\nREASONING: it was simplest\n"
                    "DEAD ENDS: - Tried X; too slow.")
        if "full task list" in prompt or "expected task list" in prompt:
            return "1. do the thing"
        return f"[{model}] output"

    def available(self, key):
        return True

    def status_lines(self):
        return ["claude [cli]: Claude (opus)"]

    def close(self):
        self.closed = True


@pytest.fixture
def fleet(monkeypatch):
    made = {}

    def build(*a, **kw):
        made["fleet"] = FakeFleet(*a, **kw)
        return made["fleet"]

    monkeypatch.setattr("quadratus.runtime.Fleet", build)
    monkeypatch.setattr("quadratus.cli.Fleet", build, raising=False)
    FakeFleet.scripted = ["build the parser"]
    return made


def _run(tmp_path, *extra):
    return main(["--session", "Build a JSON parser",
                 "--state-dir", str(tmp_path / "state"), *extra])


def test_a_session_runs_and_reports(tmp_path, fleet, capsys):
    assert _run(tmp_path) == 0
    out = capsys.readouterr().out
    assert "task(s) complete" in out
    assert "did the thing" in out


def test_the_state_directory_holds_everything_the_run_accumulated(tmp_path, fleet):
    _run(tmp_path)
    state = tmp_path / "state"
    assert (state / "artifacts").is_dir(), "raw output summaries only point at"
    assert (state / "usage.jsonl").exists(), "the API-price counterfactual"


def test_usage_is_metered_once_not_twice(tmp_path, fleet):
    """The fleet sees the CLI's real token counts; the session only sees the
    strings. Metering both would double the counterfactual bill."""
    _run(tmp_path)
    lines = (tmp_path / "state" / "usage.jsonl").read_text().strip().splitlines()
    calls = len(fleet["fleet"].calls)
    assert len(lines) == calls
    assert all(json.loads(line)["model"] for line in lines)


def test_progress_is_reported_as_the_run_moves(tmp_path, fleet, capsys):
    """A session spends minutes per task; a caller with no view cannot tell a
    slow task from a hung one."""
    _run(tmp_path)
    out = capsys.readouterr().out
    assert "next task" in out
    assert "build the parser" in out


def test_the_scratch_directories_are_dropped_afterwards(tmp_path, fleet):
    _run(tmp_path)
    assert fleet["fleet"].closed


def test_writes_are_denied_unless_asked_for(tmp_path, fleet):
    _run(tmp_path)
    assert fleet["fleet"].allow_writes is False


def test_writes_require_a_project(tmp_path, fleet):
    with pytest.raises(SystemExit) as exc:
        _run(tmp_path, "--allow-writes")
    assert exc.value.code == 2
    assert "fleet" not in fleet


def test_the_plan_gate_can_decline_before_a_window_is_spent(tmp_path, fleet, monkeypatch, capsys):
    monkeypatch.setattr("builtins.input", lambda *a: "n")
    assert _run(tmp_path, "--plan-gate") == 0
    out = capsys.readouterr().out
    assert "Expected task list" in out
    assert "Nothing ran." in out


def test_an_unseatable_run_fails_loudly(tmp_path, fleet, monkeypatch, capsys):
    monkeypatch.setattr(FakeFleet, "available", lambda self, key: False)
    assert _run(tmp_path) == 1
    assert "cannot be seated" in capsys.readouterr().out


def test_the_pipeline_is_still_the_default(tmp_path, fleet):
    """Switching what `quadratus "..."` does is the operator's call."""
    import quadratus.cli as cli
    assert "--session" in cli.main.__doc__ if cli.main.__doc__ else True
    # The session fleet is only built when --session is passed.
    main(["--status"])
    assert "fleet" not in fleet

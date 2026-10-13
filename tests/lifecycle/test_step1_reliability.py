"""Step 1 (2026-10-13, #35): execution failures recognised at once, planner
and audit held to the configured checks, family guidance that agrees with
the task.

* Reservations pilot: the lead's every command failed ``sandbox_apply`` with
  status 71 before any file was read, and six more role calls followed.
* A call that hits the same environmental failure against the same source
  as the call before it cannot change anything by being repeated.
* The pilot plan prescribed ``python3 -m unittest discover -v`` while the
  configured runner was pytest, and audits kept requiring it.
* SQLite reservation work was given the default pure-logic card, which says
  the module performs no database access.

Whole-controller replays below the vendor CLI (tests.lifecycle.harness).
"""

import json
import uuid

import pytest

from quadratus.policy import load_policy
from quadratus.scope import TaskScope
from quadratus.session import _environment_signature
from tests.lifecycle import harness as H
from tests.lifecycle.test_audit_findings import AUDIT, REQS
from tests.lifecycle.test_audit_findings import _capture as _audit_capture
from tests.lifecycle.test_audit_findings import _run as _audit_run
from tests.lifecycle.test_lifecycle_matrix import CLOSEOUT, FILES, FIXED, T1, Script

DECL = "KIND: backend standard\nSCOPE: " + json.dumps(T1) + "\nImplement add in app.py."
DECL_REVIEWED = "KIND: architect complex\nSCOPE: " + json.dumps(T1) + "\nImplement add in app.py."
SANDBOX = "sandbox_apply: Operation not permitted (status 71)"


def _codex(text, *failures):
    """A codex stream whose failed commands precede the final message."""
    events = [{"type": "thread.started", "thread_id": str(uuid.uuid4())}, {"type": "turn.started"}]
    for n, (command, output, code) in enumerate(failures):
        events.append({"type": "item.completed", "item": {
            "id": f"item_{n}", "type": "command_execution", "command": command,
            "aggregated_output": output, "exit_code": code, "status": "failed"}})
    events += [{"type": "item.completed", "item": {"id": "last", "type": "agent_message", "text": text}},
               {"type": "turn.completed", "usage": H.USAGE}]
    return "\n".join(json.dumps(e) for e in events)


def _orchestrator(call, replay):
    return DECL


def test_a_sandbox_that_cannot_start_stops_the_run_before_any_review(tmp_path, monkeypatch):
    def lead(call, replay):
        assert call.vendor == "codex"
        return _codex("I could not read the project.\nCHANGED: []", ("/bin/zsh -lc 'cat app.py'", SANDBOX, 71))

    replay = H.run(tmp_path, monkeypatch, Script(orchestrator=_orchestrator, lead=lead), files=FILES,
                   record_complete=False)
    assert not replay.result.completed
    assert "could not run in this execution environment" in replay.result.error
    assert "sandbox_apply" in replay.result.error and "exit 71" in replay.result.error
    roles = [c.role.split(":")[0] for c in replay.calls]
    assert roles.count("lead") == 1
    assert not {"collaborator", "revision", "recheck", "gate-fix"} & set(roles), roles


def test_the_same_environmental_failure_on_unchanged_source_fails_the_task(tmp_path, monkeypatch):
    missing = ("pytest -q", "zsh:1: command not found: pytest", 127)

    def lead(call, replay):
        if call.task != "t1":
            return Script()._lead(call, replay)
        H.write(call, {"app.py": FIXED})
        return _codex('Implemented add; pytest is not installed here.\nCHANGED: ["app.py"]', missing)

    def revision(call, replay):
        return _codex("Nothing to change.\nCHANGED: []", missing)

    blocking = lambda call, replay: "BLOCKING: nothing tests negative numbers."  # noqa: E731
    replay = H.run(tmp_path, monkeypatch, Script(orchestrator=_orchestrator, lead=lead, revision=revision,
                                                 collaborator=blocking, closeout=lambda c, r: CLOSEOUT),
                   files=FILES, record_complete=False)
    assert len([c for c in replay.of("revision") if c.task == "t1"]) == 1
    record = json.dumps(replay.workflow)
    assert "same environmental tool failure" in record
    assert (replay.project / "app.py").read_text() == FIXED, "partial work kept"


def test_the_planner_is_given_the_configured_runner(tmp_path, monkeypatch):
    replay = H.run(tmp_path, monkeypatch, Script(), files=FILES)
    first = replay.of("orchestrator")[0].prompt
    assert "Configured project checks (the only commands the harness executes)" in first
    assert "- check (required): runs pytest" in first and "Do not prescribe another test command" in first
    assert "--quadratus-report" not in first


@pytest.mark.requirements_ledger
def test_the_auditor_judges_by_the_configured_checks_and_keeps_failures_against(tmp_path, monkeypatch):
    replay = _audit_run(tmp_path, monkeypatch, [REQS + AUDIT], {"t1": _audit_capture()})
    prompt = replay.of("auditor")[0].prompt
    assert "- check (required): runs pytest" in prompt and "--quadratus-report" not in prompt
    assert "its absence alone is not a NOT MET" in prompt
    assert "missing, failed, skipped, stale or not run is still not evidence of a pass" in prompt


def test_a_defaulted_pure_logic_card_does_not_forbid_the_database_the_task_names(tmp_path):
    policy = load_policy(tmp_path)
    sqlite = TaskScope(permitted_paths=["reservations.py", "tests/test_reservations.py"],
                       intended_result="Reservations are stored in SQLite", acceptance=["a booking survives restart"])
    packet = policy.role_packet(sqlite, "lead")
    assert "performs no network, database or filesystem access Because" not in packet
    assert "Not applied" in packet and "Permissions are unchanged" in packet
    pure = TaskScope(permitted_paths=["total.py"], intended_result="Calculate totals", acceptance=["2+2=4"])
    assert "performs no network, database or filesystem access Because" in policy.role_packet(pure, "lead")


def test_a_reviewer_whose_sandbox_cannot_start_stops_before_any_revision(tmp_path, monkeypatch):
    def reviewer(call, replay):
        return _codex("No blocking findings.\nREVIEW: COMPLETE", ("/bin/zsh -lc 'cat app.py'", SANDBOX, 71))

    replay = H.run(tmp_path, monkeypatch, Script(orchestrator=lambda c, r: DECL_REVIEWED, collaborator=reviewer),
                   files=FILES, record_complete=False)
    assert "could not run in this execution environment" in replay.result.error
    assert len(replay.of("collaborator")) == 1 and not replay.of("revision")


def test_only_environmental_failures_form_the_repeat_signature():
    assert _environment_signature([{"command": "pytest -q", "exit_code": 1, "output_tail": "1 failed"}]) == ()
    assert _environment_signature([{"command": "npm test", "exit_code": 127, "output_tail": "npm: not found"},
                                   {"command": "pytest", "exit_code": 2, "output_tail": "error"}]) == (("npm test", 127),)
    assert _environment_signature([{"command": "pytest", "exit_code": 1, "output_tail":
                                    "E   PermissionError: [Errno 13] Permission denied: 'x'"}]) == ()
    assert _environment_signature([{"command": "cat a", "exit_code": 1,
                                    "output_tail": "cat: a: Permission denied"}]) == (("cat a", 1),)
    assert _environment_signature(None) == ()

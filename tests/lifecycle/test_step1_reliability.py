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
import sys
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from quadratus.cli_providers import _codex_journal_tool_failures
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
SANDBOX = "sandbox-exec: sandbox_apply: Operation not permitted"


def _codex(text, *failures, thread=None):
    """A codex stream whose failed commands precede the final message."""
    events = [{"type": "thread.started", "thread_id": thread or str(uuid.uuid4())}, {"type": "turn.started"}]
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
    assert "- check (required): runs pytest" in first and "Do not invent a test command" in first
    assert "ASK, naming the command and the configured checks as a configuration conflict" in first
    assert "--quadratus-report" not in first


@pytest.mark.requirements_ledger
def test_the_auditor_judges_by_the_configured_checks_and_keeps_failures_against(tmp_path, monkeypatch):
    replay = _audit_run(tmp_path, monkeypatch, [REQS + AUDIT], {"t1": _audit_capture()})
    prompt = replay.of("auditor")[0].prompt
    assert "- check (required): runs pytest" in prompt and "--quadratus-report" not in prompt
    assert "only a plan, task or model prescribed is not a requirement" in prompt
    assert "NOT MET as a configuration conflict naming the command" in prompt
    assert "missing, failed, skipped, stale or not run is still not evidence of a pass" in prompt


def test_a_defaulted_pure_logic_card_does_not_forbid_the_database_the_task_names(tmp_path):
    policy = load_policy(tmp_path)
    sqlite = TaskScope(permitted_paths=["reservations.py", "tests/test_reservations.py"],
                       intended_result="Reservations are stored in SQLite", acceptance=["a booking survives restart"])
    packet = policy.role_packet(sqlite, "lead")
    assert "performs no network, database or filesystem access Because" not in packet
    assert "no I/O beyond the database access this task's scope asks for, and no network or filesystem" in packet
    assert "Permissions are unchanged" in packet
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


# -- the native journal shape (#35, root's Mac capture, 2026-10-13) ---------------

NATIVE_REFUSAL = [{"type": "input_text", "text": "Script completed\nWall time 0.0 seconds\nOutput:\n"},
                  {"type": "input_text", "text": "sandbox-exec: sandbox_apply: Operation not permitted\n"}]


def _journal(home, thread, rows, *, cwd, meta_id=None, begun=None):
    folder = home / "sessions" / "2026" / "10" / "13"
    folder.mkdir(parents=True, exist_ok=True)
    meta = {"type": "session_meta", "payload": {"id": meta_id or thread, "cwd": str(cwd),
                                                 "timestamp": (begun or datetime.now(timezone.utc)).isoformat()}}
    (folder / f"rollout-2026-10-13T10-00-00-{thread}.jsonl").write_text(
        "\n".join(json.dumps(r) for r in [meta] + [{"type": "response_item", "payload": p} for p in rows]) + "\n")


def test_the_native_journal_refusal_is_read_from_tool_output_only(tmp_path):
    thread = str(uuid.uuid4())
    started = datetime.now(timezone.utc)
    _journal(tmp_path, thread, cwd=tmp_path, rows=[
        {"type": "reasoning", "summary": [{"type": "summary_text", "text": "sandbox-exec: sandbox_apply: x"}]},
        {"type": "message", "role": "assistant", "content": [{"type": "output_text",
                                                               "text": "sandbox-exec: sandbox_apply: y"}]},
        {"type": "custom_tool_call", "call_id": "c1", "name": "shell", "input": "pytest"},
        {"type": "custom_tool_call_output", "call_id": "c1", "output": [
            {"type": "input_text", "text": "Script failed\nOutput:\nE  assert 'sandbox-exec: ' in msg\n"
                                           "tests/test_app.py: sandbox_apply mentioned mid-line\n"}]},
        {"type": "custom_tool_call", "call_id": "c2", "name": "shell", "input": "cat app.py"},
        {"type": "custom_tool_call_output", "call_id": "c2", "output": NATIVE_REFUSAL},
    ])
    found, recovered = _codex_journal_tool_failures(tmp_path / "sessions", thread, tmp_path, started)
    assert found == [{"kind": "native-tool", "command": "shell", "status": "failed",
                      "output_tail": "sandbox-exec: sandbox_apply: Operation not permitted"}]
    assert recovered is False
    assert _codex_journal_tool_failures(tmp_path / "sessions", "../../etc", tmp_path, started) == ([], None)


@pytest.mark.parametrize("change", ["other id", "other cwd", "before the call", "duplicate", "no window"])
def test_a_journal_must_prove_it_is_this_calls_own(tmp_path, change):
    thread = str(uuid.uuid4())
    started = datetime.now(timezone.utc)
    rows = [{"type": "custom_tool_call_output", "call_id": "c1", "output": NATIVE_REFUSAL}]
    _journal(tmp_path, thread, rows, cwd=tmp_path / "elsewhere" if change == "other cwd" else tmp_path,
             meta_id=str(uuid.uuid4()) if change == "other id" else None,
             begun=started - timedelta(hours=1) if change == "before the call" else None)
    if change == "duplicate":
        (tmp_path / "sessions" / "2026" / "10" / "14").mkdir(parents=True)
        (tmp_path / "sessions" / "2026" / "10" / "14" / f"rollout-x-{thread}.jsonl").write_text("{}\n")
    assert _codex_journal_tool_failures(tmp_path / "sessions", thread, tmp_path,
                                        None if change == "no window" else started) == ([], None)


def test_a_native_journal_refusal_stops_the_run_before_any_review(tmp_path, monkeypatch):
    home = tmp_path / "codex-home"
    monkeypatch.setenv("CODEX_HOME", str(home))
    thread = str(uuid.uuid4())

    def lead(call, replay):
        assert call.vendor == "codex"
        _journal(home, thread, [{"type": "custom_tool_call", "call_id": "c1", "name": "shell", "input": "ls"},
                                {"type": "custom_tool_call_output", "call_id": "c1", "output": NATIVE_REFUSAL}],
                 cwd=call.cwd)
        return _codex("I could not read the project.\nCHANGED: []", thread=thread)

    replay = H.run(tmp_path, monkeypatch, Script(orchestrator=_orchestrator, lead=lead), files=FILES,
                   record_complete=False)
    assert "could not run in this execution environment" in replay.result.error
    assert "sandbox-exec: sandbox_apply: Operation not permitted" in replay.result.error
    roles = [c.role.split(":")[0] for c in replay.calls]
    assert roles.count("lead") == 1 and not {"collaborator", "revision"} & set(roles), roles


def test_application_output_that_mentions_a_sandbox_is_not_an_infrastructure_failure(tmp_path, monkeypatch):
    def lead(call, replay):
        if call.task != "t1":
            return Script()._lead(call, replay)
        H.write(call, {"app.py": FIXED})
        return _codex('Implemented add.\nCHANGED: ["app.py"]',
                      ("pytest -q", "E   AssertionError: expected 'bwrap: denied' and seccomp profile", 1))

    replay = H.run(tmp_path, monkeypatch, Script(orchestrator=_orchestrator, lead=lead), files=FILES)
    assert "execution environment" not in (replay.result.error or "")
    assert replay.artifact_texts("factual-closeout"), "the task ran its ordinary course to its close"


# -- root review of d51ae79: five negatives, kept as root wrote them ----------------

def test_skipped_check_keeps_private_operands_out_of_instruction_packet(tmp_path):
    from quadratus import session
    from quadratus.integration import GateCommand, GateSuite
    secret = '/private/tmp/hidden-grader/hidden_case.py'
    gate = GateSuite([GateCommand('optional', (sys.executable, '-m', 'pytest', secret),
                                  required=False, skip_reason='unavailable: ' + secret)], cwd=tmp_path)
    note = session._configured_checks(gate)
    assert secret not in note, note
    assert "runs pytest; skipped by configuration" in note
    script = GateSuite([GateCommand('grader', (secret,))], cwd=tmp_path)
    assert "hidden" not in session._configured_checks(script)


def test_journal_filename_does_not_authorize_mismatched_session_body(tmp_path):
    sid = str(uuid.uuid4())
    folder = tmp_path / '2026' / '10' / '13'
    folder.mkdir(parents=True)
    records = [dict(type='session_meta', payload=dict(id=str(uuid.uuid4()), cwd='/wrong/project')),
               dict(type='response_item', payload=dict(type='custom_tool_call_output', call_id='x',
                    output=[dict(type='input_text', text='sandbox-exec: sandbox_apply: Operation not permitted')]))]
    (folder / ('rollout-2026-10-13T10-00-00-' + sid + '.jsonl')).write_text(
        '\n'.join(json.dumps(r) for r in records) + '\n')
    assert _codex_journal_tool_failures(tmp_path, sid, tmp_path, datetime.now(timezone.utc)) == ([], None)


def test_application_diagnostic_at_line_start_is_not_sandbox_startup_failure():
    from quadratus import runtime
    entry = dict(kind='command', command='python -m pytest', exit_code=1, status='failed',
                 output_tail='bwrap: fixture diagnostic generated by a unit test\nFAILED test_expected_value')
    assert runtime._infrastructure_failures([entry]) == []


def test_resolved_tool_failure_does_not_block_delivered_and_checked_source(tmp_path, monkeypatch):
    def lead(call, replay):
        H.write(call, {'app.py': FIXED})
        events = [dict(type='thread.started', thread_id=str(uuid.uuid4())), dict(type='turn.started'),
                  dict(type='item.completed', item=dict(id='failed', type='command_execution',
                       command='cat app.py', aggregated_output='sandbox-exec: sandbox_apply: Operation not permitted',
                       exit_code=71, status='failed')),
                  dict(type='item.completed', item=dict(id='success', type='command_execution',
                       command='python -m pytest', aggregated_output='3 passed', exit_code=0, status='completed')),
                  dict(type='item.completed', item=dict(id='reply', type='agent_message',
                       text='Recovered, implemented and tested add.\nCHANGED: ["app.py"]')),
                  dict(type='turn.completed', usage=H.USAGE)]
        return '\n'.join(json.dumps(e) for e in events)
    replay = H.run(tmp_path, monkeypatch, Script(orchestrator=_orchestrator, lead=lead), files=FILES,
                   record_complete=False)
    assert replay.artifact_texts('factual-closeout'), replay.result.error
    record = json.loads((Path(replay.result.run_dir) / 'result.json').read_text())
    assert any(c.get('passed') for c in record['checks']), 'real canonical gate must run'
    calls = (Path(replay.result.run_dir) / "invocations.jsonl").read_text()
    assert "sandbox-exec: sandbox_apply: Operation not permitted" in calls, "the earlier refusal stays recorded"


def test_explicit_no_io_task_keeps_its_no_io_family_check(tmp_path):
    policy = load_policy(tmp_path)
    scope = TaskScope(permitted_paths=['compute.py'],
                      intended_result='Pure calculation without database, network or filesystem access',
                      acceptance=['No database or network calls'])
    packet = policy.role_packet(scope, 'lead')
    assert 'performs no network, database or filesystem access Because' in packet, packet


def test_a_project_policy_default_family_keeps_its_no_io_item(tmp_path):
    (tmp_path / ".quadratus").mkdir()
    from quadratus.policy import load_library
    _, _, cards = load_library()
    assert "pure-logic" in cards
    sqlite = TaskScope(permitted_paths=["reservations.py"], intended_result="Reservations are stored in SQLite")
    builtin = load_policy(tmp_path).role_packet(sqlite, "lead")
    assert "no I/O beyond the database access" in builtin


# -- root review of feacb60: two remaining edges ------------------------------------

def test_sqlite_task_forbidding_network_still_can_use_authorized_database(tmp_path):
    policy = load_policy(tmp_path)
    scope = TaskScope(permitted_paths=['inventory.py'], intended_result='Store reservations in SQLite',
                      acceptance=['No network access', 'Batch persists atomically'])
    packet = policy.role_packet(scope, 'lead')
    assert 'performs no network, database or filesystem access Because' not in packet, packet
    assert 'No network access' in packet
    assert "beyond the database access this task's scope asks for, and no network or filesystem access" in packet


def _journal_rows(tmp_path, rows):
    sid, started = str(uuid.uuid4()), datetime.now(timezone.utc)
    _journal(tmp_path, sid, rows, cwd=tmp_path)
    return _codex_journal_tool_failures(tmp_path / "sessions", sid, tmp_path, started)


REFUSED = [{"type": "custom_tool_call", "call_id": "fail", "name": "shell", "input": "cat app.py"},
           {"type": "custom_tool_call_output", "call_id": "fail", "output": NATIVE_REFUSAL}]


def test_non_shell_documentation_output_cannot_prove_sandbox_recovery(tmp_path):
    failures, recovered = _journal_rows(tmp_path, REFUSED + [
        {"type": "function_call", "call_id": "docs", "name": "web_search", "arguments": "documentation"},
        {"type": "function_call_output", "call_id": "docs",
         "output": 'Documentation example: {"exit_code": 0}; no local process was executed.'}])
    assert failures and recovered is False, (failures, recovered)


@pytest.mark.parametrize("output", [
    'Script completed\nOutput:\nREADME: run it and expect {"exit_code": 0}\n',   # quoted in command output
    '{"output": "x", "metadata": {"exit_code": 1}}',                              # a real failure
    'Script completed\nWall time 0.1 seconds\nOutput:\nok\n',                    # no stated status
])
def test_a_shell_output_without_its_own_zero_status_is_not_a_recovery(tmp_path, output):
    failures, recovered = _journal_rows(tmp_path, REFUSED + [
        {"type": "function_call", "call_id": "s2", "name": "shell", "arguments": "{}"},
        {"type": "function_call_output", "call_id": "s2", "output": output}])
    assert failures and recovered is False


@pytest.mark.parametrize("output", ['{"output": "3 passed", "metadata": {"exit_code": 0}}',
                                    'Exit code: 0\nWall time: 0.2 seconds\nOutput:\n3 passed\n'])
def test_a_later_shell_command_with_its_own_zero_status_is_a_recovery(tmp_path, output):
    failures, recovered = _journal_rows(tmp_path, REFUSED + [
        {"type": "function_call", "call_id": "s2", "name": "shell", "arguments": "{}"},
        {"type": "function_call_output", "call_id": "s2", "output": output}])
    assert failures and recovered is True

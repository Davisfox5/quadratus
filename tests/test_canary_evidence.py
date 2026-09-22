"""What the Q9 live canary (2026-09-22) showed was missing, pinned.

Both runs ended on a lead that could not start its sandbox. Three things made
that pair uninformative rather than merely failed, and each has a test here:

* the security path advertised WORKER and then ignored it (baseline);
* a blocked report carried no command, exit status or error, and nothing the
  harness retained could confirm or refute it (candidate);
* the CLI's own record of the failed command was thrown away with its stdout.
"""

from __future__ import annotations

import json
from subprocess import CompletedProcess

import pytest

from quadratus.artifacts import ArtifactStore
from quadratus.cli_providers import (
    STDERR_TAIL,
    CodexCLIProvider,
    _extract_codex_tool_failures,
)
from quadratus.config import Settings
from quadratus.delegation import (
    DelegationLedger,
    InvocationEvent,
    bounded_stderr,
    bounded_tool_failures,
)
from quadratus.runtime import Fleet
from quadratus.scope import TaskScope
from quadratus.session import _BLOCKED_REPORT_RULE, Session, SessionConfig, TaskSpec
from quadratus.task_kinds import TaskKind

SOL = "openai:gpt-5.6-sol"
SECURITY_TASK = TaskSpec("t1", "close the tenant leak", kind=TaskKind.SECURITY,
                         scope=TaskScope(permitted_paths=["app.py"], max_lines=10))

BWRAP = ("bwrap: No permissions to create a new namespace, likely because the "
         "kernel does not allow non-privileged user namespaces.")

FAILED_STREAM = "\n".join([
    '{"type":"thread.started","thread_id":"t"}',
    '{"type":"turn.started"}',
    json.dumps({"type": "item.completed", "item": {
        "id": "item_1", "type": "command_execution", "command": "cat app.py",
        "aggregated_output": BWRAP + "\n", "exit_code": 1, "status": "failed"}}),
    json.dumps({"type": "item.completed", "item": {
        "id": "item_2", "type": "command_execution", "command": "ls",
        "aggregated_output": "app.py\n", "exit_code": 0, "status": "completed"}}),
    json.dumps({"type": "item.completed", "item": {
        "id": "item_3", "type": "agent_message",
        "text": "Unable to edit or test: the sandbox failed to initialize.\n\nCHANGED: []"}}),
    '{"type":"turn.completed","usage":{"input_tokens":10,"output_tokens":5}}',
])


# -- the CLI's own record of a failed command survives ------------------------


def test_codex_tool_failures_keep_the_failed_command_and_drop_the_rest():
    failures = _extract_codex_tool_failures(FAILED_STREAM)
    assert failures == [{
        "kind": "command", "command": "cat app.py", "exit_code": 1,
        "status": "failed", "output_tail": BWRAP + "\n",
    }]


def test_codex_tool_failures_are_bounded_and_never_raise():
    long = json.dumps({"type": "item.completed", "item": {
        "id": "x", "type": "command_execution", "command": "c" * 5000,
        "aggregated_output": "o" * 5000, "exit_code": 2}})
    stream = "\n".join([long] * 20 + ["not json", '{"type":"item.completed","item":"?"}'])
    failures = _extract_codex_tool_failures(stream)
    assert len(failures) == 8
    assert all(len(f["command"]) == 500 and len(f["output_tail"]) == 500 for f in failures)
    assert _extract_codex_tool_failures("") == []


def test_the_codex_provider_records_stderr_and_tool_failures_per_attempt(tmp_path, monkeypatch):
    monkeypatch.setattr("shutil.which", lambda _: "/unused/codex")
    monkeypatch.setattr("quadratus.cli_providers._launch",
                        lambda *a, **kw: CompletedProcess([], 0, FAILED_STREAM, "x" * 5000 + BWRAP))
    provider = CodexCLIProvider(model="gpt-5.6-sol", workdir=str(tmp_path), max_retries=1)
    provider.generate("fix it")
    assert provider.last_tool_failures[0]["command"] == "cat app.py"
    assert provider.last_stderr.endswith(BWRAP)
    assert len(provider.last_stderr) == STDERR_TAIL
    # A clean attempt must not inherit the previous attempt's evidence.
    monkeypatch.setattr("quadratus.cli_providers._launch",
                        lambda *a, **kw: CompletedProcess(
                            [], 0, '{"type":"item.completed","item":{"id":"i","type":"agent_message","text":"ok"}}', ""))
    provider.generate("again")
    assert provider.last_tool_failures == [] and provider.last_stderr == ""


def test_stderr_and_tool_failures_reach_the_ledger_bounded(tmp_path, monkeypatch):
    monkeypatch.setattr("shutil.which", lambda _: "/unused/codex")
    monkeypatch.setattr("quadratus.cli_providers._launch",
                        lambda *a, **kw: CompletedProcess([], 0, FAILED_STREAM, BWRAP))
    provider = CodexCLIProvider(model="gpt-5.6-sol", workdir=str(tmp_path), max_retries=1)
    path = tmp_path / "invocations.jsonl"
    fleet = Fleet(Settings(backend="cli"), delegation_ledger=DelegationLedger(path=path))
    monkeypatch.setattr(fleet, "provider_for", lambda _: provider)
    fleet.invoke(SOL, "fix it")
    row = json.loads(path.read_text().splitlines()[0])
    assert row["stderr_tail"] == BWRAP
    assert row["tool_failures"] == [{
        "kind": "command", "command": "cat app.py", "status": "failed",
        "output_tail": BWRAP + "\n", "exit_code": 1,
    }]


def test_ledger_bounds_are_reapplied_whatever_a_provider_sends():
    assert bounded_stderr(b"a" * 3000) == "a" * 2000
    assert bounded_stderr(None) == "" and bounded_stderr(42) == ""
    kept = bounded_tool_failures([
        {"kind": "command", "command": "c" * 900, "exit_code": 3, "arguments": "secret",
         "output_tail": "o" * 900},
        "not a dict",
        {"exit_code": "1"},
    ] + [{"kind": "error", "message": "m"}] * 20)
    assert len(kept) == 8
    assert kept[0] == {"kind": "command", "command": "c" * 500, "output_tail": "o" * 500,
                       "exit_code": 3}
    assert "arguments" not in kept[0]
    assert kept[1] == {"kind": "error", "message": "m"}


def test_older_ledger_rows_still_load_without_the_new_fields():
    event = InvocationEvent(task="t", role="lead")
    assert event.stderr_tail == "" and event.tool_failures == []


# -- the security excursion serves the channels its prompt offers -------------


def _security_session(tmp_path, invoke):
    store = ArtifactStore(tmp_path / ".quadratus")
    return Session("work", store, invoke=invoke, available=lambda key: True,
                   config=SessionConfig(project=tmp_path, allow_writes=True))


def test_a_security_lead_can_commission_a_worker(tmp_path):
    """Q9 baseline: Sol answered WORKER as its prompt instructed, the harness
    filed the request as the draft, and the verifier rejected 'a dispatch, not
    a result'. The excursion now serves the worker channel like any lead."""
    calls = []

    def invoke(key, prompt, *, allow_writes=False):
        calls.append((key, prompt))
        if "change line 29" in prompt and "You are leading" not in prompt:
            return "patched: line 29 now checks the tenant"
        if "The task is finished" in prompt:
            return "SUMMARY: fixed\nREASONING: worker evidence"
        if "verifying security work" in prompt:
            return "Accepted."
        if "patched: line 29" in prompt:
            return "Applied the tenant check.\n\nCHANGED: []"
        return ('WORKER {"errand":"code","instruction":"change line 29 to check the tenant",'
                '"write":true,"needs":["patch"]}')

    session = _security_session(tmp_path, invoke)
    result = session.run_task(SECURITY_TASK)
    assert result.author == SOL
    worker = [k for k, p in calls if "change line 29" in p and "You are leading" not in p]
    assert worker == ["claude:haiku"]
    drafts = [p for k, p in calls if "You are leading" in p]
    assert any("patched: line 29" in p for p in drafts)
    assert any("Applied the tenant check" in p for k, p in calls if "verifying security work" in p)


def test_a_security_lead_asking_for_a_consult_is_refused_and_re_asked(tmp_path):
    """Consults never happen inside a security excursion. Before, the prompt
    did not offer one; now the refusal is explicit and the lead is re-asked."""
    calls = []

    def invoke(key, prompt, *, allow_writes=False):
        calls.append((key, prompt))
        if "The task is finished" in prompt:
            return "SUMMARY: done\nREASONING: alone"
        if "verifying security work" in prompt:
            return "Accepted."
        if "not available inside a security excursion" in prompt:
            return "Decided alone.\n\nCHANGED: []"
        return "CONSULT Opus 5: is this predicate right?"

    session = _security_session(tmp_path, invoke)
    session.run_task(SECURITY_TASK)
    leads = [p for k, p in calls if "You are leading" in p]
    assert len(leads) == 2
    assert "you may consult" not in leads[0].lower()
    assert all(k != "claude:opus" or "verifying security work" in p for k, p in calls)


def test_the_lead_prompt_demands_evidence_for_a_blocked_report(tmp_path):
    seen = []

    def invoke(key, prompt, *, allow_writes=False):
        seen.append(prompt)
        if "The task is finished" in prompt:
            return "SUMMARY: s\nREASONING: r"
        if "verifying security work" in prompt:
            return "Accepted."
        return "done\n\nCHANGED: []"

    session = _security_session(tmp_path, invoke)
    session.run_task(SECURITY_TASK)
    lead = next(p for p in seen if "You are leading" in p)
    assert _BLOCKED_REPORT_RULE in lead
    verifier = next(p for p in seen if "verifying security work" in p)
    assert "verbatim error" in verifier


def test_the_rule_is_absent_without_a_project(tmp_path):
    """No project means nothing to read, edit or run; the rule would only be noise."""
    store = ArtifactStore(tmp_path / "artifacts")
    seen = []

    def invoke(key, prompt, system=None):
        seen.append(prompt)
        if "The task is finished" in prompt:
            return "SUMMARY: s\nREASONING: r"
        return "answer"

    Session("work", store, invoke).run_task(TaskSpec("t1", "think"))
    assert not any(_BLOCKED_REPORT_RULE in p for p in seen)


@pytest.mark.parametrize("stream", ["", "garbage"])
def test_tool_failure_extraction_tolerates_empty_output(stream):
    assert _extract_codex_tool_failures(stream) == []

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
import subprocess
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
from quadratus.session import _BLOCKED_REPORT_RULE, RunStalled, Session, SessionConfig, TaskSpec
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


def test_repeated_consult_requests_in_a_security_excursion_stall_the_run(tmp_path):
    """Codex's finding on #25: the refusal branch re-asked without counting, so
    a lead that never stopped asking was re-invoked until the budget was gone.
    Refusals now spend the consult allowance and the run stalls past it."""
    calls = []

    def invoke(key, prompt, *, allow_writes=False):
        calls.append(key)
        return "CONSULT Opus 5: still asking?"

    session = _security_session(tmp_path, invoke)
    with pytest.raises(RunStalled, match="not converging"):
        session.run_task(SECURITY_TASK)
    assert len(calls) == session.config.max_consults + 1
    assert set(calls) == {SOL}


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


# -- a live run names a preflight report that passed --------------------------


def _canary_launcher():
    import importlib.util
    from pathlib import Path

    path = Path(__file__).resolve().parents[1] / "docs" / "harness-canary" / "run_fixture.py"
    spec = importlib.util.spec_from_file_location("canary_run_fixture", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_allowance_missing_preflight_report_is_refused():
    launcher = _canary_launcher()
    with pytest.raises(SystemExit, match="preflight_report"):
        launcher.require_allowance_preflight({"authorized_by": "Davis", "source": "x"}, ".")


def test_allowance_preflight_not_ok_names_the_first_blocker(tmp_path):
    report = tmp_path / "preflight.json"
    report.write_text(json.dumps({
        "ok": False,
        "blockers": ["grok is not signed in", "codex is not signed in"],
    }))
    launcher = _canary_launcher()
    with pytest.raises(SystemExit, match="grok is not signed in") as raised:
        launcher.require_allowance_preflight({"preflight_report": str(report)}, tmp_path)
    assert "codex is not signed in" not in str(raised.value)


def _bound_preflight(tmp_path, *, host=True, contained=False, blockers=None, ok=True, probe=None):
    project = tmp_path / "proj"
    project.mkdir()
    app = project / "app.py"
    app.write_text("x")
    report = tmp_path / "preflight.json"
    report.write_text(json.dumps({
        "ok": ok,
        "blockers": [] if blockers is None else blockers,
        "probe_file": str(app if probe is None else probe),
        "host": host,
        "contained": contained,
    }))
    return project, report


def _approved(tmp_path, **over):
    project, report = _bound_preflight(tmp_path)
    record = {
        "schema": "quadratus-canary-allowance/2",
        "approved": True,
        "batch_id": "batch-1",
        "authorized_by": "Davis",
        "source": "https://example.test/allowance",
        "instruction": "one pair",
        "recorded_at": "2026-09-22T00:00:00+00:00",
        "environment": "native-mac",
        "fixture": "fixture-v2",
        "grader_sha256": "ab" * 32,
        "baseline_sha": "a" * 40,
        "candidate_sha": "b" * 40,
        "runs": ["baseline", "candidate"],
        "runs_per_version": 1,
        "max_calls_each": 24,
        "max_reported_tokens_each": 500000,
        "max_reported_tokens_batch": 1000000,
        "internal_wall_seconds_each": 840,
        "external_wall_seconds_each": 900,
        "automatic_reruns": False,
        "transport": "subscription CLI only",
        "preflight_report": str(report),
    }
    record.update(over)
    return record, project


def test_allowance_preflight_ok_is_accepted(tmp_path):
    record, project = _approved(tmp_path)
    _canary_launcher().require_allowance_preflight(record, project)


def test_shipped_allowance_template_cannot_pass():
    from pathlib import Path as P
    template = P(__file__).resolve().parents[1] / "docs" / "harness-canary" / "allowance.template.json"
    with pytest.raises(SystemExit, match="approved"):
        _canary_launcher().require_allowance_record(json.loads(template.read_text()))


def test_allowance_preflight_wrong_project_is_refused(tmp_path):
    record, _project = _approved(tmp_path)
    other = tmp_path / "other"
    other.mkdir()
    with pytest.raises(SystemExit, match="probe_file"):
        _canary_launcher().require_allowance_preflight(record, other)


def test_allowance_preflight_wrong_environment_is_refused(tmp_path):
    record, project = _approved(tmp_path)
    report_path = project.parent / "preflight.json"
    report = json.loads(report_path.read_text())
    report["host"] = False
    report_path.write_text(json.dumps(report))
    with pytest.raises(SystemExit, match="host"):
        _canary_launcher().require_allowance_preflight(record, project)


def test_allowance_preflight_contradictory_report_names_the_blocker(tmp_path):
    record, project = _approved(tmp_path)
    report_path = project.parent / "preflight.json"
    report = json.loads(report_path.read_text())
    report["ok"] = True
    report["blockers"] = ["grok is not signed in"]
    report_path.write_text(json.dumps(report))
    with pytest.raises(SystemExit, match="grok is not signed in"):
        _canary_launcher().require_allowance_preflight(record, project)


def test_allowance_unknown_runtime_commit_is_refused(tmp_path):
    record, _project = _approved(tmp_path)
    launcher = _canary_launcher()
    launcher.require_allowance_record(record)
    with pytest.raises(SystemExit, match="unknown"):
        launcher.bind_runtime(record, "unknown")


def test_allowance_unlisted_runtime_commit_is_refused(tmp_path):
    record, _project = _approved(tmp_path)
    with pytest.raises(SystemExit, match="not a commit the allowance names"):
        _canary_launcher().bind_runtime(record, "c" * 40)


def test_launcher_limits_must_match_the_record(tmp_path):
    from quadratus.run_budget import RunLimits
    record, _project = _approved(tmp_path)
    limits = RunLimits(max_calls=25, max_reported_tokens=500000, wall_seconds=840)
    with pytest.raises(SystemExit, match="max_calls_each"):
        _canary_launcher().check_launcher_limits(record, limits)


def test_record_cannot_raise_the_call_ceiling(tmp_path):
    from quadratus.run_budget import RunLimits
    record, _project = _approved(tmp_path, max_calls_each=25)
    limits = RunLimits(max_calls=24, max_reported_tokens=500000, wall_seconds=840)
    with pytest.raises(SystemExit, match="launcher ceiling is 24"):
        _canary_launcher().check_launcher_limits(record, limits)


def test_ceiling_limits_are_accepted(tmp_path):
    from quadratus.run_budget import RunLimits
    record, _project = _approved(tmp_path)
    limits = RunLimits(max_calls=24, max_reported_tokens=500000, wall_seconds=840)
    _canary_launcher().check_launcher_limits(record, limits)


def test_runtime_commit_binds_to_the_launcher_checkout(tmp_path, monkeypatch):
    other = tmp_path / "other-repo"
    other.mkdir()
    git = ["git", "-C", str(other)]
    subprocess.run([*git, "init"], check=True, capture_output=True)
    subprocess.run([*git, "config", "user.email", "canary@example.test"], check=True)
    subprocess.run([*git, "config", "user.name", "canary"], check=True)
    (other / "note.txt").write_text("not the launcher\n")
    subprocess.run([*git, "add", "note.txt"], check=True)
    subprocess.run(
        [*git, "-c", "commit.gpgsign=false", "commit", "-m", "other"],
        check=True, capture_output=True,
    )
    monkeypatch.chdir(other)
    launcher = _canary_launcher()
    got = launcher.runtime_commit()
    root = launcher.runtime_root()
    expected = subprocess.check_output(["git", "-C", str(root), "rev-parse", "HEAD"], text=True).strip()
    other_head = subprocess.check_output(["git", "-C", str(other), "rev-parse", "HEAD"], text=True).strip()
    assert got == expected
    assert got != other_head


def test_empty_batch_id_is_refused(tmp_path):
    record, _project = _approved(tmp_path, batch_id="  ")
    with pytest.raises(SystemExit, match="batch_id"):
        _canary_launcher().require_allowance_record(record)


def test_missing_batch_id_is_refused(tmp_path):
    record, _project = _approved(tmp_path)
    del record["batch_id"]
    with pytest.raises(SystemExit, match="batch_id"):
        _canary_launcher().require_allowance_record(record)


def _write_manifest(project, digest):
    folder = project / ".quadratus"
    folder.mkdir(exist_ok=True)
    (folder / "fixture-manifest.json").write_text(json.dumps({
        "instrument_sha256": {"test_contract.py": digest},
    }))


def test_grader_sha256_mismatch_is_refused(tmp_path):
    record, project = _approved(tmp_path, grader_sha256="ab" * 32)
    _write_manifest(project, "cd" * 32)
    with pytest.raises(SystemExit, match="grader_sha256"):
        _canary_launcher().check_grader(record, project)


def test_grader_sha256_match_is_accepted(tmp_path):
    digest = "ab" * 32
    record, project = _approved(tmp_path, grader_sha256=digest)
    _write_manifest(project, digest)
    _canary_launcher().check_grader(record, project)


def test_missing_grader_manifest_is_refused(tmp_path):
    record, project = _approved(tmp_path, grader_sha256="ab" * 32)
    with pytest.raises(SystemExit, match="manifest"):
        _canary_launcher().check_grader(record, project)


# -- the launcher resolves the fixture it was handed ---------------------------


def test_q9_fixture_resolves_without_a_manifest(tmp_path):
    fx = _canary_launcher().resolve_fixture(tmp_path)
    assert fx["mode"] == "q9" and fx["scope_paths"] == ("app.py",) and fx["max_tasks"] == 2


def _v2_copy(tmp_path, grader_text="def test_preservation_x():\n    pass\n"):
    import hashlib
    project = tmp_path / "trial"
    (project / ".quadratus").mkdir(parents=True)
    instrument = tmp_path / "trial-instrument"
    instrument.mkdir()
    grader = instrument / "test_contract.py"
    grader.write_text(grader_text)
    digest = hashlib.sha256(grader.read_bytes()).hexdigest()
    (project / ".quadratus" / "fixture-manifest.json").write_text(json.dumps({
        "grader": str(grader), "instrument_sha256": {"test_contract.py": digest},
        "allowed_paths": ["presentation.py", "app.py", "access.py"], "max_tasks": 2}))
    return project, grader, digest


def test_v2_fixture_resolves_from_the_manifest(tmp_path):
    project, grader, digest = _v2_copy(tmp_path)
    fx = _canary_launcher().resolve_fixture(project)
    assert fx["mode"] == "v2"
    assert fx["scope_paths"] == ("presentation.py", "app.py", "access.py")
    assert fx["max_lines"] == 60 and fx["max_tasks"] == 2
    assert fx["check"].endswith(f"{grader} -k preservation")
    assert "Complete exactly two tasks" in fx["goal"]


def test_v2_fixture_refuses_a_grader_that_does_not_match_the_manifest(tmp_path):
    project, grader, digest = _v2_copy(tmp_path)
    grader.write_text("def test_preservation_x():\n    assert False\n")
    with pytest.raises(SystemExit, match="hashes to"):
        _canary_launcher().resolve_fixture(project)
    grader.unlink()
    with pytest.raises(SystemExit, match="unreadable"):
        _canary_launcher().resolve_fixture(project)


def test_v2_fixture_refuses_a_grader_inside_the_solver_tree(tmp_path):
    project, grader, digest = _v2_copy(tmp_path)
    inside = project / "control" / "test_contract.py"
    inside.parent.mkdir()
    inside.write_text(grader.read_text())
    manifest = json.loads((project / ".quadratus" / "fixture-manifest.json").read_text())
    manifest["grader"] = str(inside)
    (project / ".quadratus" / "fixture-manifest.json").write_text(json.dumps(manifest))
    with pytest.raises(SystemExit, match="inside the solver tree"):
        _canary_launcher().resolve_fixture(project)


def test_runtime_root_is_the_imported_package_checkout(tmp_path, monkeypatch):
    from pathlib import Path

    import quadratus
    launcher = _canary_launcher()
    assert launcher.runtime_root() == Path(quadratus.__file__).resolve().parents[1]
    monkeypatch.setattr(launcher, "HERE", tmp_path / "elsewhere" / "docs" / "harness-canary")
    assert launcher.runtime_root() == Path(quadratus.__file__).resolve().parents[1]

"""Harness-owned capture and capability stops (Codex, Run 18; contract v2 on #25).

Whole-controller replays. With a capture profile the harness starts a real
preview (``http.server``), captures the task's declared page with a real
browser, and stops it; the replayed leads only edit source. Without one, a
lead whose transport cannot run the capture is stopped before it is invoked,
and a lead denied the harness's own command is stopped after its one call.
Scripted APPROVED is routing proof, not image judgement.
"""

import json
import os
import socket
import sys
from pathlib import Path

import pytest

from quadratus.config import Settings
from tests.lifecycle import harness as H
from tests.lifecycle.test_audit_findings import (
    AUDIT_SCOPE,
    MET,
    NO_EDIT,
    REPAIR_SCOPE,
    REQS,
    Script,
    _design_files,
)

pytestmark = pytest.mark.requirements_ledger

ICON = "<head><link rel=icon href='data:,'></head>"
WIDE_PAGE = f"<html>{ICON}<body><div class=toolbar style='width:450px'>tools</div></body></html>\n"
FITTING_PAGE = f"<html>{ICON}<body><div class=toolbar style='max-width:100%'>tools</div></body></html>\n"
CAPTURE = {"path": "/index.html", "steps": []}
browser = pytest.mark.skipif(not os.environ.get("QUADRATUS_CHROMIUM"), reason="needs a browser")


def _free_port():
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


def _decl(kind_line, scope, text, covers="R1, R2"):
    return f"{kind_line}\nSCOPE: {json.dumps(scope)}\n{text}\nCOVERS: {covers}"


AUDIT = _decl("KIND: frontend standard", dict(AUDIT_SCOPE, capture=CAPTURE), "Audit the import page.")
REPAIR = _decl("KIND: frontend standard", dict(REPAIR_SCOPE, capture=CAPTURE), "Fix the mobile overflow.")


def _profile(tmp_path, preview=None):
    port = _free_port()
    path = tmp_path / "profile.json"
    path.write_text(json.dumps(dict(
        preview=preview or [sys.executable, "-m", "http.server", str(port), "--bind", "127.0.0.1",
                            "--directory", "templates"],
        origin=f"http://127.0.0.1:{port}", ready_timeout=15, capture_timeout=90)))
    return path, port


def _run(tmp_path, monkeypatch, plan, leads, *, profile=None, runs_commands=True, max_tasks=6,
         files=None, roles=None, lead="claude:opus", settings_kw=None, record_complete=True):
    """Leads are pinned to ``lead`` (claude by default: the transport whose
    editing calls run only granted commands, as in Run 18)."""
    from quadratus.session import Session
    monkeypatch.setattr(Session, "_pick_lead", lambda self, spec: lead)
    plan = list(plan)
    overrides = {
        "orchestrator": lambda call, replay: plan.pop(0) if plan else "DONE",
        "lead": lambda call, replay: leads[call.task](call, replay),
        "revision": lambda call, replay: NO_EDIT,
        "design-review": lambda call, replay: "APPROVED",
        "requirements-review": lambda call, replay: "COMPLETE",
        "auditor": lambda call, replay: MET,
        "design-fix": lambda call, replay: NO_EDIT,
        **(roles or {}),
    }
    replay = H.run(tmp_path, monkeypatch, Script(**overrides), files=files or {
        **_design_files(), "templates/index.html": WIDE_PAGE}, max_tasks=max_tasks,
        settings=Settings(backend="cli", **(settings_kw or {})), lead_runs_commands=runs_commands,
        capture_profile=str(profile) if profile else None, record_complete=record_complete)
    replay.findings = H.result_json(replay).get("findings", [])
    return replay


def _no_edit(call, replay):
    return NO_EDIT


def _fix(call, replay):
    H.write(call, {"templates/index.html": FITTING_PAGE})
    return 'Let the toolbar shrink.\nCHANGED: ["templates/index.html"]'


def _lead_prompts(replay):
    return [c.prompt for c in replay.of("lead")]


# -- positive ---------------------------------------------------------------------------

@browser
def test_the_harness_captures_the_audit_and_the_repair_and_resolves_the_debt(tmp_path, monkeypatch):
    profile, port = _profile(tmp_path)
    replay = _run(tmp_path, monkeypatch, [REQS + AUDIT, REPAIR + "\nRESOLVES: F1"],
                  {"t1": _no_edit, "t2": _fix}, profile=profile, runs_commands=False)
    (f1,) = replay.findings
    assert f1["target"] == f"http://127.0.0.1:{port}/index.html" and f1["width"] > 390
    assert f1["status"] == "resolved" and f1["resolved_by"] == "t2", replay.result.error
    assert replay.result.completed
    prompts = _lead_prompts(replay)
    assert all("The harness itself starts the preview" in p for p in prompts)
    assert not any("quadratus.design_evidence" in p or "Start the app" in p for p in prompts)
    assert not _listening(port), "the preview was stopped"


@browser
def test_a_clean_page_needs_no_repair(tmp_path, monkeypatch):
    profile, _ = _profile(tmp_path)
    replay = _run(tmp_path, monkeypatch, [REQS + AUDIT], {"t1": _no_edit}, profile=profile,
                  files={**_design_files(), "templates/index.html": FITTING_PAGE})
    assert replay.findings == [] and replay.result.completed, replay.result.error


@browser
def test_a_design_fix_that_edits_is_recaptured_by_the_harness(tmp_path, monkeypatch):
    profile, _ = _profile(tmp_path)
    fixes = []

    def fix(call, replay):
        fixes.append(call.prompt)
        return _fix(call, replay)
    build = _decl("KIND: frontend standard", dict(REPAIR_SCOPE, capture=CAPTURE), "Add the toolbar.")
    replay = _run(tmp_path, monkeypatch, [REQS + build], {"t1": _no_edit}, profile=profile,
                  roles={"design-fix": fix})
    assert len(fixes) == 1 and "The harness rendered this design task" in fixes[0]
    assert "Do not start servers or run capture commands" in fixes[0]
    assert replay.result.completed, replay.result.error


# -- negative: before any call ------------------------------------------------------------

def test_without_a_profile_a_lead_that_cannot_capture_is_never_invoked(tmp_path, monkeypatch):
    audit = AUDIT.replace(', "capture": ' + json.dumps(CAPTURE), "")
    replay = _run(tmp_path, monkeypatch, [REQS + audit], {}, runs_commands=False)
    assert replay.result.error.startswith("CapabilityUnavailable: task t1 is UI work")
    assert "No call was made" in replay.result.error
    assert not replay.of("lead") and not replay.result.completed


def test_with_a_profile_a_ui_task_without_a_capture_is_sent_back(tmp_path, monkeypatch):
    profile, _ = _profile(tmp_path)
    audit = AUDIT.replace(', "capture": ' + json.dumps(CAPTURE), "")
    replay = _run(tmp_path, monkeypatch, [REQS + audit, REQS + audit, REQS + audit, REQS + audit, REQS + audit],
                  {}, profile=profile)
    orchestrator = [c.prompt for c in replay.of("orchestrator")]
    assert any("must declare what the harness captures" in p for p in orchestrator)
    assert not replay.of("lead")


@browser
def test_a_repair_declaring_another_state_is_sent_back_before_its_lead(tmp_path, monkeypatch):
    profile, _ = _profile(tmp_path)
    other = REPAIR.replace('"/index.html"', '"/other.html"')
    replay = _run(tmp_path, monkeypatch, [REQS + AUDIT, other + "\nRESOLVES: F1", REPAIR + "\nRESOLVES: F1"],
                  {"t1": _no_edit, "t2": _fix}, profile=profile)
    assert any("is not the state F1 was measured in" in c.prompt for c in replay.of("orchestrator"))
    assert [c.task for c in replay.of("lead")] == ["t1", "t2"]


def test_an_unusable_profile_stops_before_any_model_call(tmp_path, monkeypatch):
    path = tmp_path / "profile.json"
    path.write_text(json.dumps(dict(preview=["python", "app.py; rm x"], origin="http://127.0.0.1:5000")))

    def never(call, replay):
        raise AssertionError("no model call may be made")
    with pytest.raises(ValueError, match="no shell syntax"):
        H.run(tmp_path, monkeypatch, never, files=_design_files(), capture_profile=str(path))


# -- negative: after the one call ---------------------------------------------------------

def _denied(text, *commands):
    envelope = json.loads(H.claude_ok(text))
    envelope["permission_denials"] = [dict(tool_name="Bash", tool_use_id=f"u{i}", tool_input={"command": c})
                                      for i, c in enumerate(commands)]
    return json.dumps(envelope)


def test_a_denied_harness_capture_stops_the_task_after_its_one_call(tmp_path, monkeypatch):
    audit = AUDIT.replace(', "capture": ' + json.dumps(CAPTURE), "")
    command = "PYTHONPATH=/pkg python -m quadratus.design_evidence http://localhost:5000/ t1 ."
    replay = _run(tmp_path, monkeypatch, [REQS + audit],
                  {"t1": lambda call, replay: _denied("Could not capture.\nCHANGED: []", command)})
    assert replay.result.error.startswith("CapabilityUnavailable") and "quadratus.design_evidence" in replay.result.error
    assert [c.task for c in replay.of("lead")] == ["t1"], "no continuation, no other seat"
    assert not replay.of("design-fix")


def test_an_incidental_denial_is_recorded_and_the_run_goes_on(tmp_path, monkeypatch):
    audit = AUDIT.replace(', "capture": ' + json.dumps(CAPTURE), "")

    def lead(call, replay):
        H.evidence(Path(call.cwd), call.task, age=0)
        return _denied("Captured the page.\nCHANGED: []", "ls -la /")
    replay = _run(tmp_path, monkeypatch, [REQS + audit], {"t1": lead},
                  files={**_design_files(), "templates/index.html": FITTING_PAGE})
    assert replay.result.completed, replay.result.error
    ledger = (replay.result.run_dir / "invocations.jsonl").read_text()
    assert "permission_denied" in ledger and "ls -la /" in ledger


def test_a_refusal_outranks_a_denial(tmp_path, monkeypatch):
    audit = AUDIT.replace(', "capture": ' + json.dumps(CAPTURE), "")
    refusal = json.loads(H.claude_refusal("cyber"))
    refusal["permission_denials"] = [dict(tool_name="Bash", tool_input={
        "command": "python -m quadratus.design_evidence http://localhost:5000/ t1 ."})]
    replay = _run(tmp_path, monkeypatch, [REQS + audit], {"t1": lambda call, replay: json.dumps(refusal)})
    assert replay.result.error.startswith("ProviderRefusal")
    assert "CapabilityUnavailable" not in replay.result.error


# -- negative: harness capture failures ---------------------------------------------------

@browser
def test_a_preview_that_writes_source_leaves_the_design_unverified(tmp_path, monkeypatch):
    port = _free_port()
    serve = ("import http.server, socketserver, pathlib\n"
             "pathlib.Path('templates/generated.html').write_text('x')\n"
             f"socketserver.TCPServer(('127.0.0.1', {port}), http.server.SimpleHTTPRequestHandler).serve_forever()\n")
    path = tmp_path / "profile.json"
    path.write_text(json.dumps(dict(preview=[sys.executable, "serve.py"], origin=f"http://127.0.0.1:{port}")))
    replay = _run(tmp_path, monkeypatch, [REQS + AUDIT], {"t1": _no_edit}, profile=path,
                  files={**_design_files(), "templates/index.html": FITTING_PAGE, "serve.py": serve})
    assert replay.result.error.startswith("DesignUnverified")
    assert "source changed while the harness previewed" in replay.result.error


def test_a_failed_gate_means_no_capture_and_the_finding_stays_open(tmp_path, monkeypatch):
    profile, port = _profile(tmp_path)
    started = []
    from quadratus import preview
    real = preview.running
    monkeypatch.setattr(preview, "running", lambda *a, **k: started.append(1) or real(*a, **k))

    def breaking(call, replay):
        H.write(call, {"templates/index.html": FITTING_PAGE, "app.py": "def add(a, b):\n    return a - b - 1\n"})
        return 'Fixed.\nCHANGED: ["templates/index.html", "app.py"]'
    scope = dict(REPAIR_SCOPE, capture=CAPTURE,
                 permitted_paths=REPAIR_SCOPE["permitted_paths"] + ["templates/index.html", "app.py"])
    repair = _decl("KIND: frontend standard", scope, "Fix the mobile overflow.")
    replay = _run(tmp_path, monkeypatch, [REQS + repair], {"t1": breaking}, profile=profile,
                  roles={"gate-fix": lambda call, replay: "Could not see why.\nCHANGED: []"})
    assert H.gate_results(replay)[-1] == "FAILED"
    assert not started, "no preview was started for a task whose gate failed"
    assert not replay.result.completed


def _listening(port):
    try:
        with socket.create_connection(("127.0.0.1", port), timeout=0.5):
            return True
    except OSError:
        return False


# -- across preview restarts, with an interaction and task-owned fixtures -------------------

UPLOAD_PAGE = ("<html>" + ICON + "<body><input type=file id=csv>"
               "<table id=rows></table><style>#rows td{{{style}}}</style><script>"
               "csv.onchange = async () => {{ const text = await csv.files[0].text();"
               " rows.innerHTML = '<tr id=done><td>' + text.trim() + '</td></tr>'; }};"
               "</script></body></html>\n")
UPLOAD_STEPS = lambda task: [{"action": "file", "selector": "#csv",  # noqa: E731
                              "path": f".quadratus/capture-fixtures/{task}/sample.csv"},
                             {"action": "wait", "selector": "#done"}]


def _with_fixture(task, then):
    def lead(call, replay):
        H.write(call, {f".quadratus/capture-fixtures/{task}/sample.csv": "a,b\n1,2\n"})
        return then(call, replay)
    return lead


@browser
def test_an_interaction_state_survives_preview_restarts_from_audit_to_repair(tmp_path, monkeypatch):
    profile, port = _profile(tmp_path)
    audit = _decl("KIND: frontend standard",
                  dict(AUDIT_SCOPE, capture={"path": "/index.html", "steps": UPLOAD_STEPS("t1")}),
                  "Audit the preview table.")
    repair = _decl("KIND: frontend standard",
                   dict(REPAIR_SCOPE, capture={"path": "/index.html", "steps": UPLOAD_STEPS("t2")}),
                   "Let the preview table fit.")

    def fix(call, replay):
        H.write(call, {"templates/index.html": UPLOAD_PAGE.format(style="max-width:100%")})
        return 'Let the rows wrap.\nCHANGED: ["templates/index.html"]'
    replay = _run(tmp_path, monkeypatch, [REQS + audit, repair + "\nRESOLVES: F1"],
                  {"t1": _with_fixture("t1", _no_edit), "t2": _with_fixture("t2", fix)}, profile=profile,
                  files={**_design_files(), "templates/index.html": UPLOAD_PAGE.format(style="display:block;width:450px")})
    (f1,) = replay.findings
    assert [s[:2] for s in f1["steps"]] == [["file", "#csv"], ["wait", "#done"]] and f1["steps"][0][2]
    assert f1["status"] == "resolved" and f1["resolved_by"] == "t2", replay.result.error
    assert replay.result.completed


def test_a_harness_capture_may_upload_only_its_own_fixture(tmp_path, monkeypatch):
    profile, _ = _profile(tmp_path)
    borrowed = _decl("KIND: frontend standard",
                     dict(AUDIT_SCOPE, capture={"path": "/index.html", "steps": UPLOAD_STEPS("t9")}),
                     "Audit the preview table.")
    replay = _run(tmp_path, monkeypatch, [REQS + borrowed] * 5, {}, profile=profile)
    assert any("committed project file or this task's own fixture" in c.prompt
               for c in replay.of("orchestrator"))
    assert not replay.of("lead")


def test_a_harness_capture_may_upload_a_committed_project_sample(tmp_path, monkeypatch):
    """Phase-4 rerun on a6c9576: task 2 committed tests/fixtures/<sample>.csv and the
    next task's capture named it; dispatch refused it and the run stalled."""
    profile, _ = _profile(tmp_path)
    steps = [{"action": "file", "selector": "#csv", "path": "tests/fixtures/sample.csv"},
             {"action": "wait", "selector": "#done"}]
    audit = _decl("KIND: frontend standard",
                  dict(AUDIT_SCOPE, capture={"path": "/index.html", "steps": steps}),
                  "Audit the preview table.")
    replay = _run(tmp_path, monkeypatch, [REQS + audit], {"t1": _no_edit}, profile=profile,
                  files={**_design_files(), "templates/index.html": UPLOAD_PAGE.format(style="max-width:100%"),
                         "tests/fixtures/sample.csv": "a,b\n1,2\n"},
                  record_complete=False)
    assert not any("this task's own fixture" in c.prompt for c in replay.of("orchestrator"))
    assert len(replay.of("lead")) == 1, "the task was dispatched with the committed sample"


@browser
def test_a_preview_that_writes_source_as_it_shuts_down_is_caught(tmp_path, monkeypatch):
    port = _free_port()
    serve = ("import http.server, socketserver, pathlib\n"
             f"server = socketserver.TCPServer(('127.0.0.1', {port}), http.server.SimpleHTTPRequestHandler)\n"
             "try:\n    server.serve_forever()\n"
             "finally:\n    pathlib.Path('templates/left-behind.html').write_text('x')\n")
    path = tmp_path / "profile.json"
    path.write_text(json.dumps(dict(preview=[sys.executable, "serve.py"], origin=f"http://127.0.0.1:{port}",
                                    ready_path="/templates/index.html")))
    replay = _run(tmp_path, monkeypatch, [REQS + AUDIT.replace('"/index.html"', '"/templates/index.html"')],
                  {"t1": _no_edit}, profile=path,
                  files={**_design_files(), "templates/index.html": FITTING_PAGE, "serve.py": serve})
    assert replay.result.error.startswith("DesignUnverified")
    assert "source changed while the harness previewed" in replay.result.error


def test_a_capped_call_denied_the_harness_command_is_a_capability_stop(tmp_path, monkeypatch):
    audit = AUDIT.replace(', "capture": ' + json.dumps(CAPTURE), "")
    capped = json.loads(H.claude_cap("Kept trying to capture."))
    capped["permission_denials"] = [dict(tool_name="Bash", tool_input={
        "command": "PYTHONPATH=/pkg python -m quadratus.design_evidence http://localhost:5000/ t1 ."})]
    replay = _run(tmp_path, monkeypatch, [REQS + audit, REQS + audit], {"t1": lambda call, replay: json.dumps(capped)},
                  settings_kw={"lead_max_turns": 14})
    assert replay.result.error.startswith("CapabilityUnavailable") and "turn limit" in replay.result.error
    assert [c.task for c in replay.of("lead")] == ["t1"], "no continuation"


def test_a_malformed_denial_field_changes_nothing(tmp_path, monkeypatch):
    audit = AUDIT.replace(', "capture": ' + json.dumps(CAPTURE), "")

    def lead(call, replay):
        H.evidence(Path(call.cwd), call.task, age=0)
        envelope = json.loads(H.claude_ok("Captured the page.\nCHANGED: []"))
        envelope["permission_denials"] = 42
        return json.dumps(envelope)
    replay = _run(tmp_path, monkeypatch, [REQS + audit], {"t1": lead},
                  files={**_design_files(), "templates/index.html": FITTING_PAGE})
    assert replay.result.completed, replay.result.error
    usage = (replay.result.run_dir / "usage.jsonl").read_text()
    assert usage.strip(), "the call's usage is still metered"


def test_the_generated_capture_command_denied_at_the_cap_is_a_capability_stop(tmp_path, monkeypatch):
    import shlex

    from quadratus import preview
    profile = preview.profile_from_dict(dict(preview=["python", "-m", "http.server"],
                                             origin="http://127.0.0.1:5000"), tmp_path)
    argv = preview.capture_argv(profile, "t1", {"path": "/", "steps": []})
    command = shlex.join(a if a != "{root}" else "/project" for a in argv)
    audit = AUDIT.replace(', "capture": ' + json.dumps(CAPTURE), "")
    capped = json.loads(H.claude_cap("Kept trying to capture."))
    capped["permission_denials"] = [dict(tool_name="Bash", tool_input={"command": command})]
    replay = _run(tmp_path, monkeypatch, [REQS + audit, REQS + audit], {"t1": lambda call, replay: json.dumps(capped)},
                  settings_kw={"lead_max_turns": 14})
    assert replay.result.error.startswith("CapabilityUnavailable")
    assert [c.task for c in replay.of("lead")] == ["t1"]


# -- a review-only lead writes its own fixture (diagnostic run 20260930T020711Z) --------

def test_a_review_only_lead_may_write_its_own_fixture_and_is_told_to(tmp_path, monkeypatch):
    """t2 of the diagnostic run was an audit whose task text named a
    capture-only sample and also said "without editing source"; the lead
    reported CHANGED: [] and never wrote it (Codex, 5903031111). The lead's
    instruction now says the sample is harness state it must write, and the
    write is not a source edit: the audit's scope stays clean and the task
    is not failed for it. The capture itself needs a browser (the @browser
    cases above); here the fixture reaches the capture as a regular file."""
    profile, _ = _profile(tmp_path)
    audit = _decl("KIND: frontend standard",
                  dict(AUDIT_SCOPE, capture={"path": "/index.html", "steps": UPLOAD_STEPS("t1")}),
                  "Audit the preview table without editing source.")
    replay = _run(tmp_path, monkeypatch, [REQS + audit], {"t1": _with_fixture("t1", _no_edit)}, profile=profile,
                  files={**_design_files(), "templates/index.html": UPLOAD_PAGE.format(style="max-width:100%")},
                  record_complete=False)
    prompt = _lead_prompts(replay)[0]
    assert "uploads .quadratus/capture-fixtures/t1/sample.csv into #csv" in prompt
    assert "do not create it" in prompt and "harness asks you for its content" in prompt and "needs no CHANGED entry" in prompt
    assert (replay.project / ".quadratus/capture-fixtures/t1/sample.csv").read_text() == "a,b\n1,2\n"
    task = next(t for t in replay.workflow["tasks"] if t["task_id"] == "t1")
    assert task["closed_as"] == "closed", "writing the fixture is not a scope failure"
    assert task["partial"]["changed"] == [], "harness state is not a source change"
    assert "not a regular file" not in (replay.result.error or "")

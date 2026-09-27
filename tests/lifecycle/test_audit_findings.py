"""Audit findings as requirement debt (Codex, Run 17; contract v3 on #25).

A declared review-only task that finds a measured overflow on otherwise
valid evidence records a finding instead of ending the run. Its COVERS stay
NOT MET until a task naming the finding in RESOLVES verifies fresh renders
of the same page without overflow and is approved with delivered evidence.
Everything that is not that keeps today's stop. Whole-controller replays,
scripted replies; APPROVED here is routing proof, not image judgement.
"""

import json
from pathlib import Path

import pytest

from quadratus.config import Settings
from tests.lifecycle import harness as H
from tests.lifecycle.test_lifecycle_matrix import DESIGN, T2, Script, _design_files

pytestmark = pytest.mark.requirements_ledger

AUDIT_SCOPE = dict(DESIGN, max_lines=1, edits="none")
REPAIR_SCOPE = dict(DESIGN, max_lines=40)
REQS = "REQUIREMENTS:\nR1: the import page works on a phone\nR2: the import button is labelled\n"
AUDIT = "KIND: frontend standard\nSCOPE: " + json.dumps(AUDIT_SCOPE) + "\nAudit the import page.\nCOVERS: R1, R2"
REPAIR = "KIND: frontend standard\nSCOPE: " + json.dumps(REPAIR_SCOPE) + "\nFix the mobile overflow.\nCOVERS: R1, R2"
RECAPTURE = AUDIT.replace("Audit the import page.", "Re-check the import page.")
DOCS = "KIND: docs simple\nSCOPE: " + json.dumps(T2) + "\nDocument add in README.md.\nCOVERS: R1, R2"
MET = "R1: MET - templates/index.html\nR2: MET - templates/index.html"
WIDE = {"mobile": 450}
NO_EDIT = "Nothing to change after review.\nCHANGED: []"


def _capture(**kw):
    def lead(call, replay):
        H.evidence(Path(call.cwd), call.task, age=0, **kw)
        return "Captured the page.\nCHANGED: []"
    return lead


def _repair(**kw):
    def lead(call, replay):
        H.write(call, {"static/style.css": ".toolbar { flex-wrap: wrap; }\n"})
        H.evidence(Path(call.cwd), call.task, age=0, **kw)
        return 'Wrapped the toolbar and re-captured.\nCHANGED: ["static/style.css"]'
    return lead


def _run(tmp_path, monkeypatch, plan, leads, *, review="APPROVED", fix=None, max_tasks=6, audits=None):
    """``plan``: orchestrator replies in order (then DONE); ``leads``: lead handler per task id."""
    plan = list(plan)
    audits = list(audits or [MET] * 8)

    def orchestrator(call, replay):
        return plan.pop(0) if plan else "DONE"

    def lead(call, replay):
        return leads[call.task](call, replay)

    overrides = {
        "orchestrator": orchestrator, "lead": lead,
        "revision": lambda call, replay: NO_EDIT,
        "design-review": review if callable(review) else (lambda call, replay: review),
        "requirements-review": lambda call, replay: "COMPLETE",
        "auditor": lambda call, replay: audits.pop(0) if audits else MET,
        "design-fix": fix or (lambda call, replay: "Captured again.\nCHANGED: []"),
    }
    replay = H.run(tmp_path, monkeypatch, Script(**overrides), files=_design_files(), max_tasks=max_tasks,
                   settings=Settings(backend="cli"))
    replay.findings = H.result_json(replay).get("findings", [])
    return replay


def _orchestrator_prompts(replay):
    return [c.prompt for c in replay.of("orchestrator")]


# -- positive ---------------------------------------------------------------------------

def test_a_clean_audit_records_nothing_and_the_run_can_finish(tmp_path, monkeypatch):
    replay = _run(tmp_path, monkeypatch, [REQS + AUDIT], {"t1": _capture()})
    assert replay.findings == [] and replay.result.completed, replay.result.error


def test_an_overflow_audit_becomes_debt_and_a_repair_resolves_it(tmp_path, monkeypatch):
    replay = _run(tmp_path, monkeypatch, [REQS + AUDIT, REPAIR + "\nRESOLVES: F1"],
                  {"t1": _capture(measured=WIDE), "t2": _repair()})
    assert replay.result.error == "" and replay.result.completed
    (f1,) = replay.findings
    assert f1["id"] == "F1" and f1["task"] == "t1" and f1["requirements"] == ["R1", "R2"]
    assert f1["view"] == "mobile" and f1["width"] == 450 and f1["viewport"] == 390
    assert f1["status"] == "resolved" and f1["resolved_by"] == "t2" and f1["resolution"]["sha256"]
    assert f1["evidence"]["summary"] == ".quadratus/design-evidence/t1/summary.json"
    assert "OPEN AUDIT FINDINGS" in _orchestrator_prompts(replay)[1] and "F1 (found by t1" in _orchestrator_prompts(replay)[1]
    assert not replay.of("design-fix"), "no recapture is spent on a measured fault in an audit"
    assert json.loads((replay.result.run_dir / "findings.json").read_text())[0]["status"] == "resolved"


def test_a_later_clean_recapture_audit_can_resolve_a_finding(tmp_path, monkeypatch):
    replay = _run(tmp_path, monkeypatch, [REQS + AUDIT, RECAPTURE + "\nRESOLVES: F1"],
                  {"t1": _capture(measured=WIDE), "t2": _capture()})
    assert replay.findings[0]["status"] == "resolved" and replay.result.completed


def test_two_findings_on_shared_requirements_close_one_at_a_time(tmp_path, monkeypatch):
    both = {"mobile": 450, "desktop": 1400}
    replay = _run(tmp_path, monkeypatch,
                  [REQS + AUDIT, REPAIR + "\nRESOLVES: F1", RECAPTURE + "\nRESOLVES: F2"],
                  {"t1": _capture(measured=both), "t2": _repair(), "t3": _capture()})
    assert [f["status"] for f in replay.findings] == ["resolved", "resolved"]
    third = _orchestrator_prompts(replay)[2]
    assert "F2 (found by t1" in third and "F1 (found by t1" not in third, "R1/R2 stayed owed to F2"
    assert replay.result.completed


# -- negative ---------------------------------------------------------------------------

@pytest.mark.parametrize("variant", ["stale-identity", "unclean", "missing"])
def test_overflow_with_any_integrity_or_page_problem_is_todays_stop(tmp_path, monkeypatch, variant):
    def lead(call, replay):
        if variant != "missing":
            H.evidence(Path(call.cwd), call.task, age=0, measured=WIDE, clean=variant != "unclean")
        if variant == "stale-identity":
            summary = Path(call.cwd, ".quadratus/design-evidence/t1/summary.json")
            data = json.loads(summary.read_text())
            data["source_fingerprint"] = "0" * 64
            summary.write_text(json.dumps(data))
        return "Captured.\nCHANGED: []"
    replay = _run(tmp_path, monkeypatch, [REQS + AUDIT], {"t1": lead})
    assert replay.findings == []
    assert replay.result.error.startswith("DesignUnverified") and not replay.result.completed


def test_an_unclean_page_alone_is_todays_stop(tmp_path, monkeypatch):
    replay = _run(tmp_path, monkeypatch, [REQS + AUDIT], {"t1": _capture(clean=False)})
    assert replay.findings == [] and replay.result.error.startswith("DesignUnverified")


@pytest.mark.parametrize("resolves,expected", [
    ("RESOLVES: F9", "findings that do not exist: F9"),
    ("RESOLVES:", "RESOLVES names no finding"),
    ("RESOLVES: F1, F1", "names a finding twice"),
])
def test_an_invalid_resolves_is_sent_back_before_any_lead_call(tmp_path, monkeypatch, resolves, expected):
    replay = _run(tmp_path, monkeypatch, [REQS + AUDIT, REPAIR + "\n" + resolves, REPAIR + "\nRESOLVES: F1"],
                  {"t1": _capture(measured=WIDE), "t2": _repair()})
    assert expected in _orchestrator_prompts(replay)[2]
    assert [c.task for c in replay.of("lead")] == ["t1", "t2"], "the refused task never reached a lead"
    assert replay.findings[0]["status"] == "resolved" and replay.result.completed


def test_a_resolves_without_its_requirements_in_covers_is_sent_back(tmp_path, monkeypatch):
    partial = REPAIR.replace("COVERS: R1, R2", "COVERS: R1") + "\nRESOLVES: F1"
    replay = _run(tmp_path, monkeypatch, [REQS + AUDIT, partial, REPAIR + "\nRESOLVES: F1"],
                  {"t1": _capture(measured=WIDE), "t2": _repair()})
    assert "missing: R2" in _orchestrator_prompts(replay)[2]


def test_a_non_ui_task_cannot_resolve_an_overflow(tmp_path, monkeypatch):
    def docs(call, replay):
        H.write(call, {"README.md": "# app\n\ndocumented\n"})
        return 'Documented.\nCHANGED: ["README.md"]'
    replay = _run(tmp_path, monkeypatch, [REQS + AUDIT, DOCS + "\nRESOLVES: F1"],
                  {"t1": _capture(measured=WIDE), "t2": docs})
    assert replay.findings[0]["status"] == "open"
    assert replay.result.error.startswith("FindingsUnresolved: audit findings F1") and not replay.result.completed


def test_clean_renders_of_another_page_do_not_resolve(tmp_path, monkeypatch):
    replay = _run(tmp_path, monkeypatch, [REQS + AUDIT, REPAIR + "\nRESOLVES: F1"],
                  {"t1": _capture(measured=WIDE), "t2": _repair(target="http://127.0.0.1:5000/other")})
    assert replay.findings[0]["status"] == "open"
    assert "rendered http://127.0.0.1:5000/other" in replay.findings[0]["last_attempt"]
    assert replay.result.error.startswith("FindingsUnresolved")


def test_approval_without_delivered_evidence_resolves_nothing(tmp_path, monkeypatch):
    from quadratus import runtime
    monkeypatch.setattr(runtime, "_MAX_EVIDENCE_TOTAL", 1)
    replay = _run(tmp_path, monkeypatch, [REQS + AUDIT, REPAIR + "\nRESOLVES: F1"],
                  {"t1": _capture(measured=WIDE), "t2": _repair()})
    assert not replay.of("design-review")
    assert replay.findings[0]["status"] == "open" and not replay.result.completed


def test_a_repair_that_leaves_the_overflow_stops_as_today(tmp_path, monkeypatch):
    replay = _run(tmp_path, monkeypatch, [REQS + AUDIT, REPAIR + "\nRESOLVES: F1"],
                  {"t1": _capture(measured=WIDE), "t2": _repair(measured=WIDE)})
    assert replay.result.error.startswith("DesignUnverified: task t2")
    assert replay.findings[0]["status"] == "open" and not replay.result.completed


def test_a_source_change_after_resolution_reopens_the_finding_at_done(tmp_path, monkeypatch):
    def docs(call, replay):
        H.write(call, {"README.md": "# app\n\nchanged later\n"})
        return 'Documented.\nCHANGED: ["README.md"]'
    replay = _run(tmp_path, monkeypatch, [REQS + AUDIT, REPAIR + "\nRESOLVES: F1", DOCS],
                  {"t1": _capture(measured=WIDE), "t2": _repair(), "t3": docs}, max_tasks=10)
    f1 = replay.findings[0]
    assert f1["status"] == "open" and "no longer holds" in f1["reopened"]
    assert any("(reopened: its resolving evidence no longer holds" in p for p in _orchestrator_prompts(replay))
    assert replay.result.error.startswith("FindingsUnresolved") and not replay.result.completed


def test_an_unrelated_covering_task_leaves_an_owed_requirement_unmet(tmp_path, monkeypatch):
    def docs(call, replay):
        H.write(call, {"README.md": "# app\n\ndocumented\n"})
        return 'Documented.\nCHANGED: ["README.md"]'
    replay = _run(tmp_path, monkeypatch, [REQS + AUDIT, DOCS], {"t1": _capture(measured=WIDE), "t2": docs})
    assert replay.findings[0]["status"] == "open"
    assert any("Audit findings are still open: F1" in p for p in _orchestrator_prompts(replay))
    assert not replay.result.completed


def test_with_the_ledger_off_an_overflow_audit_is_todays_stop(tmp_path, monkeypatch):
    monkeypatch.setenv("QUADRATUS_REQUIREMENTS_LEDGER", "0")
    audit = AUDIT.replace("\nCOVERS: R1, R2", "")
    replay = _run(tmp_path, monkeypatch, [audit], {"t1": _capture(measured=WIDE)})
    assert replay.findings == [] and replay.result.error.startswith("DesignUnverified")


def test_an_audit_that_edits_source_is_still_a_scope_stop(tmp_path, monkeypatch):
    def lead(call, replay):
        H.write(call, {"static/style.css": "edited by an audit\n"})
        H.evidence(Path(call.cwd), call.task, age=0, measured=WIDE)
        return 'Audited.\nCHANGED: ["static/style.css"]'
    replay = _run(tmp_path, monkeypatch, [REQS + AUDIT], {"t1": lead})
    assert "exceeded its declared scope" in replay.result.error and replay.findings == []


def test_a_refused_audit_lead_is_todays_refusal_stop(tmp_path, monkeypatch):
    replay = _run(tmp_path, monkeypatch, [REQS + AUDIT], {"t1": lambda call, replay: H.claude_refusal("cyber")})
    assert replay.result.error.startswith("ProviderRefusal") and replay.findings == []

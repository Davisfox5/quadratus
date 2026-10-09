"""Audit findings as requirement debt (Codex, Run 17; contract v3 on #25).

A declared review-only task that finds a measured overflow on otherwise
valid evidence records a finding instead of ending the run. Its COVERS stay
NOT MET until a task naming the finding in RESOLVES verifies fresh renders
of the same page without overflow and is approved with delivered evidence.
Everything that is not that keeps today's stop. Whole-controller replays,
scripted replies; APPROVED here is routing proof, not image judgement.
"""

import hashlib
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


def _repair(css=".toolbar { flex-wrap: wrap; }\n", **kw):
    def lead(call, replay):
        H.write(call, {"static/style.css": css})
        H.evidence(Path(call.cwd), call.task, age=0, **kw)
        return 'Wrapped the toolbar and re-captured.\nCHANGED: ["static/style.css"]'
    return lead


def _run(tmp_path, monkeypatch, plan, leads, *, review="APPROVED", fix=None, max_tasks=6, audits=None,
         settings=None, roles=None, record_complete=True):
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
        **(roles or {}),
    }
    replay = H.run(tmp_path, monkeypatch, Script(**overrides), files=_design_files(), max_tasks=max_tasks,
                   settings=settings or Settings(backend="cli"), record_complete=record_complete)
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
    assert "OPEN FINDINGS" in _orchestrator_prompts(replay)[1] and "F1 (found by t1" in _orchestrator_prompts(replay)[1]
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


STATE = [("click", "#import"), ("wait", "#preview-rows")]


def test_a_recapture_in_the_measured_state_resolves_the_finding(tmp_path, monkeypatch):
    replay = _run(tmp_path, monkeypatch, [REQS + AUDIT, RECAPTURE + "\nRESOLVES: F1"],
                  {"t1": _capture(measured=WIDE, steps=STATE), "t2": _capture(steps=STATE)})
    f1 = replay.findings[0]
    assert f1["steps"] == [["click", "#import", None], ["wait", "#preview-rows", None]]
    assert set(f1["evidence"]["screenshots"]) == {"desktop", "mobile"} and all(f1["evidence"]["screenshots"].values())
    assert f1["status"] == "resolved" and replay.result.completed


def test_two_editing_repairs_reopen_the_first_until_its_state_is_rechecked(tmp_path, monkeypatch):
    """Codex worker review: F2's edit makes F1's resolving renders stale."""
    both = {"mobile": 450, "desktop": 1400}
    replay = _run(tmp_path, monkeypatch,
                  [REQS + AUDIT, REPAIR + "\nRESOLVES: F1", REPAIR + "\nRESOLVES: F2", "DONE",
                   RECAPTURE + "\nRESOLVES: F1"],
                  {"t1": _capture(measured=both), "t2": _repair(), "t3": _repair(css=".toolbar { gap: 0; }\n"),
                   "t4": _capture()}, max_tasks=8)
    f1, f2 = replay.findings
    assert f2["resolved_by"] == "t3" and f1["resolved_by"] == "t4"
    assert any("F1 (found by t1" in p and "reopened" in p for p in _orchestrator_prompts(replay))
    assert replay.result.completed


# -- negative ---------------------------------------------------------------------------

def test_a_recapture_of_the_same_address_in_another_state_does_not_resolve(tmp_path, monkeypatch):
    """Codex worker review: one URL shows the list, an empty dialog and the preview."""
    replay = _run(tmp_path, monkeypatch, [REQS + AUDIT, RECAPTURE + "\nRESOLVES: F1", REPAIR + "\nRESOLVES: F1"],
                  {"t1": _capture(measured=WIDE, steps=STATE), "t2": _capture(), "t3": _repair(steps=STATE)})
    f1 = replay.findings[0]
    assert f1["status"] == "open" and "different state" in f1["last_attempt"]
    assert replay.result.error.startswith("FindingsUnresolved: task t2") and not replay.result.completed
    assert [c.task for c in replay.of("lead")] == ["t1", "t2"], "no automatic second repair"


@pytest.mark.parametrize("replacement, reason", [
    (dict(target="http://127.0.0.1:5000/other"), "different state"),
    (dict(steps=STATE, measured={"mobile": 391}), "replaced after they were reviewed"),
])
def test_resolving_renders_replaced_after_review_reopen_at_done(tmp_path, monkeypatch, replacement, reason):
    """Codex review of e47c7ed: a later no-source task swapped t2's renders and DONE completed."""
    def swap(call, replay):
        H.evidence(Path(call.cwd), call.task, age=0)
        H.evidence(Path(call.cwd), "t2", age=0, **replacement)
        return "Captured the page.\nCHANGED: []"
    later = RECAPTURE.replace("Re-check the import page.", "Look over the page once more.")
    replay = _run(tmp_path, monkeypatch, [REQS + AUDIT, REPAIR + "\nRESOLVES: F1", later],
                  {"t1": _capture(measured=WIDE, steps=STATE), "t2": _repair(steps=STATE), "t3": swap},
                  max_tasks=8)
    f1 = replay.findings[0]
    assert f1["status"] == "open" and reason in f1["reopened"]
    assert replay.result.error.startswith("FindingsUnresolved") and not replay.result.completed


def test_a_failed_gate_keeps_the_finding_open_and_stays_the_primary_stop(tmp_path, monkeypatch):
    """Codex review of e47c7ed: checks [pass, fail] and F1 resolved by t2."""
    def breaking(call, replay):
        H.write(call, {"static/style.css": ".toolbar { flex-wrap: wrap; }\n",
                       "app.py": "def add(a, b):\n    return a - b - 1\n"})
        H.evidence(Path(call.cwd), call.task, age=0)
        return 'Wrapped the toolbar and re-captured.\nCHANGED: ["static/style.css", "app.py"]'
    scope = dict(REPAIR_SCOPE, permitted_paths=REPAIR_SCOPE["permitted_paths"] + ["app.py"])
    repair = REPAIR.replace(json.dumps(REPAIR_SCOPE), json.dumps(scope))
    replay = _run(tmp_path, monkeypatch, [REQS + AUDIT, repair + "\nRESOLVES: F1"],
                  {"t1": _capture(measured=WIDE), "t2": breaking},
                  roles={"gate-fix": lambda call, replay: "Could not see why.\nCHANGED: []"})
    f1, f2 = replay.findings
    assert H.gate_results(replay)[-1] == "FAILED"
    assert f1["status"] == "open" and "integration gate failed" in f1["last_attempt"]
    # The failed gate is t2's own debt now (check debt, 2026-09-30), and the
    # explicit repair that did not establish acceptance is still the stop.
    assert f2["task"] == "t2" and f2["kind"] == "check.failed" and f2["status"] == "open"
    assert replay.result.error.startswith("FindingsUnresolved: task t2 named RESOLVES F1")
    assert not replay.result.completed


def test_an_audit_whose_evidence_cannot_be_delivered_creates_no_debt(tmp_path, monkeypatch):
    from quadratus import runtime
    monkeypatch.setattr(runtime, "_MAX_EVIDENCE_TOTAL", 1)
    replay = _run(tmp_path, monkeypatch, [REQS + AUDIT], {"t1": _capture(measured=WIDE)})
    assert replay.findings == []
    assert replay.result.error.startswith("DesignUnverified") and "could not be delivered" in replay.result.error


@pytest.mark.parametrize("png, measured, debt", [
    (1280, 450, False),  # a desktop-width image filed as mobile
    (520, 520, True),    # a genuine full-page capture of an overflowing page
])
def test_screenshot_and_measured_width_must_agree_for_debt(tmp_path, monkeypatch, png, measured, debt):
    replay = _run(tmp_path, monkeypatch, [REQS + AUDIT],
                  {"t1": _capture(measured={"mobile": measured}, png={"mobile": png})})
    assert bool(replay.findings) is debt
    if not debt:
        assert "does not match the 450px page width" in replay.result.error


def test_a_scope_stop_in_a_resolving_task_is_written_into_the_finding(tmp_path, monkeypatch):
    def rogue(call, replay):
        H.write(call, {"static/style.css": ".toolbar { flex-wrap: wrap; }\n", "README.md": "# app\n\nx\n"})
        H.evidence(Path(call.cwd), call.task, age=0)
        return 'Done.\nCHANGED: ["static/style.css", "README.md"]'
    replay = _run(tmp_path, monkeypatch, [REQS + AUDIT, REPAIR + "\nRESOLVES: F1"],
                  {"t1": _capture(measured=WIDE), "t2": rogue})
    f1 = replay.findings[0]
    t2 = next(t for t in replay.workflow["tasks"] if t["task_id"] == "t2")
    assert t2["closed_as"] == "failed", "the repair fails on its scope; the run goes on"
    assert replay.result.error and not replay.result.completed
    assert f1["status"] == "open" and f1["unresolved_reason"].startswith("open when")
    assert f1["last_attempt"]


def test_the_task_cap_with_an_open_finding_is_a_named_stop(tmp_path, monkeypatch):
    replay = _run(tmp_path, monkeypatch, [REQS + AUDIT], {"t1": _capture(measured=WIDE)}, max_tasks=1)
    assert replay.result.error.startswith("FindingsUnresolved") and "task cap" in replay.result.error
    assert replay.findings[0]["unresolved_reason"] == "open when the task cap was reached"
    assert not replay.result.completed


def test_a_stale_resolution_at_the_final_slot_is_a_named_stop(tmp_path, monkeypatch):
    def docs(call, replay):
        H.write(call, {"README.md": "# app\n\nchanged later\n"})
        return 'Documented.\nCHANGED: ["README.md"]'
    replay = _run(tmp_path, monkeypatch, [REQS + AUDIT, REPAIR + "\nRESOLVES: F1", DOCS],
                  {"t1": _capture(measured=WIDE), "t2": _repair(), "t3": docs}, max_tasks=3)
    assert replay.findings[0]["status"] == "open" and "no longer holds" in replay.findings[0]["reopened"]
    assert replay.result.error.startswith("FindingsUnresolved") and not replay.result.completed


def test_findings_are_kept_when_a_later_lead_is_refused(tmp_path, monkeypatch):
    replay = _run(tmp_path, monkeypatch, [REQS + AUDIT, REPAIR + "\nRESOLVES: F1"],
                  {"t1": _capture(measured=WIDE), "t2": lambda call, replay: H.claude_refusal("cyber")})
    assert replay.result.error.startswith("ProviderRefusal")
    (f1,) = replay.findings
    assert f1["status"] == "open" and f1["evidence"]["sha256"]


def test_a_capped_repair_leaves_the_finding_open_until_a_continuation_resolves_it(tmp_path, monkeypatch):
    def capped(call, replay):
        H.write(call, {"static/style.css": ".toolbar { flex-wrap: wrap; }\n"})
        return H.claude_cap("Wrapped the toolbar; not captured yet.", num_turns=14)
    finish = REPAIR.replace("Fix the mobile overflow.", "Finish the capped repair.") + "\nCONTINUES: t2\nRESOLVES: F1"
    replay = _run(tmp_path, monkeypatch, [REQS + AUDIT, REPAIR + "\nRESOLVES: F1", finish],
                  {"t1": _capture(measured=WIDE), "t2": capped, "t3": _capture()},
                  settings=Settings(backend="cli", lead_max_turns=14))
    f1 = replay.findings[0]
    assert f1["status"] == "resolved" and f1["resolved_by"] == "t3"
    assert "t2" in json.dumps(H.result_json(replay)["turn_limited_tasks"]), "t2 really was capped"
    assert replay.result.completed, replay.result.error


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
    ("RESOLVES: F1, F1x", "findings that do not exist: F1x"),
    ("RESOLVES: F1\nRESOLVES: F9", "RESOLVES appears on more than one line"),
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


def test_a_non_ui_task_naming_resolves_is_sent_back_before_its_lead(tmp_path, monkeypatch):
    """Codex review of e47c7ed: it used to run, leave F1 open, and let another repair run."""
    replay = _run(tmp_path, monkeypatch, [REQS + AUDIT, DOCS + "\nRESOLVES: F1"], {"t1": _capture(measured=WIDE)})
    assert "RESOLVES needs a UI task" in _orchestrator_prompts(replay)[2]
    assert [c.task for c in replay.of("lead")] == ["t1"]
    assert replay.findings[0]["status"] == "open" and not replay.result.completed


def test_clean_renders_of_another_page_do_not_resolve(tmp_path, monkeypatch):
    replay = _run(tmp_path, monkeypatch, [REQS + AUDIT, REPAIR + "\nRESOLVES: F1"],
                  {"t1": _capture(measured=WIDE), "t2": _repair(target="http://127.0.0.1:5000/other")})
    assert replay.findings[0]["status"] == "open"
    assert "different state: http://127.0.0.1:5000/other" in replay.findings[0]["last_attempt"]
    assert replay.result.error.startswith("FindingsUnresolved: task t2 named RESOLVES F1")


def test_approval_without_delivered_evidence_resolves_nothing(tmp_path, monkeypatch):
    from quadratus import runtime
    repair = _repair()

    def starved(call, replay):
        monkeypatch.setattr(runtime, "_MAX_EVIDENCE_TOTAL", 1)
        return repair(call, replay)
    replay = _run(tmp_path, monkeypatch, [REQS + AUDIT, REPAIR + "\nRESOLVES: F1", REPAIR + "\nRESOLVES: F1"],
                  {"t1": _capture(measured=WIDE), "t2": starved})
    assert not replay.of("design-review")
    assert replay.findings[0]["status"] == "open" and not replay.result.completed
    assert [c.task for c in replay.of("lead")] == ["t1", "t2"], "no second repair after a failed attempt"


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


def test_an_audit_that_edits_source_fails_the_task_and_records_no_finding(tmp_path, monkeypatch):
    def lead(call, replay):
        H.write(call, {"static/style.css": "edited by an audit\n"})
        H.evidence(Path(call.cwd), call.task, age=0, measured=WIDE)
        return 'Audited.\nCHANGED: ["static/style.css"]'
    replay = _run(tmp_path, monkeypatch, [REQS + AUDIT], {"t1": lead})
    t1 = next(t for t in replay.workflow["tasks"] if t["task_id"] == "t1")
    assert t1["closed_as"] == "failed"
    assert any(f["kind"] == "failed" and "exceeded its declared scope" in f["detail"] for f in t1["facts"])
    assert not replay.result.completed and replay.findings == []


def test_a_refused_audit_lead_is_todays_refusal_stop(tmp_path, monkeypatch):
    replay = _run(tmp_path, monkeypatch, [REQS + AUDIT], {"t1": lambda call, replay: H.claude_refusal("cyber")})
    assert replay.result.error.startswith("ProviderRefusal") and replay.findings == []


# -- approval-to-settlement interval and exception exits (Codex review of dd17a1d) --------

@pytest.mark.parametrize("mutation, reason", [
    ("evidence", "replaced after they were reviewed"),
    ("json", "replaced after they were reviewed"),
    ("source", "different source tree"),
    ("state", "different state"),
])
def test_renders_changed_between_approval_and_settlement_do_not_resolve(tmp_path, monkeypatch, mutation, reason):
    """A synthetic write in the interval: the controller invariant, not observed vendor behaviour."""
    from quadratus.session import Session
    settle = Session._settle_resolution

    def mutated(self, spec, *args):
        root = Path(self.project)
        if spec.task_id != "t2":
            pass
        elif mutation == "evidence":
            H.evidence(root, spec.task_id, age=0, measured={"mobile": 391})
        elif mutation == "json":
            # Only the render record changes (Codex review of 2e57e94: the
            # approval snapshot covered summary and screenshots alone).
            record = root / ".quadratus" / "design-evidence" / spec.task_id / "desktop" / "evidence.json"
            record.write_text(record.read_text() + " ")
        elif mutation == "source":
            (root / "README.md").write_text("# app\n\nchanged in the interval\n")
        else:
            H.evidence(root, spec.task_id, age=0, target="http://127.0.0.1:5000/other")
        return settle(self, spec, *args)
    monkeypatch.setattr(Session, "_settle_resolution", mutated)
    replay = _run(tmp_path, monkeypatch, [REQS + AUDIT, REPAIR + "\nRESOLVES: F1"],
                  {"t1": _capture(measured=WIDE), "t2": _repair()})
    f1 = replay.findings[0]
    assert f1["status"] == "open" and "did not hold until it closed" in f1["last_attempt"]
    assert reason in f1["last_attempt"]
    if mutation in ("evidence", "json"):
        # Map J9b: the approved digests no longer hold, an observed identity
        # mismatch (was FindingsUnresolved before the J9b follow-up).
        _settlement_mismatch(replay)
    else:
        assert replay.result.error.startswith("FindingsUnresolved: task t2") and not replay.result.completed
    assert len(replay.of("design-review")) == 1, "no second review"


def _settlement_mismatch(replay):
    """The named integrity stop for approved renders replaced before settlement."""
    result = replay.result
    assert result.error.startswith("EvidenceIdentityMismatch: task t2: the renders the design review "
                                   "approved were replaced before settlement"), result.error
    assert not result.completed
    stop = replay.workflow["run"]["facts"][-1]
    assert stop["kind"] == "integrity" and stop["legacy"] == "EvidenceIdentityMismatch", stop
    assert not [c for c in replay.of("design-fix") if c.task == "t2"], "no repair or recapture"
    (text,) = replay.artifact_texts("evidence-identity")
    record = json.loads(text)
    assert record["task"] == "t2" and record["comparison"].startswith("settlement")
    assert set(record["approved"]) == set(record["observed"])
    assert record["differing"] == sorted(p for p, d in record["approved"].items() if record["observed"][p] != d)
    assert record["differing"], "a positive digest comparison"
    assert record["approved_bytes_retained"] is False and "cannot be reconstructed" in record["note"]
    folder = replay.project / ".quadratus" / "design-evidence" / "t2"
    on_disk = {rel: hashlib.sha256((replay.project / rel).read_bytes()).hexdigest() for rel in record["observed"]}
    assert on_disk == record["observed"], "the changed evidence is left as found"
    assert folder.is_dir()


def _unmeasurable_scope_on(monkeypatch, task_id):
    """A run-level stop after a source change: the task's edits cannot be
    measured (since J39 an ordinary scope overrun fails only the task)."""
    from quadratus.session import Session
    original = Session._assess_scope
    monkeypatch.setattr(Session, "_assess_scope",
                        lambda self, spec, task, before: (None if spec.task_id == task_id
                                                          else original(self, spec, task, before)))


def test_an_exception_after_a_source_change_reopens_an_earlier_resolution(tmp_path, monkeypatch):
    def rogue(call, replay):
        H.write(call, {"README.md": "# app\n\nx\n", "templates/index.html": "<p>moved</p>\n"})
        return 'Documented.\nCHANGED: ["README.md", "templates/index.html"]'
    _unmeasurable_scope_on(monkeypatch, "t3")
    replay = _run(tmp_path, monkeypatch, [REQS + AUDIT, REPAIR + "\nRESOLVES: F1", DOCS],
                  {"t1": _capture(measured=WIDE), "t2": _repair(), "t3": rogue}, max_tasks=8)
    f1 = replay.findings[0]
    assert replay.result.error.startswith("PartialWorkStopped: Scope could not be measured")
    assert f1["status"] == "open" and "no longer holds" in f1["reopened"]
    assert f1["unresolved_reason"].startswith("open when the run stopped")


def test_a_scope_overrun_after_a_resolution_is_rechecked_at_done(tmp_path, monkeypatch):
    """J39: the rogue task fails, the run goes on, and DONE finds the
    resolved finding's target changed underneath it."""
    def rogue(call, replay):
        H.write(call, {"README.md": "# app\n\nx\n", "templates/index.html": "<p>moved</p>\n"})
        return 'Documented.\nCHANGED: ["README.md", "templates/index.html"]'
    replay = _run(tmp_path, monkeypatch, [REQS + AUDIT, REPAIR + "\nRESOLVES: F1", DOCS],
                  {"t1": _capture(measured=WIDE), "t2": _repair(), "t3": rogue}, max_tasks=8)
    t3 = next(t for t in replay.workflow["tasks"] if t["task_id"] == "t3")
    assert t3["closed_as"] == "failed"
    f1 = replay.findings[0]
    assert f1["status"] == "open" and "no longer holds" in f1["reopened"]
    assert not replay.result.completed


def test_an_exception_with_source_unchanged_keeps_a_valid_resolution(tmp_path, monkeypatch):
    replay = _run(tmp_path, monkeypatch, [REQS + AUDIT, REPAIR + "\nRESOLVES: F1", DOCS],
                  {"t1": _capture(measured=WIDE), "t2": _repair(),
                   "t3": lambda call, replay: H.claude_refusal("cyber")}, max_tasks=8)
    assert replay.result.error.startswith("Provider"), "the docs lead's failure is the stop"
    f1 = replay.findings[0]
    assert f1["status"] == "resolved" and f1["resolved_by"] == "t2"


# -- review-time identity and failed finalisation (Codex review of ee7e62b) ----------------

@pytest.mark.parametrize("changed", ["mobile/page.png", "summary.json"])
def test_renders_changed_while_the_review_is_in_flight_do_not_resolve(tmp_path, monkeypatch, changed):
    """The reviewer read the snapshotted copy; the project's file changed before it answered."""
    def review(call, replay):
        path = replay.project / ".quadratus" / "design-evidence" / call.task / changed
        path.write_bytes(path.read_bytes() + (b"\0" if changed.endswith(".png") else b" "))
        return "APPROVED"
    replay = _run(tmp_path, monkeypatch, [REQS + AUDIT, REPAIR + "\nRESOLVES: F1"],
                  {"t1": _capture(measured=WIDE), "t2": _repair()}, review=review)
    f1 = replay.findings[0]
    assert f1["status"] == "open" and "replaced after they were reviewed" in f1["last_attempt"]
    # Map J9b: was FindingsUnresolved before the J9b follow-up.
    _settlement_mismatch(replay)
    assert len(replay.of("design-review")) == 1


def test_an_unreadable_digest_at_settlement_is_not_an_observed_mismatch(tmp_path, monkeypatch):
    """Map J9b: only digests actually read are compared. A file the settlement
    read could not digest stays today's unverified stop, never integrity."""
    from quadratus.session import Session
    settle, state = Session._settle_resolution, Session._capture_state

    def unreadable(self, task_id):
        target, steps, evidence = state(self, task_id)
        return target, steps, dict(evidence, screenshots=dict(evidence["screenshots"], mobile=None))

    def settling(self, spec, *args):
        if spec.task_id == "t2":
            monkeypatch.setattr(Session, "_capture_state", unreadable)
        try:
            return settle(self, spec, *args)
        finally:
            monkeypatch.setattr(Session, "_capture_state", state)
    monkeypatch.setattr(Session, "_settle_resolution", settling)
    replay = _run(tmp_path, monkeypatch, [REQS + AUDIT, REPAIR + "\nRESOLVES: F1"],
                  {"t1": _capture(measured=WIDE), "t2": _repair()})
    f1 = replay.findings[0]
    assert f1["status"] == "open" and "replaced after they were reviewed" in f1["last_attempt"]
    assert replay.result.error.startswith("FindingsUnresolved: task t2") and not replay.result.completed
    assert not replay.artifact_texts("evidence-identity")


def test_a_file_changed_between_snapshot_and_copy_is_not_delivered(tmp_path, monkeypatch):
    from quadratus import runtime
    furnish = runtime._furnish_evidence

    def racing(root, directory, paths, **kw):
        if kw.get("expected"):
            png = Path(root) / ".quadratus" / "design-evidence" / "t2" / "mobile" / "page.png"
            png.write_bytes(png.read_bytes() + b"\0")
        return furnish(root, directory, paths, **kw)
    monkeypatch.setattr(runtime, "_furnish_evidence", racing)
    replay = _run(tmp_path, monkeypatch, [REQS + AUDIT, REPAIR + "\nRESOLVES: F1"],
                  {"t1": _capture(measured=WIDE), "t2": _repair()})
    assert not replay.of("design-review"), "refused before the reviewer was asked"
    assert replay.findings[0]["status"] == "open" and not replay.result.completed


def test_a_failed_recheck_on_an_exception_exit_distrusts_resolutions(tmp_path, monkeypatch):
    from quadratus.session import Session

    def refuse(self):
        raise OSError("synthetic finalisation refusal")
    monkeypatch.setattr(Session, "_recheck_resolved_findings", refuse)

    def rogue(call, replay):
        H.write(call, {"README.md": "# app\n\nx\n", "templates/index.html": "<p>moved</p>\n"})
        return 'Documented.\nCHANGED: ["README.md", "templates/index.html"]'
    _unmeasurable_scope_on(monkeypatch, "t3")
    replay = _run(tmp_path, monkeypatch, [REQS + AUDIT, REPAIR + "\nRESOLVES: F1", DOCS],
                  {"t1": _capture(measured=WIDE), "t2": _repair(), "t3": rogue}, max_tasks=8)
    f1 = replay.findings[0]
    assert replay.result.error and "synthetic" not in replay.result.error, "the original stop is reported"
    assert f1["status"] == "open" and f1["reopened"].startswith("unverified")
    assert H.result_json(replay)["requirements"]["status"]["R1"].startswith("NOT MET")

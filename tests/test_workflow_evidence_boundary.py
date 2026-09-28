"""Evidence and operator boundaries, adversarially, through the whole controller.

Candidate regression controls (#25, "Opus evidence" lane) for four
obligations of docs/workflow-map.md and the shared plan:

- mandatory evidence identity: what the design reviewer approved is, byte
  for byte, what was delivered to it and what the run records;
- delivery and reviewer acknowledgment: a set that did not reach the
  reviewer, or a response that is not an exact APPROVED, never satisfies the
  ``delivered`` / ``reviewer`` edges and never completes a run;
- stale, tampered or wrong-source evidence: it is never approved;
- operator capability failures: a runner or preview that fails for reasons
  outside the application gets no repair call, on every re-check path, not
  only the first gate.

Every case drives ``run_project`` through ``tests.lifecycle.harness``: only
the vendor CLI launch is faked, and parity and completeness of the typed
record are asserted on every replay. Faults are injected in the test's own
environment (files, a check wrapper, a preview command) or by wrapping a
copy/snapshot step, never by patching a decision. Scripted replies prove
routing, not model judgement. Existing coverage is extended, not repeated:
see docs/workflow-evidence-boundary.md for the map to the existing tests.
"""

import hashlib
import json
import sys
from pathlib import Path

import pytest

from quadratus.design_evidence import evidence_dir
from tests.lifecycle import harness as H
from tests.lifecycle.test_lifecycle_matrix import (
    DECL_DESIGN,
    DECL_T1,
    DECL_T2,
    FILES,
    FILES_OK,
    Script,
    _design_files,
    _design_script,
)

EVIDENCE = [".quadratus/design-evidence/t1/desktop/page.png",
            ".quadratus/design-evidence/t1/mobile/page.png",
            ".quadratus/design-evidence/t1/summary.json"]


# -- helpers ----------------------------------------------------------------------

def _task(replay, task_id):
    return next(t for t in replay.workflow["tasks"] if t["task_id"] == task_id)


def _active(task):
    return [f["kind"] for f in task["facts"] if f["terminal"] and not f["recovered"]]


def _stop(replay):
    return replay.workflow["run"]["facts"][-1]


def _sha(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _then_done(first):
    def orchestrator(call, replay):
        return first if len(replay.of("orchestrator")) == 1 else "DONE"
    return orchestrator


def _design_run(tmp_path, monkeypatch, *, review="APPROVED", orchestrator=None, **roles):
    """The lifecycle matrix's design task (draft renders outdated by the
    revision, recaptured once by the design-fix, then the cross-vendor final
    review), led by claude so the reviewer is another vendor."""
    from quadratus.session import Session
    monkeypatch.setattr(Session, "_pick_lead", lambda self, spec: "claude:opus")
    script = _design_script("Renders refreshed.\nCHANGED: []",
                            review=review if isinstance(review, str) else "unused")
    if callable(review):
        script.overrides["design-review"] = review
    script.overrides["orchestrator"] = orchestrator or _then_done(DECL_DESIGN)
    script.overrides.update(roles)
    return H.run(tmp_path, monkeypatch, script, files=_design_files(), max_tasks=3)


def _not_delivered(replay, why):
    """No verdict was taken for a set the reviewer did not receive."""
    assert not replay.of("design-review"), "no reviewer model call for an undelivered set"
    t1 = _task(replay, "t1")
    assert t1["edges"]["evidence"] is True, "the renders themselves qualified"
    assert t1["edges"]["delivered"] is False and t1["edges"].get("reviewer") is not True
    assert t1["delivery"] is None, "nothing is recorded as delivered"
    assert {"delivered", "reviewer"} <= set(t1["unsatisfied"])
    assert "unverified" in _active(t1)
    verdict = json.loads(replay.artifact_texts("design-evidence")[0])["final_review"]["verdict"]
    assert verdict.startswith("BLOCKING") and why in verdict, verdict
    assert not replay.result.completed
    assert replay.result.error.startswith("FindingsOpen:") and "Task t1 design: BLOCKING" in replay.result.error
    return t1


# -- 1. delivery: a set the reviewer did not receive is never reviewed -----------

def test_a_copy_that_drops_a_file_leaves_delivery_and_reviewer_unsatisfied(tmp_path, monkeypatch):
    """The lifecycle matrix pins the verdict text for this path; this pins
    the typed edges, the absent delivery record and the named stop."""
    from quadratus import runtime
    real = runtime._furnish_evidence
    monkeypatch.setattr(runtime, "_furnish_evidence",
                        lambda root, directory, paths, **kw: real(root, directory, list(paths)[:1], **kw))
    _not_delivered(_design_run(tmp_path, monkeypatch), "was not all delivered to the review copy")


def test_renders_rewritten_between_the_snapshot_and_the_copy_are_not_delivered(tmp_path, monkeypatch):
    """Hash-bound delivery: the session snapshots sha256 per file before the
    review call; bytes that differ at copy time are not copied, so a
    reviewer can never approve renders other than the ones on record."""
    from quadratus import runtime
    real = runtime._furnish_evidence
    swapped = {}

    def furnish(root, directory, paths, **kw):
        if kw.get("expected") and not swapped:
            shot = evidence_dir(Path(root), "t1") / "desktop" / "page.png"
            before = shot.read_bytes()
            H.evidence(Path(root), "t1", age=H.FRESH, png={"desktop": 1300})   # other valid bytes
            swapped.update(before=before, after=shot.read_bytes())
        return real(root, directory, paths, **kw)
    monkeypatch.setattr(runtime, "_furnish_evidence", furnish)
    replay = _design_run(tmp_path, monkeypatch)
    assert swapped and swapped["before"] != swapped["after"], "the fault was injected"
    _not_delivered(replay, ".quadratus/design-evidence/t1/desktop/page.png")
    shot = evidence_dir(replay.project, "t1") / "desktop" / "page.png"
    assert shot.read_bytes() == swapped["after"], "the changed render is left as found"


def test_renders_changed_while_being_prepared_for_review_are_not_delivered(tmp_path, monkeypatch):
    """The file hashes and the capture snapshot are taken separately; a
    change between them refuses the review before any copy or model call."""
    from quadratus.session import Session
    real = Session._capture_state
    moved = []

    def capture_state(self, task_id):
        if not moved:
            moved.append(task_id)
            H.evidence(Path(self.project), task_id, age=H.FRESH, png={"mobile": 391})
        return real(self, task_id)
    monkeypatch.setattr(Session, "_capture_state", capture_state)
    _not_delivered(_design_run(tmp_path, monkeypatch), "changed while they were being prepared for review")


# -- 2. reviewer acknowledgment: only an exact APPROVED counts --------------------

@pytest.mark.parametrize("reply", [
    "APPROVED\nBLOCKING: the button is clipped at mobile width",
    "BLOCKING: the button is clipped\nAPPROVED",
    "Approved.",
    "approved",
    "APPROVED.",
    "LGTM",
    "APPROVED, with minor nits",
    "The renders look fine to me.",
])
def test_anything_but_an_exact_approval_leaves_the_reviewer_edge_unsatisfied(tmp_path, monkeypatch, reply):
    replay = _design_run(tmp_path, monkeypatch, review=reply)
    assert len(replay.of("design-review")) == 1, "one review call; an unclear verdict is not re-asked"
    t1 = _task(replay, "t1")
    assert t1["edges"]["delivered"] is True, "it was delivered; only the acknowledgment failed"
    assert t1["edges"]["reviewer"] is False and "reviewer" in t1["unsatisfied"]
    assert "unverified" in _active(t1)
    assert not replay.result.completed and replay.result.error.startswith("FindingsOpen:")


def test_an_exact_approval_with_surrounding_whitespace_is_an_approval(tmp_path, monkeypatch):
    """The positive boundary: the one accepted form, as a reviewer sends it."""
    replay = _design_run(tmp_path, monkeypatch, review="\n  APPROVED  \n")
    t1 = _task(replay, "t1")
    assert t1["edges"]["reviewer"] is True and t1["unsatisfied"] == []
    assert replay.result.completed, replay.result.error


# -- 3. identity: approved == delivered == recorded ---------------------------------

def test_the_recorded_delivery_is_the_bytes_the_reviewer_read_and_the_bytes_kept(tmp_path, monkeypatch):
    seen = {}

    def review(call, replay):
        cwd = Path(call.cwd)
        seen.update({rel: _sha(cwd / rel) for rel in EVIDENCE if (cwd / rel).is_file()})
        return "APPROVED"
    replay = _design_run(tmp_path, monkeypatch, review=review)
    t1 = _task(replay, "t1")
    assert replay.result.completed, replay.result.error
    reviewer = replay.of("design-review")[0]
    assert t1["delivery"]["reviewer"].endswith(reviewer.model) and reviewer.vendor != "claude"
    assert sorted(seen) == EVIDENCE, "every mandatory file reached the reviewer"
    assert t1["delivery"]["files"] == seen, "the record names exactly what the reviewer read"
    assert {rel: _sha(replay.project / rel) for rel in EVIDENCE} == seen, "and what the project keeps"


def test_an_approved_tasks_renders_changed_by_a_later_task_diverge_from_the_record(tmp_path, monkeypatch):
    """Observation, not a contract: after t1 is approved, t2's lead
    rewrites t1's screenshot (evidence is excluded from source, so CHANGED
    cannot see it). The record keeps the approved digest, so the change is
    detectable offline; whether DONE must refuse it is an open question
    posted on #25 (docs/workflow-evidence-boundary.md, gap E2)."""
    shot = ".quadratus/design-evidence/t1/desktop/page.png"

    def orchestrator(call, replay):
        return {1: DECL_DESIGN, 2: DECL_T2}.get(len(replay.of("orchestrator")), "DONE")

    def lead(call, replay):
        if call.task == "t1":
            return _design_script("unused").overrides["lead"](call, replay)
        H.evidence(Path(call.cwd), "t1", age=H.FRESH, png={"desktop": 1300})
        return Script()._lead(call, replay)
    replay = _design_run(tmp_path, monkeypatch, orchestrator=orchestrator, lead=lead)
    t1 = _task(replay, "t1")
    assert t1["edges"]["reviewer"] is True and shot in t1["delivery"]["files"]
    assert _sha(replay.project / shot) != t1["delivery"]["files"][shot], "the record still names the approved bytes"
    # Today's route, pinned so a change to it is deliberate:
    assert replay.result.completed, replay.result.error


# -- 4. operator failures on every re-check path: no repair call -------------------

#
# The check is the harness's own (H.GATE). The environment is lost through an
# unusable pytest option in PYTEST_ADDOPTS, which the check process inherits:
# the runner exits with a usage error before collecting any test and writes
# no report, as a runner does when its environment is broken. Nothing in the
# project changes for it.

def _lose_environment(monkeypatch):
    monkeypatch.setenv("PYTEST_ADDOPTS", "--quadratus-environment-lost")


def _handed_off(replay, task_id="t1"):
    task = _task(replay, task_id)
    assert replay.result.error.startswith("CheckUnattributable:"), replay.result.error
    assert "check: structured report missing" in replay.result.error
    assert "No repair call was made" in replay.result.error
    assert _stop(replay)["kind"] == "operator" and _stop(replay)["legacy"] == "CheckUnattributable"
    assert task["closed_as"] == "stopped:CheckUnattributable" and task["primary"] == "operator"
    assert task["checks"][-1]["attribution"]["product"] is False
    assert task["edges"]["checks"] is False and not replay.result.completed
    return task


def test_a_runner_lost_after_the_design_fix_gets_no_gate_fix_and_no_review(tmp_path, monkeypatch):
    def design_fix(call, replay):
        H.write(call, {"static/style.css": "#import { padding: 10px; }\n"})
        H.evidence(Path(call.cwd), "t1", age=H.FRESH)
        _lose_environment(monkeypatch)                   # the environment goes, not the app
        return 'Padded the button and recaptured.\nCHANGED: ["static/style.css"]'
    from quadratus.session import Session
    monkeypatch.setattr(Session, "_pick_lead", lambda self, spec: "claude:opus")
    script = _design_script("unused")
    script.overrides.update({"orchestrator": _then_done(DECL_DESIGN), "design-fix": design_fix})
    replay = H.run(tmp_path, monkeypatch, script, files=_design_files(), max_tasks=3)
    t1 = _handed_off(replay)
    assert t1["checks"][0]["passed"] is True, "the task's own gate passed before the environment went"
    assert not replay.of("gate-fix") and len(replay.of("design-fix")) == 1
    assert not replay.of("design-review"), "no approval is sought over an unproven tree"
    assert "padding: 10px" in (replay.project / "static/style.css").read_text(), "work preserved"


def test_a_runner_lost_after_a_gate_fix_is_the_operators_not_a_product_failure(tmp_path, monkeypatch):
    def gate_fix(call, replay):
        _lose_environment(monkeypatch)
        return "Looked; the failure is not obvious.\nCHANGED: []"
    script = Script(orchestrator=_then_done(DECL_T1), lead=lambda call, replay: "Looked; left it.\nCHANGED: []",
                    **{"gate-fix": gate_fix})
    replay = H.run(tmp_path, monkeypatch, script, files=FILES, max_tasks=3)
    t1 = _handed_off(replay)
    assert len(replay.of("gate-fix")) == 1, "the one attributable failure bought one fix; the crash buys none"
    first, last = t1["checks"][0], t1["checks"][-1]
    assert first["attribution"]["product"] is True and last["attribution"]["product"] is False
    assert (replay.project / "app.py").read_text() == FILES["app.py"]


def test_a_runner_lost_after_a_security_fix_gets_no_further_repair_or_verdict(tmp_path, monkeypatch):
    from quadratus import project_run
    from tests.lifecycle.test_workflow_contract import SECURITY, _structured
    real = project_run.SessionConfig
    monkeypatch.setattr(project_run, "SessionConfig", lambda **kw: real(**{**kw, "security_verdict_json": True}))
    def security_fix(call, replay):
        _lose_environment(monkeypatch)
        return "Re-read the caller; no change.\nCHANGED: []"
    script = Script(orchestrator=_then_done(SECURITY),
                    lead=lambda call, replay: "add only adds; no input reaches a shell.\nCHANGED: []",
                    verifier=_structured("reject", None), **{"security-fix": security_fix})
    replay = H.run(tmp_path, monkeypatch, script, files=FILES_OK, max_tasks=3)
    _handed_off(replay)
    assert len(replay.of("security-fix")) == 1 and len(replay.of("verifier")) == 1
    assert not replay.of("gate-fix")


def _dead_preview_run(tmp_path, monkeypatch):
    """A harness-captured build task whose preview dies before it serves
    anything (argv without shell syntax: the profile refuses that)."""
    from tests.lifecycle.test_harness_capture import CAPTURE, REPAIR_SCOPE, REQS, _decl, _fix, _free_port, _run
    profile = tmp_path / "profile.json"
    profile.write_text(json.dumps(dict(
        preview=[sys.executable, "-c", "raise SystemExit('Xvfb: cannot open display')"],
        origin=f"http://127.0.0.1:{_free_port()}", ready_timeout=5, capture_timeout=5)))
    build = _decl("KIND: frontend standard", dict(REPAIR_SCOPE, capture=CAPTURE), "Add the toolbar.")
    return _run(tmp_path, monkeypatch, [REQS + build], {"t1": _fix}, profile=profile)


@pytest.mark.requirements_ledger
def test_a_preview_that_never_starts_gets_no_repair_call(tmp_path, monkeypatch):
    """Operator capability lost at the harness capture. The application is
    not edited for it and nothing is approved."""
    from tests.lifecycle.test_harness_capture import FITTING_PAGE
    replay = _dead_preview_run(tmp_path, monkeypatch)
    assert not replay.of("design-fix") and not replay.of("gate-fix") and not replay.of("design-review")
    assert [c.task for c in replay.of("lead")] == ["t1"], "no other seat, no continuation"
    assert replay.result.error.startswith("DesignUnverified: task t1"), replay.result.error   # today (gap E1)
    assert "exited with 1 before it was ready" in replay.result.error
    assert "cannot open display" in replay.result.error
    assert (replay.project / "templates/index.html").read_text() == FITTING_PAGE, "the lead's work is kept"
    assert not replay.result.completed
    t1 = _task(replay, "t1")
    assert t1["edges"]["evidence"] is False and t1["delivery"] is None


@pytest.mark.requirements_ledger
@pytest.mark.xfail(strict=True, raises=AssertionError,
                   reason="gap E1 (#25): a preview that cannot start is typed invalid_proof and stops as "
                          "DesignUnverified; the plan routes capability lost mid-run to an operator "
                          "handoff (map J6/J8, plan 'Failure routing')")
def test_a_preview_that_never_starts_is_an_operator_handoff(tmp_path, monkeypatch):
    replay = _dead_preview_run(tmp_path, monkeypatch)
    t1 = _task(replay, "t1")
    assert t1["primary"] == "operator" and "invalid_proof" not in _active(t1), (t1["primary"], _active(t1))
    assert _stop(replay)["kind"] == "operator"

"""Cycle01 (Davis, 2026-11-07, PR73 6702597850), after bounded-recovery-01
stopped FindingsUnresolved with five task slots left.

1. A review's EVIDENCE finding on a scenario that binds no case goes to one
   declaration-only call to the task's own lead for the PROVES lines. The
   new binding is settled against the harness run already recorded on the
   same source, and the reviewer who raised the finding rechecks it. The old
   route sent it to the harness, which cannot bind anything, so the row
   stayed missing and no one was asked.
2. A RESOLVES whose owner's recheck was never asked (the task closed with
   findings of its own, its gate failed, its owner was unavailable) is
   pending work held for the planner. The stop is kept for the owner's own
   adverse verdict and for a repeat that changed nothing.

Real Session and controller with scripted providers and the project's own
pytest gate. Every assertion reads a record the harness wrote or a prompt a
seat was handed."""
import json
from pathlib import Path

import pytest

from tests.lifecycle import harness as H
from tests.lifecycle.replies import recheck_reply
from tests.lifecycle.test_lifecycle_matrix import FILES
from tests.lifecycle.test_proof_binding import (
    CASE,
    NEGATIVE,
    _acceptance,
    _at,
    _lead,
    _plan,
    _proofs,
    _scope,
)
from tests.lifecycle.test_workflow_repair import MET, SKIP_TESTS, Seen, _roles, _rows, _script

UNBOUND = ("BLOCKING: EVIDENCE P1 - P1 binds no case: the lead declared no PROVES line for it "
           "| basis: P1 | impact: contract")
CODE = "BLOCKING: no negative case covers subtraction of negatives | basis: R1 | impact: correctness"


def _binding(reply):
    """The lead's reply to the declaration-only call, recorded by role."""
    calls = []

    def answer(call, replay):
        calls.append(call)
        return reply(call, replay) if callable(reply) else reply
    return calls, answer


def _run(tmp_path, monkeypatch, name, *, binding_reply, recheck, lead=None, collaborator=None, files=FILES,
         **script):
    seen = Seen()
    calls, answer = _binding(binding_reply)
    built = _script(seen, lead=lead or _lead(proves=()),
                    collaborator=collaborator or (lambda c, r: UNBOUND + MET), recheck=recheck, **script)
    built.overrides["proof-binding"] = answer
    replay = H.run(_at(tmp_path, name), monkeypatch, built, files=files, max_tasks=1, record_complete=False)
    return seen, calls, replay


def _route(replay, fid="RFT1-1"):
    routes = [r for r in H.result_json(replay)["evidence_routes"] if r["finding"] == fid]
    assert routes, f"no route recorded for {fid}"
    return routes[-1]


# -- 1. the missing binding goes to its author, once, and settles on the recorded run ------------

@pytest.mark.requirements_ledger
def test_a_missing_binding_is_declared_by_the_lead_and_settles_on_the_run_already_recorded(tmp_path, monkeypatch):
    """Regression for bounded-recovery-01 t3. Before cycle01 the EVIDENCE
    finding went to the harness: no binding call, no editor call, the row
    stayed unbound and missing."""
    seen, calls, replay = _run(tmp_path, monkeypatch, "declared", binding_reply=f"PROVES: P1 - {CASE}",
                               recheck=lambda c, r: "RFT1-1: RESOLVED - observed-1 shows test_add ran on this source")
    (call,) = calls
    assert call.task == "t1" and Path(call.cwd) != replay.project, "a source copy, never the project"
    # Prompt parity: the grammar the reader enforces is the one stated.
    from quadratus.scope import PROVES_RULE
    assert PROVES_RULE in call.prompt and "Only lines for the scenarios above (P1) are read" in call.prompt
    assert "Do not change files" in call.prompt and "already run on this exact source" in call.prompt
    record = H.result_json(replay)
    assert [c["stage"] for c in record["review_checks"]] == ["review"], "no suite rerun to attach a declaration"
    route = _route(replay)
    assert route["action"].startswith("binding requested from") and route["action"].endswith("declared")
    assert route["binding_before"] != ["bound"] and route["binding_after"] == ["bound"]
    assert route["execution_before"] == ["missing"] and route["execution_after"] == ["ran"]
    assert route["cases"] == [CASE] and route["source"] == record["review_checks"][0]["source"]
    (row,) = _acceptance(replay)
    assert row["binding"] == "bound" and row["cases"] == [CASE] and row["execution"] == "ran"
    assert row["binding_history"][-1]["role"] == "proof-binding"
    assert "execution ran" in _rows(seen.prompts["recheck"][0]), "the owner rechecks what is now shown"
    finding = record["review_findings"][0]
    assert finding["closed"] and finding["history"][-1]["author"] == finding["author"]
    assert "revision" not in _roles(replay), "a declaration needs no editor call"
    assert replay.result.completed, replay.result.error


@pytest.mark.requirements_ledger
@pytest.mark.parametrize("name, reply, binding, why", [
    ("no-line", "I could not find the test.", "missing", "no PROVES line"),
    ("invalid", "PROVES: P1 - test_add", "invalid declaration", "declared"),
    ("other-scenario", f"PROVES: P2 - {CASE}", "missing", "no PROVES line"),
    ("absent-case", "PROVES: P1 - tests/test_app.py::test_absent", "bound", "declared"),
])
def test_a_binding_reply_that_proves_nothing_leaves_the_finding_open_for_its_owner(tmp_path, monkeypatch, name,
                                                                                   reply, binding, why):
    seen, calls, replay = _run(tmp_path, monkeypatch, name, binding_reply=reply,
                               recheck=lambda c, r: "RFT1-1: UNRESOLVED P1 is still not shown executed")
    assert len(calls) == 1
    (row,) = _acceptance(replay)
    assert row["execution"] != "ran", "nothing executed the declared case on this source"
    assert (row["binding"] or "missing").startswith(binding) or (binding == "missing" and row["binding"] != "bound")
    assert why in _route(replay)["action"]
    record = H.result_json(replay)
    assert not record["review_findings"][0]["closed"] and len(seen.prompts["recheck"]) == 1
    if name == "other-scenario":
        assert any("not a requested scenario's line" in p["problem"] for p in record["proof_binding_problems"])
    assert not replay.result.completed


@pytest.mark.requirements_ledger
def test_a_declared_case_the_run_skipped_is_not_shown_executed(tmp_path, monkeypatch):
    lead = _lead(proves=(), tests=SKIP_TESTS)
    seen, calls, replay = _run(tmp_path, monkeypatch, "skipped", lead=lead, binding_reply=f"PROVES: P1 - {NEGATIVE}",
                               recheck=lambda c, r: "RFT1-1: UNRESOLVED test_negative was skipped")
    (row,) = _acceptance(replay)
    assert row["binding"] == "bound" and row["cases"] == [NEGATIVE] and row["execution"] != "ran"
    assert _route(replay)["execution_after"] != ["ran"] and not replay.result.completed


@pytest.mark.requirements_ledger
def test_the_reviewer_cannot_write_the_binding_it_judges(tmp_path, monkeypatch):
    """A PROVES line in the reviewer's own reply binds nothing: only the
    lead's declaration does, and here the lead declares none."""
    review = lambda c, r: f"{UNBOUND}\nPROVES: P1 - {CASE}{MET}"  # noqa: E731
    seen, calls, replay = _run(tmp_path, monkeypatch, "impersonation", collaborator=review,
                               binding_reply="No test proves it yet.",
                               recheck=lambda c, r: "RFT1-1: UNRESOLVED still unbound")
    (row,) = _acceptance(replay)
    assert row["binding"] != "bound" and not row.get("cases")
    assert all(h.get("role") != "collaborator" for h in row.get("binding_history") or [])
    assert not replay.result.completed


@pytest.mark.requirements_ledger
def test_a_bound_case_with_missing_execution_still_goes_to_the_harness(tmp_path, monkeypatch):
    """The other kind of EVIDENCE finding is unchanged: a bound case the
    harness has not shown executed is the harness's to run, not a binding
    request."""
    seen, calls, replay = _run(tmp_path, monkeypatch, "bound", lead=_lead(),
                               collaborator=lambda c, r: ("BLOCKING: EVIDENCE P1 - test_add is not shown executed "
                                                          "on this source | basis: P1 | impact: contract" + MET),
                               binding_reply=f"PROVES: P1 - {CASE}",
                               recheck=lambda c, r: "RFT1-1: RESOLVED - observed-1 shows it")
    assert not calls, "a bound scenario is never re-declared"
    assert _route(replay)["action"].startswith("no new run")
    assert replay.result.completed, replay.result.error


@pytest.mark.requirements_ledger
def test_a_binding_with_a_code_finding_is_settled_on_the_revised_source_not_the_stale_run(tmp_path, monkeypatch):
    """Mixed round: the binding and a code finding in one bundle. Since
    cycle22 the editor revision the bundle already buys declares the binding
    itself (no separate declaration call, which used to settle on the
    pre-edit run the revision then made stale); the row settles on the
    revised source and the recheck is shown that run."""
    def revision(call, replay):
        assert "Scenarios of your task that bind no case, or bind invalidly" in call.prompt
        H.write(call, {"tests/test_app.py": SKIP_TESTS.replace("@pytest.mark.skip(reason='not yet')\n", "")})
        return f'Added the negative case.\nPROVES: P1 - {CASE}\nCHANGED: ["tests/test_app.py"]'
    seen, calls, replay = _run(tmp_path, monkeypatch, "mixed", binding_reply=f"PROVES: P1 - {CASE}",
                               collaborator=lambda c, r: f"{UNBOUND}\n{CODE}{MET}",
                               recheck=lambda c, r: recheck_reply(c.prompt, default="RESOLVED - shown on this source"),
                               revision=revision)
    assert replay.of("proof-binding") == [], "no separate declaration call beside the revision"
    route = _route(replay)
    record = H.result_json(replay)
    assert route["folded"] and route["declared"] == [f"PROVES: P1 - {CASE}"]
    (row,) = _acceptance(replay)
    assert row["source_digest"] == record["source_fingerprint"], "the row's execution is on the revised source"
    assert replay.result.completed, replay.result.error


# -- 2. a recheck never asked is pending work, held for the planner ---------------------------------

OWN = "BLOCKING: README does not document add | basis: R1 | impact: documentation"
PLAN1 = _plan(_scope(proofs=_proofs("R1")))


def _resolving(*fids, text="Fix add in app.py as the open findings ask."):
    return (f"KIND: backend simple\nSCOPE: {json.dumps(_scope(proofs=_proofs('R1')))}\n{text}\nCOVERS: R1\n"
            f"RESOLVES: {', '.join(fids)}")


def _settling(*, own=lambda task: task == "t1", settle="RESOLVED - the negative case is handled now"):
    """Reviews: ``own(task)`` tasks raise a finding of their own that their
    in-task recheck keeps open; every settlement recheck answers ``settle``."""
    def review(call, replay):
        text = CODE if call.task == "t1" else OWN
        return (text if own(call.task) else "NO FINDINGS") + MET

    def recheck(call, replay):
        if "in RESOLVES" in call.prompt:
            return recheck_reply(call.prompt, default=settle)
        return recheck_reply(call.prompt, default="UNRESOLVED still open")
    return review, recheck


def _run_tasks(tmp_path, monkeypatch, name, plans, *, max_tasks, review, recheck, lead=None):
    seen = Seen()
    def orchestrator(call, replay):
        n = len(replay.of("orchestrator"))
        return plans[n - 1] if n <= len(plans) else "DONE"
    built = _script(seen, orchestrator=orchestrator, lead=lead or _lead(), collaborator=review, recheck=recheck)
    replay = H.run(_at(tmp_path, name), monkeypatch, built, files=FILES, max_tasks=max_tasks, record_complete=False)
    return seen, replay


def _resolution(replay, task):
    return [r for r in H.result_json(replay)["review_resolutions"] if r["task"] == task][-1]


@pytest.mark.requirements_ledger
def test_a_resolves_whose_recheck_was_never_asked_is_held_and_the_planner_continues(tmp_path, monkeypatch):
    """Regression for bounded-recovery-01: t2 RESOLVES t1's finding but
    closes with a finding of its own, so its owner is never asked. That is
    pending work: the planner names t3, which resolves both, the owners
    recheck them RESOLVED, t2's settlement is established by that fact and
    the run completes. Before cycle01 the run stopped FindingsUnresolved
    after t2 with slots left."""
    review, recheck = _settling(own=lambda task: task in ("t1", "t2"))
    seen, replay = _run_tasks(tmp_path, monkeypatch, "held",
                              [PLAN1, _resolving("RFT1-1"), _resolving("RFT1-1", "RFT2-1")],
                              max_tasks=4, review=review, recheck=recheck)
    t2 = _resolution(replay, "t2")
    assert t2["outcomes"] == {"RFT1-1": "not_attempted"} and "closed with open findings" in t2["refused"]
    assert t2["rechecks"] is None, "no recheck call was made for t2's RESOLVES"
    assert not (replay.result.error or "").startswith("FindingsUnresolved"), replay.result.error
    assert len(replay.of("orchestrator")) >= 3, "the planner had its turn after t2"
    t3 = _resolution(replay, "t3")
    assert t3["outcomes"] == {"RFT1-1": "closed", "RFT2-1": "closed"}
    findings = {r["fid"]: r for r in H.result_json(replay)["review_findings"]}
    for fid in ("RFT1-1", "RFT2-1"):
        assert findings[fid]["closed"] and findings[fid]["history"][-1]["author"] == findings[fid]["author"]
    assert [a["outcome"] for a in findings["RFT1-1"]["resolution_attempts"]] == ["not_attempted", "closed"]
    assert t2["settled_later_by"] == "t3"
    tasks = {t["task_id"]: t for t in H.result_json(replay)["workflow"]["tasks"]}
    assert tasks["t2"]["edges"]["settlement"] is True and tasks["t3"]["edges"]["settlement"] is True
    assert replay.result.completed, replay.result.error


@pytest.mark.requirements_ledger
def test_the_owners_adverse_verdict_is_still_the_stop(tmp_path, monkeypatch):
    review, recheck = _settling(settle="UNRESOLVED the negative case still fails")
    seen, replay = _run_tasks(tmp_path, monkeypatch, "adverse", [PLAN1, _resolving("RFT1-1")], max_tasks=4,
                              review=review, recheck=recheck)
    assert _resolution(replay, "t2")["outcomes"] == {"RFT1-1": "adverse"}
    assert (replay.result.error or "").startswith(
        "FindingsUnresolved: task t2 named RESOLVES RFT1-1 but did not establish their acceptance (RFT1-1: adverse)")
    assert len(replay.of("orchestrator")) == 2 and not replay.result.completed


@pytest.mark.requirements_ledger
def test_a_second_unasked_resolves_on_an_unchanged_source_is_no_progress_and_stops(tmp_path, monkeypatch):
    review, recheck = _settling(own=lambda task: True)
    seen, replay = _run_tasks(tmp_path, monkeypatch, "repeat",
                              [PLAN1, _resolving("RFT1-1"),
                               _resolving("RFT1-1", text="Handle negatives in add in app.py.")], max_tasks=5,
                              review=review, recheck=recheck)
    assert _resolution(replay, "t2")["outcomes"] == {"RFT1-1": "not_attempted"}
    assert _resolution(replay, "t3")["outcomes"] == {"RFT1-1": "not_attempted (again in an unchanged decision state)"}
    assert "RFT1-1: not_attempted (again in an unchanged decision state)" in (replay.result.error or "")
    assert (replay.result.error or "").startswith("FindingsUnresolved: task t3"), replay.result.error
    assert not replay.result.completed


@pytest.mark.requirements_ledger
def test_a_second_unasked_resolves_that_changed_the_source_is_progress_and_continues(tmp_path, monkeypatch):
    review, recheck = _settling(own=lambda task: task in ("t1", "t2", "t3"))
    variants = iter(["", "\n\n\ndef add_all(*xs):\n    return sum(xs)\n", "\n\n\ndef add_three(a, b, c):\n    return a + b + c\n"])
    from tests.lifecycle.test_lifecycle_matrix import FIXED

    def lead(call, replay):
        H.write(call, {"app.py": FIXED + next(variants, "")})
        return f'Changed app.py.\nPROVES: P1 - {CASE}\nCHANGED: ["app.py"]'
    seen, replay = _run_tasks(tmp_path, monkeypatch, "progress",
                              [PLAN1, _resolving("RFT1-1"),
                               _resolving("RFT1-1", text="Handle negatives in add in app.py."),
                               _resolving("RFT1-1", "RFT2-1", "RFT3-1", text="Finish add and its docs.")],
                              max_tasks=5, review=review, recheck=recheck, lead=lead)
    assert _resolution(replay, "t3")["outcomes"] == {"RFT1-1": "not_attempted"}, "a changed state is not a repeat"
    assert not (replay.result.error or "").startswith("FindingsUnresolved"), replay.result.error
    assert _resolution(replay, "t4")["outcomes"]["RFT1-1"] == "closed"
    assert replay.result.completed, replay.result.error


@pytest.mark.requirements_ledger
def test_on_the_last_slot_a_held_resolves_goes_to_the_cap_decision_and_never_completes(tmp_path, monkeypatch):
    review, recheck = _settling(own=lambda task: task in ("t1", "t2"))
    seen, replay = _run_tasks(tmp_path, monkeypatch, "last", [PLAN1, _resolving("RFT1-1")], max_tasks=2,
                              review=review, recheck=recheck)
    assert _resolution(replay, "t2")["outcomes"] == {"RFT1-1": "not_attempted"}
    assert not (replay.result.error or "").startswith("FindingsUnresolved"), replay.result.error
    close = [d for d in H.result_json(replay)["close_decisions"] if "t2" in d["closed"]][-1]
    assert close["stop"] is None and close["holds"] and not close["slot_left"]
    tasks = {t["task_id"]: t for t in H.result_json(replay)["workflow"]["tasks"]}
    assert tasks["t2"]["edges"]["settlement"] is False
    assert not replay.result.completed and "RFT1-1" in (replay.result.error or "")


@pytest.mark.requirements_ledger
def test_a_failed_gate_in_a_resolving_task_is_never_hidden_by_the_held_finding(tmp_path, monkeypatch):
    review, recheck = _settling()
    from tests.lifecycle.test_lifecycle_matrix import FIXED

    def lead(call, replay):
        broken = call.task == "t2"
        H.write(call, {"app.py": FIXED.replace("a + b", "a - b") if broken else FIXED})
        return f'Changed app.py.\nPROVES: P1 - {CASE}\nCHANGED: ["app.py"]'
    seen, replay = _run_tasks(tmp_path, monkeypatch, "gate", [PLAN1, _resolving("RFT1-1")], max_tasks=3,
                              review=review, recheck=recheck, lead=lead)
    t2 = _resolution(replay, "t2")
    assert t2["outcomes"] == {"RFT1-1": "not_attempted"} and "integration gate failed" in t2["refused"]
    record = H.result_json(replay)
    t2_checks = [c for c in record["checks"] if c.get("task") in (None, "t2")]
    assert record["checks"][-1]["passed"] is False or any(not c["passed"] for c in t2_checks)
    close = [d for d in record["close_decisions"] if "t2" in d["closed"]][-1]
    assert close["stop"] == "CheckFailing" or close["debt_recorded"], close
    assert not replay.result.completed


@pytest.mark.requirements_ledger
def test_an_unavailable_owner_is_pending_work_not_an_adverse_verdict(tmp_path, monkeypatch):
    from quadratus.session import Session
    real = Session._settle_review_findings
    owner = {}

    def settle(self, spec, named, inherited, reasons):
        available = self._available
        self._available = lambda key: key != owner.get("key") and available(key)
        try:
            return real(self, spec, named, inherited, reasons)
        finally:
            self._available = available
    monkeypatch.setattr(Session, "_settle_review_findings", settle)

    def review(call, replay):
        if call.task == "t1":
            owner.setdefault("key", f"{'openai' if call.vendor == 'codex' else call.vendor}:{call.model}")
            return CODE + MET
        return "NO FINDINGS" + MET
    _, recheck = _settling()
    seen, replay = _run_tasks(tmp_path, monkeypatch, "unavailable", [PLAN1, _resolving("RFT1-1")], max_tasks=3,
                              review=review, recheck=recheck)
    t2 = _resolution(replay, "t2")
    assert t2["outcomes"].get("RFT1-1") == "unavailable", (t2, owner)
    assert not (replay.result.error or "").startswith("FindingsUnresolved"), replay.result.error
    assert not replay.result.completed


def test_the_saved_record_names_each_unestablished_resolves_by_what_happened():
    from tools.truth_check import _resolution_problems
    record = dict(
        review_findings=[dict(fid="A", closed=False), dict(fid="B", closed=False), dict(fid="C", closed=True),
                         dict(fid="D", closed=False), dict(fid="E", closed=False)],
        review_resolutions=[
            dict(task="t2", named=["A", "C"], outcomes={"A": "not_attempted", "C": "closed"},
                 refused="it closed with open findings"),
            dict(task="t3", named=["B"], outcomes={"B": "adverse"}, refused=None),
            dict(task="t4", named=["D", "E"], outcomes={"D": "owed", "E": "unavailable"}, refused=None),
            dict(task="t5", named=["A"])])
    lines = _resolution_problems(record)
    assert lines == [
        "task t2: RESOLVES A not established (not_attempted; it closed with open findings)",
        "task t3: RESOLVES B not established (adverse)",
        "task t4: RESOLVES D not established (owed)",
        "task t4: RESOLVES E not established (unavailable)",
        "task t5: RESOLVES A not established (not recorded)"]
    record["review_findings"][0]["closed"] = True
    assert not any(" A " in line for line in _resolution_problems(record)), "a closed finding is settled"


@pytest.mark.requirements_ledger
def test_a_parallel_child_declares_its_missing_binding_and_the_merge_gate_shows_it(tmp_path, monkeypatch):
    """In a parallel child the lead declares the binding (a declaration needs
    no execution), the finding stays held for the post-merge recheck, and
    the merge gate's own receipt on the merged source shows the bound case
    to the reviewer who raised it."""
    from tests.lifecycle.replies import judging_scenarios
    from tests.lifecycle.test_lifecycle_matrix import FIXED, Script
    from tests.lifecycle.test_proof_binding import PROOF_R1, SHOWN
    requirements = "REQUIREMENTS:\nR1: add(a, b) returns the sum\nR2: README documents add\n"
    seen = Seen()

    def block(path, covers, proofs):
        scope = dict(permitted_paths=[path], intended_result=f"{path} written", acceptance=[f"{path} meets the task"],
                     max_lines=20, proofs=proofs)
        return f"KIND: backend simple\nSCOPE: {json.dumps(scope)}\nWrite {path}.\nCOVERS: {covers}"
    batch = (requirements + "PARALLEL\n" + block("app.py", "R1", _proofs("R1")) + "\n---\n"
             + block("README.md", "R2", _proofs("R2", location="README.md", evidence="reading")))

    def lead(call, replay):
        if "Write README.md." in call.prompt:
            H.write(call, {"README.md": "# app\n\n`add(a, b)` returns the sum.\n"})
            return 'Documented add.\nCHANGED: ["README.md"]'
        H.write(call, {"app.py": FIXED})
        return 'Implemented add.\nCHANGED: ["app.py"]'

    def collaborator(call, replay):
        return UNBOUND + "\nR1: MET - app.py" if call.task == "t1" else "NO FINDINGS\nR2: MET - README.md"

    def recheck(call, replay):
        seen.keep("recheck", call)
        return "RFT1-1: RESOLVED - the merge gate's receipt shows test_add ran on the merged source"

    def auditor(call, replay):
        docs = H.obligation_ids(call.prompt, modality="reading", requirement="R2")
        return (f"R1: MET - {CASE}\nR2: MET - README.md"
                + "".join(f"\n{o}: SUFFICIENT - README.md names add(a, b) and its result" for o in docs))
    calls, answer = _binding(f"PROVES: P1 - {CASE}")
    script = Script(orchestrator=lambda c, r: batch if len(r.of("orchestrator")) == 1 else "DONE", lead=lead,
                    collaborator=collaborator, auditor=judging_scenarios(auditor, PROOF_R1, basis=SHOWN),
                    **{"requirements-review": lambda c, r: "COMPLETE"})
    script.overrides["recheck"] = recheck
    script.overrides["proof-binding"] = answer
    script.overrides["revision"] = lambda c, r: pytest.fail("no editor call for a binding in a child")
    replay = H.run(_at(tmp_path, "parallel"), monkeypatch, script, files=FILES, max_tasks=4, record_complete=False)
    (call,) = calls
    assert call.task == "t1"
    route = _route(replay)
    assert route["action"].startswith("binding requested from") and route["binding_after"] == ["bound"]
    assert route["execution_after"] == ["missing"], "a child measures no execution"
    assert len(seen.prompts["recheck"]) == 1
    assert f"binding bound: {CASE}; execution ran (receipt check-1)" in _rows(seen.prompts["recheck"][0])
    record = H.result_json(replay)
    finding = next(f for f in record["review_findings"] if f["fid"] == "RFT1-1")
    assert finding["closed"] and finding["history"][-1]["role"] == "recheck"
    assert replay.result.completed, replay.result.error


# -- 3. no progress is the same decision state, not the same bytes (root review of 9b0280b, R1) --

@pytest.mark.requirements_ledger
def test_root_binding_evidence_progress_without_a_source_edit_lets_the_next_task_run(tmp_path, monkeypatch):
    """Root's reproducer: t2 leaves P1 unbound and missing, t3 changes no
    byte but declares the existing case so P1 is bound and ran. That is
    progress: t4 runs and settles every finding with its owner."""
    review, recheck = _settling(own=lambda task: task in ("t1", "t2", "t3"))

    def lead(call, replay):
        return _lead(proves=() if call.task == "t2" else (CASE,))(call, replay)
    _, replay = _run_tasks(tmp_path, monkeypatch, "binding-progress",
                           [PLAN1, _resolving("RFT1-1"),
                            _resolving("RFT1-1", text="Declare existing proof for add and address the pending finding."),
                            _resolving("RFT1-1", "RFT2-1", "RFT3-1", text="Settle the remaining findings with their owners.")],
                           max_tasks=5, review=review, recheck=recheck, lead=lead)
    record = H.result_json(replay)
    rows = {t: [r for r in record["evidence_obligations"] if r.get("task") == t and r.get("kind") == "acceptance"]
            for t in ("t2", "t3")}
    assert any(r.get("binding") != "bound" for r in rows["t2"])
    assert any(r.get("binding") == "bound" and r.get("execution") == "ran" for r in rows["t3"])
    attempts = next(f for f in record["review_findings"] if f["fid"] == "RFT1-1")["resolution_attempts"]
    assert attempts[0]["source"] == attempts[1]["source"], "the bytes did not change"
    assert attempts[0]["state"] != attempts[1]["state"] and not attempts[1]["repeated"]
    assert "t4" in [c.task for c in replay.of("lead")], replay.result.error
    assert replay.result.completed, replay.result.error


def test_an_unknown_source_never_proves_an_unchanged_decision_state(tmp_path):
    from tests.lifecycle.test_proof_binding import _session
    session = _session(tmp_path)
    target = dict(fid="RFT1-1", author="claude:opus")
    assert session._decision_state(target, "not_attempted", "it closed with open findings", None) is None
    known = session._decision_state(target, "not_attempted", "it closed with open findings", "S")
    assert known and known == session._decision_state(target, "not_attempted", "it closed with open findings", "S")
    assert known != session._decision_state(target, "not_attempted", "its integration gate failed", "S")
    session._available = lambda key: False
    assert known != session._decision_state(target, "not_attempted", "it closed with open findings", "S"), \
        "an owner who can no longer be asked is a different state"


def test_a_binding_is_requested_again_once_the_scenario_state_changed(tmp_path):
    from types import SimpleNamespace

    from tests.lifecycle.test_proof_binding import _session
    session = _session(tmp_path)
    session.project = tmp_path
    session._source_fingerprint = lambda: "S"
    spec = SimpleNamespace(task_id="t1")
    row = dict(oid="O1", task="t1", proof_id="P1", kind="acceptance", modality="test", binding="unbound", cases=[])
    session.evidence_obligations = [row]
    session.evidence_routes = [dict(task="t1", proof_id="P1", finding="RFT1-1", source="S",
                                    action="binding requested from claude:opus: no PROVES line for a requested scenario",
                                    binding_after=["unbound"], cases=[])]
    assert session._binding_requested(spec, "P1"), "same source, same binding: the same request"
    row.update(binding="invalid declaration: not one case", cases=[])
    assert session._binding_requested(spec, "P1") == "", "the binding changed since: a new state"
    row.update(binding="unbound")
    session._source_fingerprint = lambda: None
    assert session._binding_requested(spec, "P1") == "", "an unknown source proves no sameness"

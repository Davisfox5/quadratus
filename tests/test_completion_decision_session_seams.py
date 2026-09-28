"""Whole-Session seams for the completion decision candidate (map P3.4).

``tests/test_completion_decision.py`` drives the candidate once, from the
session's end state. This file adds what that leaves out:

- Round by round. Each DONE reply is snapshotted where the session makes
  its decision, after the findings re-check. The candidate is replayed per
  round with the answers the session got in that round. Every round before
  the last must be a send-back that carries the session's own reopen note,
  and the last must land where the session landed.
- Reachability. Real parallel runs show that an active run-level fact (the
  merge gate) ends the run before either DONE site. The repaired merge gate
  reaches DONE and the cap only as a recovered fact.
- Synthetic characterisations, labelled as such. Each injects a state no
  path at 064f930 produces, and pins today's engine answer beside the
  candidate's: an active run-level fact the guard does not read, typed-only
  partial debt, and legacy open findings short-circuiting partiality.

Nothing here wires the candidate in or changes the engine.
"""

import copy

import pytest

from quadratus import completion_decision as C
from quadratus.completion_decision import (
    CAP,
    CHECK_REQUIREMENTS,
    COMPLETE,
    DONE_REPLY,
    INCOMPLETE,
    SEND_BACK,
    VERIFY_DEPENDENCIES,
    RequirementsAnswer,
    resolve,
)
from quadratus.outcome import typed_completed
from tests.test_completion_decision import _agrees, _shadow_decision, shadow  # noqa: F401


@pytest.fixture
def rounds(monkeypatch):
    """Read-only spies: one snapshot per DONE reply, and its answers."""
    from quadratus import session as S
    log = dict(session=None, rounds=[])
    run, block, requirements, verify = (S.Session.run, S.Session._findings_block_done,
                                        S.Session._requirements_satisfied, S.Session._verify_dependencies)

    def spy_run(self, *a, **kw):
        log["session"] = self
        return run(self, *a, **kw)

    def spy_block(self):
        # The DONE site's first move; the re-check has run when it returns.
        blocked = block(self)
        log["rounds"].append(dict(inputs=copy.deepcopy(C.snapshot_session(self, site=DONE_REPLY)),
                                  refusal=self._done_refusal if blocked else "",
                                  requirements=None, answer_refusal="", ledger=None, verified=False))
        return blocked

    def spy_requirements(self):
        result = requirements(self)
        if log["rounds"]:
            log["rounds"][-1].update(requirements=result, answer_refusal=self._done_refusal,
                                     ledger=C.LedgerSnapshot.from_parts(
                                         enabled=self.config.requirements_ledger,
                                         requirement_status=self.memory.ledger.requirement_status,
                                         findings=self.findings))
        return result

    def spy_verify(self, window):
        result = verify(self, window)
        if window == "at DONE":
            log["rounds"][-1]["verified"] = True
        return result
    for name, spy in (("run", spy_run), ("_findings_block_done", spy_block),
                      ("_requirements_satisfied", spy_requirements), ("_verify_dependencies", spy_verify)):
        monkeypatch.setattr(S.Session, name, spy)
    return log


def _replay_round(r):
    made = []

    def confirm():
        raise AssertionError("the goal question is never asked at a DONE reply")

    def check():
        assert r["requirements"] is not None, "the candidate checked requirements the session did not"
        made.append(CHECK_REQUIREMENTS)
        return RequirementsAnswer(r["requirements"], r["answer_refusal"], r["ledger"])

    def verify():
        assert r["verified"], "the candidate verified dependencies the session did not"
        made.append(VERIFY_DEPENDENCIES)
    decision = resolve(r["inputs"], confirm_goal=confirm, check_requirements=check, verify_dependencies=verify)
    asked = r["requirements"] is not None and r["inputs"].ledger.enabled
    assert (CHECK_REQUIREMENTS in made) == asked, "the requirements check parity, this round"
    assert (VERIFY_DEPENDENCIES in made) == r["verified"], "the dependency check parity, this round"
    return decision


def _partial_notes(session):
    return {(o.task_id, m) for o in session.task_outcomes for m in o.mismatches if m.startswith("partial:")}


def _replay_all(log):
    """Every round replayed; returns the decisions and the session."""
    session = log["session"]
    assert log["rounds"], "the session never reached a DONE reply"
    decisions = [_replay_round(r) for r in log["rounds"]]
    for decision, r in zip(decisions[:-1], log["rounds"]):
        assert decision.status == SEND_BACK
        # The findings refusal is the candidate's to name; a requirements
        # refusal was already written by the session's own check.
        assert decision.refusal == r["refusal"]
    notes = [f.detail for f in session.run_outcome.facts if not f.terminal]
    assert [d.reopen_fact for d in decisions[:-1]] == notes
    final = decisions[-1]
    assert final.status != SEND_BACK
    assert final.completed == session.completed
    assert bool(final.done_accepted) == session.run_outcome.done_accepted
    assert (final.stop.reason if final.stop else "") == session.stop_reason
    if final.stop:
        stop = session.run_outcome.stop()
        assert (final.stop.kind, final.stop.name) == (stop.kind, stop.legacy)
    assert set(final.partial_mismatches) == _partial_notes(session)
    return decisions, session


# -- reachable: round by round -------------------------------------------------------------

@pytest.mark.requirements_ledger
def test_requirements_sent_back_once_then_met_complete_round_by_round(tmp_path, rounds):
    """SEND_BACK then COMPLETE in one session: the reopen spent is read, not
    reset, and its non-terminal note neither blocks nor is reported."""
    from quadratus.artifacts import ArtifactStore
    from quadratus.session import Session
    from tests.test_requirements_ledger import PLAN, Script
    met = "R1: MET - app.py\nR2: MET - templates/index.html\nR3: MET - app.py"
    script = Script([PLAN, "DONE", "KIND: frontend simple\nAdd the button.\nCOVERS: R2, R3", "DONE"],
                    audits=[met])
    session = Session("Build the preview", ArtifactStore(tmp_path / "a"), script)
    session.run(max_tasks=6)
    decisions, _ = _replay_all(rounds)
    assert [d.status for d in decisions] == [SEND_BACK, COMPLETE], session.stop_reason
    assert [r["inputs"].reopens for r in rounds["rounds"]] == [0, 1]
    assert session.completed and decisions[-1].divergences == ()


@pytest.mark.requirements_ledger
def test_a_reopened_finding_is_sent_back_each_round_before_any_requirements_check(tmp_path, monkeypatch, rounds):
    """Per round, not only at the end: every send-back carries the session's
    own refusal text, and no round reaches the requirements check."""
    from pathlib import Path

    from tests.lifecycle import harness as H
    from tests.lifecycle.test_audit_findings import AUDIT, DOCS, REPAIR, REQS, WIDE, _capture, _repair
    from tests.lifecycle.test_audit_findings import _run as audit_run

    def docs(call, replay):
        H.write(call, {"README.md": "# app\n\nchanged later\n"})
        assert Path(call.cwd).is_dir()
        return 'Documented.\nCHANGED: ["README.md"]'
    audit_run(tmp_path, monkeypatch, [REQS + AUDIT, REPAIR + "\nRESOLVES: F1", DOCS],
              {"t1": _capture(measured=WIDE), "t2": _repair(), "t3": docs}, max_tasks=10)
    decisions, session = _replay_all(rounds)
    assert [d.status for d in decisions] == [SEND_BACK] * 3 + [INCOMPLETE]
    assert all(r["requirements"] is None for r in rounds["rounds"])
    assert decisions[0].refusal == "\n\n--- DONE SENT BACK ---\nAudit findings are still open: F1."
    assert decisions[-1].stop.name == "FindingsUnresolved"


# -- reachable: run-level facts and the two DONE sites -------------------------------------

def _run_level(session):
    return [f for f in session.run_outcome.facts if f.kind == "product"]


def test_a_repaired_merge_gate_reaches_done_only_as_a_recovered_run_level_fact(tmp_path, rounds):
    from tests.test_merge_gate_allowance import _run
    session, _, _ = _run(tmp_path)
    decisions, _ = _replay_all(rounds)
    assert [d.status for d in decisions] == [COMPLETE]
    facts = _run_level(session)
    assert facts and not any(f.active for f in facts), "present on the record, recovered by the passing gate"
    assert decisions[0].divergences == (), "a recovered run-level fact is not a divergence"


def test_a_standing_merge_gate_failure_ends_the_run_before_the_done_reply(tmp_path, rounds):
    """The one run-level writer that can outlive a task stops the loop at
    the batch check (session.py, after ``_run_batch``): the orchestrator's
    DONE is never read, so neither site sees the active fact."""
    from tests.test_merge_gate_allowance import _run
    session, _, _ = _run(tmp_path, merge_fix_works=False)
    assert rounds["rounds"] == []
    assert any(f.active for f in _run_level(session))
    assert session.stop_reason.startswith("FindingsOpen: a task closed with open work, but the merge gate "
                                          "still fails"), session.stop_reason


def _cap_run(tmp_path, *, merge_fix_works):
    """``test_merge_gate_allowance._run`` with the slots exactly spent (one
    serial task, a batch of two) and the goal question answered DONE."""
    from quadratus.session import _TERMINAL_REQUEST
    from tests.test_merge_gate_allowance import SERIAL, MergeGate
    from tests.test_parallel_tasks import BATCH, Orchestrated
    from tests.test_parallel_tasks import _session as parallel_session
    script = Orchestrated([SERIAL, BATCH], lead_delay=0)
    project = tmp_path / "project"
    project.mkdir()
    session, _ = parallel_session(tmp_path, script, integration_gate=MergeGate(project), max_gate_fixes=1)
    parent = script.invoke_for(project)

    def invoke(model, prompt, system=None, allow_writes=False):
        if _TERMINAL_REQUEST[:60] in prompt:
            return "DONE"
        if "integration check failed" in prompt:
            if (project / "a.py").exists():
                (project / "a.py").write_text("# a.py fixed\n" if merge_fix_works else "# a.py again\n")
                return 'Fixed the merge.\nCHANGED: ["a.py"]'
            (project / "c.py").write_text("# c.py fixed\n")
            return 'Fixed c.\nCHANGED: ["c.py"]'
        if "You are leading" in prompt and " in c.py" in prompt:
            (project / "c.py").write_text("# c.py\n")
            return 'Wrote it\nCHANGED: ["c.py"]'
        return parent(model, prompt, system, allow_writes)
    session.invoke = invoke
    session.run(max_tasks=3)
    return session


def test_a_repaired_merge_gate_on_the_last_slots_completes_at_the_cap(tmp_path, shadow):
    session = _cap_run(tmp_path, merge_fix_works=True)
    decision, _ = _shadow_decision(shadow, CAP, 3)
    assert shadow["goal"] is True and decision.ready and session.completed
    assert _run_level(session) and not any(f.active for f in _run_level(session))
    assert decision.divergences == ()
    _agrees(decision, session, session.stop_reason)


def test_a_standing_merge_gate_failure_on_the_last_slots_never_reaches_the_cap(tmp_path, shadow):
    """The batch check breaks the loop, so the cap's ``else`` never runs."""
    session = _cap_run(tmp_path, merge_fix_works=False)
    assert shadow["goal"] is None and shadow["requirements"] is None
    assert any(f.active for f in _run_level(session)) and not session.completed
    assert session.run_outcome.stop().legacy == "FindingsOpen"


# -- synthetic: states no path at 064f930 produces -----------------------------------------

def _at_done(monkeypatch, inject):
    """Run ``inject(session)`` when the orchestrator replies DONE, before
    the DONE site's first move."""
    from quadratus import session as S
    next_task = S.Session.next_task

    def patched(self):
        spec = next_task(self)
        if spec is None:
            inject(self)
        return spec
    monkeypatch.setattr(S.Session, "next_task", patched)


def test_synthetic_an_active_run_level_fact_at_done_completes_today(tmp_path, monkeypatch, rounds):
    """Characterisation, not a reachable defect: ``completion_blockers``
    reads tasks only, so the engine completes over an active run-level
    fact. The candidate returns the same and names it. The typed record
    disagrees. No path produces this state (see the reachability tests
    above and docs/review/completion-session-seams.md)."""
    from tests.test_parallel_tasks import BATCH, Orchestrated
    from tests.test_parallel_tasks import _session as parallel_session
    _at_done(monkeypatch, lambda s: s.run_outcome.note("denial", "injected run-level denial"))
    session, _ = parallel_session(tmp_path, Orchestrated([BATCH, "DONE"], lead_delay=0))
    session.run(max_tasks=4)
    decisions, _ = _replay_all(rounds)
    assert session.completed and decisions[-1].status == COMPLETE
    assert decisions[-1].divergences == ("run-level active denial fact is not a completion blocker today: "
                                         "injected run-level denial",)
    assert not typed_completed(session.run_outcome, session.task_outcomes, [])


def _capped_done(tmp_path, monkeypatch, inject):
    from tests.lifecycle import harness as H
    from tests.lifecycle.test_lifecycle_matrix import DECL_T1, FILES, Script
    from tests.lifecycle.test_named_stops import _capped_lead, _settings
    _at_done(monkeypatch, inject)

    def orchestrator(call, replay):
        return DECL_T1 if len(replay.of("orchestrator")) == 1 else "DONE"
    return H.run(tmp_path, monkeypatch, Script(orchestrator=orchestrator, lead=_capped_lead), files=FILES,
                 max_tasks=4, settings=_settings(), record_complete=False)


def test_synthetic_typed_only_partial_blocks_without_being_named(tmp_path, monkeypatch, rounds):
    """Characterisation: the legacy set loses t1 before the DONE site. The
    engine still refuses, but its reasons read the legacy set and name
    nothing. The candidate reproduces the text and the mismatch note the
    session records, and reports the omission."""
    _capped_done(tmp_path, monkeypatch, lambda s: s._partial_tasks.clear())
    decisions, session = _replay_all(rounds)
    final = decisions[-1]
    assert session.stop_reason == ("DoneWithOpenWork: the orchestrator reported DONE, but the record shows no "
                                   "single open item; see the task outcomes. Work preserved.")
    assert final.stop.kind == "cap" and final.status == INCOMPLETE
    assert final.partial_mismatches == (("t1", "partial: typed True, legacy False"),)
    assert final.divergences == ("typed-only partial work (t1) blocks completion but is not named among the "
                                 "stop's reasons, which read the legacy set",)


def test_synthetic_legacy_open_findings_short_circuit_partiality_at_done(tmp_path, monkeypatch, rounds):
    """Characterisation of the conjunction's order: with a legacy open
    finding, partiality is never evaluated, so the typed/legacy mismatch is
    not recorded on the task, and the candidate returns none either."""
    def inject(session):
        session._partial_tasks.clear()
        session.open_findings.append("an injected legacy finding")
    _capped_done(tmp_path, monkeypatch, inject)
    decisions, session = _replay_all(rounds)
    final = decisions[-1]
    assert final.partial_mismatches == () and _partial_notes(session) == set()
    assert final.divergences == ()
    assert session.stop_reason == ("DoneWithOpenWork: the orchestrator reported DONE, but 1 open finding(s), "
                                   "first: an injected legacy finding. Work preserved.")

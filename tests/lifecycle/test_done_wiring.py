"""How the session applies the completion decision (map P3.4 item 16; Codex
review matrix 5863176589).

The candidate's own tests prove the decision; these prove the wiring, whole
controller or whole Session: which effectful callbacks the session makes at
each DONE path, in what order and how often; that a mutation made inside a
callback is read by the decision, as today's conjunction read it; that the
E2 re-read makes no callback twice and writes its stop and notes once; and
that each send-back moves the counter, the refusal and the non-terminal fact
exactly once. Spies only record and pass through; mutations inside a
callback are injected, a controller invariant.

One callback differs from the legacy branches, on purpose: with the
requirements ledger off, legacy still called ``_requirements_satisfied``,
whose first statement returns True; the decision skips that step. The
no-op is pinned below, so the skipped call cannot hide an effect.
"""

import pytest

from quadratus.artifacts import ArtifactStore
from quadratus.session import Session
from tests.lifecycle import harness as H
from tests.lifecycle.test_lifecycle_matrix import DECL_T1, FILES, Script

CALLBACKS = ("_confirm_goal_met", "_requirements_satisfied", "_verify_dependencies", "_note_replaced_evidence")


@pytest.fixture
def calls(monkeypatch):
    """Every DONE-site callback and progress note, in order."""
    log = []
    for name in CALLBACKS:
        original = getattr(Session, name)

        def spy(self, *args, _name=name, _original=original):
            if _name != "_verify_dependencies" or args == ("at DONE",):
                log.append(_name)
            return _original(self, *args)
        monkeypatch.setattr(Session, name, spy)
    note = Session._note

    def noting(self, message):
        log.append(("note", message))
        return note(self, message)
    monkeypatch.setattr(Session, "_note", noting)
    return log


def _callbacks(log):
    return [c for c in log if isinstance(c, str)]


def _notes(log, text):
    return [c for c in log if isinstance(c, tuple) and text in c[1]]


def _then_done(first):
    def orchestrator(call, replay):
        return first if len(replay.of("orchestrator")) == 1 else "DONE"
    return orchestrator


# -- the DONE reply ------------------------------------------------------------------

def test_a_clean_done_makes_the_dependency_check_once_then_the_guard(tmp_path, monkeypatch, calls):
    replay = H.run(tmp_path, monkeypatch, Script(orchestrator=_then_done(DECL_T1)), files=FILES, max_tasks=3)
    assert replay.result.completed
    assert _callbacks(calls) == ["_verify_dependencies", "_note_replaced_evidence"]
    assert len(_notes(calls, "the orchestrator reports the goal met")) == 1


@pytest.mark.requirements_ledger
def test_each_send_back_moves_the_counter_refusal_and_fact_once(tmp_path, calls):
    from tests.test_requirements_ledger import PLAN
    from tests.test_requirements_ledger import Script as LedgerScript
    met = "R1: MET - app.py\nR2: MET - templates/index.html\nR3: MET - app.py"
    script = LedgerScript([PLAN, "DONE", "KIND: frontend simple\nAdd the button.\nCOVERS: R2, R3", "DONE"],
                          audits=[met])
    session = Session("Build the preview", ArtifactStore(tmp_path / "a"), script)
    refusals = []
    requirements = session._requirements_satisfied

    def watching():
        result = requirements()
        refusals.append(session._done_refusal)
        return result
    session._requirements_satisfied = watching
    session.run(max_tasks=6)
    assert session.completed
    assert _callbacks(calls) == ["_requirements_satisfied", "_requirements_satisfied",
                                 "_verify_dependencies", "_note_replaced_evidence"]
    assert session._requirement_reopens == 1
    notes = [f.detail for f in session.run_outcome.facts if not f.terminal]
    assert notes == ["DONE sent back: requirements open"], "one non-terminal fact for one send-back"
    assert refusals[0] and "not covered" in refusals[0], "the check's own refusal reached the orchestrator"


@pytest.mark.requirements_ledger
def test_a_mutation_inside_the_requirements_check_is_read(tmp_path, calls):
    """Today's conjunction read ``open_findings`` after the requirements check."""
    from tests.test_requirements_ledger import PLAN
    from tests.test_requirements_ledger import Script as LedgerScript
    met = "R1: MET - app.py\nR2: MET - templates/index.html\nR3: MET - app.py"
    plan = PLAN.replace("COVERS: R1", "COVERS: R1, R2, R3")
    session = Session("Build the preview", ArtifactStore(tmp_path / "a"),
                      LedgerScript([plan, "DONE"], audits=[met]))
    requirements = session._requirements_satisfied

    def adding():
        result = requirements()
        if result:
            session.open_findings.append("injected during the requirements check")
        return result
    session._requirements_satisfied = adding
    session.run(max_tasks=6)
    assert not session.completed and session.run_outcome.done_accepted
    assert session.stop_reason == ("DoneWithOpenWork: the orchestrator reported DONE, but 1 open finding(s), "
                                   "first: injected during the requirements check. Work preserved."), \
        session.stop_reason
    assert _callbacks(calls) == ["_requirements_satisfied", "_verify_dependencies"], "no guard after open work"


# -- the cap ---------------------------------------------------------------------------

def test_the_cap_asks_the_goal_once_before_anything_else(tmp_path, monkeypatch, calls):
    replay = H.run(tmp_path, monkeypatch, Script(orchestrator=_then_done(DECL_T1)), files=FILES, max_tasks=1)
    assert replay.result.completed, replay.result.error
    assert _callbacks(calls) == ["_confirm_goal_met", "_note_replaced_evidence"], "no dependency step at the cap"


def test_a_mutation_inside_the_goal_question_is_read(tmp_path, monkeypatch, calls):
    """Today's cap conjunction read ``open_findings`` after the goal question."""
    confirm = Session._confirm_goal_met

    def adding(self):
        self.open_findings.append("injected during the goal question")
        return confirm(self)
    monkeypatch.setattr(Session, "_confirm_goal_met", adding)
    replay = H.run(tmp_path, monkeypatch, Script(orchestrator=_then_done(DECL_T1)), files=FILES, max_tasks=1,
                   record_complete=False)
    assert not replay.result.completed
    assert replay.result.error.startswith("GoalUnconfirmedAtCap: the task cap (1) was reached, but 1 open "
                                          "finding(s), first: injected during the goal question"), \
        replay.result.error
    assert _callbacks(calls).count("_confirm_goal_met") == 1


# -- the E2 re-read ----------------------------------------------------------------------

def test_the_e2_re_read_makes_no_callback_twice_and_names_the_stop_once(tmp_path, monkeypatch, calls):
    from tests.lifecycle.test_replaced_evidence import _run
    replay = _run(tmp_path, monkeypatch, rewrite=True)
    assert replay.result.error.startswith("CompletionUnproven: DONE was accepted"), replay.result.error
    assert _callbacks(calls) == ["_verify_dependencies", "_note_replaced_evidence"], _callbacks(calls)
    assert len(_notes(calls, "completion refused")) == 1
    assert len(_notes(calls, "the orchestrator reports the goal met")) == 1
    stops = [f for f in replay.workflow["run"]["facts"] if f.get("legacy") == "CompletionUnproven"]
    assert len(stops) == 1


def test_the_requirements_check_is_a_no_op_with_the_ledger_off(tmp_path, calls):
    """The one legacy callback the wired path skips changes nothing."""
    session = Session("goal", ArtifactStore(tmp_path / "a"), lambda *a, **k: "DONE")
    assert not session.config.requirements_ledger
    session._done_refusal = "before"
    before = (list(session.requirement_reviews), dict(session.memory.ledger.requirement_status),
              list(session.findings), list(session.open_findings))
    assert session._requirements_satisfied() is True
    assert session._done_refusal == "before" and not _notes(calls, "")
    assert (list(session.requirement_reviews), dict(session.memory.ledger.requirement_status),
            list(session.findings), list(session.open_findings)) == before

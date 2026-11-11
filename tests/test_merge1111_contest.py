"""Contests under the merged engine (merge of 2026-11-11).

The review batch's contest (quadratus.contest, ``--compete complex``) was
written against that line's task flow and had never run under the
development line's machinery: the requirements ledger with proof scenarios,
operation records in the job store, the finish held from ordinary work, the
effort policy and the two-seat brain trust. These tests hold what the merge
had to add for it to work there. The contest's own rules are held in
tests/test_review1110_compete.py and are unchanged.
"""

import functools
import json
from pathlib import Path

import pytest

from quadratus import effort
from quadratus.project_run import run_project
from quadratus.run_budget import FinishReserved, RunBudget, RunLimits
from quadratus.session import _delivers
from tests import test_review1110_compete as arena
from tests.lifecycle import harness as H
from tests.lifecycle.test_lifecycle_matrix import FILES, FIXED
from tests.lifecycle.test_proof_binding import CASE, _acceptance, _proofs, _scope, _script

OPUS, SOL, GROK = arena.OPUS, arena.SOL, arena.GROK
#: Fails the fixture's own test, so the gate separates the two drafts.
BROKEN = "def add(a, b):\n    return a - b\n"
REFUSED = "Ordinary call refused to keep the finish: goal, audit need 2 call(s)"


# -- 1. the whole controller, with the requirements ledger on ----------------------------------

def _plan():
    scope = _scope(proofs=_proofs("R1"), max_lines=40)
    return ("REQUIREMENTS:\nR1: add(a, b) returns the sum\nKIND: backend complex\n"
            f"SCOPE: {json.dumps(scope)}\nImplement add in app.py.\nCOVERS: R1")


def _entrants(said=""):
    """The routed lead (claude) breaks the check; the challenger (codex)
    passes it. Both bind the task's scenario, each in its own copy."""
    def lead(call, replay):
        H.write(call, {"app.py": BROKEN if call.vendor == "claude" else FIXED})
        more = said if call.vendor == "codex" else ""
        return f'Implemented add.\nPROVES: P1 - {CASE}{more}\nCHANGED: ["app.py"]'
    return lead


def _contested(tmp_path, monkeypatch, lead):
    monkeypatch.setattr(H, "run_project", functools.partial(run_project, compete="complex"))
    replay = H.run(tmp_path, monkeypatch, _script(_plan(), lead), files=FILES, max_tasks=2, record_complete=False)
    return replay, H.result_json(replay)


@pytest.mark.requirements_ledger
def test_the_winning_drafts_proof_binding_stands_and_the_run_completes(tmp_path, monkeypatch):
    """Before the merge fix the challenger's PROVES line was read only in its
    own session, which holds none of the task's scenarios: P1 stayed unbound
    in the project's session and a run that completes with one lead ended
    RequirementsUnmet."""
    replay, record = _contested(tmp_path, monkeypatch, _entrants())
    (held,) = record["contests"]
    assert held["formed"] and held["decided_by"] == "gate" and held["winner_model"] == SOL
    leads = replay.of("lead")
    assert sorted(c.vendor for c in leads) == ["claude", "codex"]
    assert all(Path(c.cwd).resolve() != replay.project.resolve() for c in leads), "both drafts in private copies"
    assert (replay.project / "app.py").read_text() == FIXED
    (row,) = _acceptance(replay)
    assert row["binding"] == "bound" and row["cases"] == [CASE] and row["execution"] == "ran"
    assert (row["binding_history"][-1]["author"], row["binding_history"][-1]["role"]) == (SOL, "lead")
    # The winner is reviewed by the routed lead, the other seat of the trust.
    assert [c.vendor for c in replay.of("collaborator")] == ["claude"]
    assert replay.result.completed, replay.result.error


@pytest.mark.requirements_ledger
def test_the_merge_and_both_entrants_calls_are_on_the_operation_record(tmp_path, monkeypatch):
    replay, record = _contested(tmp_path, monkeypatch, _entrants())
    operations = record["operations"]
    assert all(op["status"] == "settled" for op in operations), "nothing is left for a resume to guess at"
    (merge,) = [op for op in operations if op["purpose"] == "merge"]
    assert merge["allowed_effects"] == ["project-write"] and merge["outcome"] == "completed"
    writers = [op for op in operations if op["purpose"] == "model call" and op["allowed_effects"] == ["project-write"]]
    assert len(writers) == 2 and len({op["owner_id"] for op in writers}) == 2, "one editing call per entrant"
    assert merge["owner_id"] not in {op["owner_id"] for op in writers}, "the project's session merged, no entrant"
    assert record["job_store"]["refused"] is None


@pytest.mark.requirements_ledger
def test_the_winners_not_run_line_gets_this_sessions_row_and_id(tmp_path, monkeypatch):
    """The entrant's session numbered its NOT RUN row O1, which in the
    project's session is the task's proof scenario. Read where the task
    lives, the item gets its own row, id, gate and checks there."""
    replay, record = _contested(tmp_path, monkeypatch,
                                _entrants("\nNOT RUN: tests/test_browser.py - no browser here"))
    (entry,) = record["unexecuted_acceptance"]
    assert (entry["task"], entry["author"], entry["role"], entry["item"]) == ("t1", SOL, "lead",
                                                                             "tests/test_browser.py")
    rows = {row["oid"]: row for row in record["evidence_obligations"]}
    assert rows[entry["oid"]]["kind"] == "not_run" and rows[entry["oid"]]["behavior"] == "tests/test_browser.py"
    (scenario,) = _acceptance(replay)
    assert scenario["oid"] != entry["oid"] and scenario["binding"] == "bound"
    assert entry["after_check"] == 0 and entry["requirements"] == ["R1"]
    assert not replay.result.completed, "what the winner said it did not run still holds its requirement"


# -- 2. the merged roster: two seats on the trust ----------------------------------------------

def test_on_the_two_seat_trust_a_tie_has_no_judge_and_the_smaller_diff_decides(tmp_path):
    """grok:default left the brain trust on the development line (owner,
    2026-11-01). A contest still forms between the two seats; a tie the
    measurements leave has no third vendor to break it, and says so."""
    longer = "    difference = a - b\n    return difference"
    script = arena.Script([arena._task(), "DONE"], {OPUS: arena._sub(longer), SOL: arena._sub()})
    session, project = arena._session(tmp_path, script, compete="complex")
    assert session.brain_trust == [OPUS, SOL]
    session.run(max_tasks=3)
    record = arena._held(session)
    assert record["formed"] and record["decided_by"] == "size" and record["winner_model"] == SOL
    assert record["judge"] == dict(model=None, verdict=None, reason="",
                                   note="no verdict: no brain-trust member from a third vendor is available")
    assert not script.of("judge") and script.of("review") == [(OPUS, "review", "project", False)]
    assert "difference" not in (project / "calc.py").read_text() and session.completed


def test_after_a_challenger_wins_the_tasks_route_names_its_reviewers_not_itself(tmp_path):
    """The route's reviewers are who a repaired test is judged by and what
    the budget plans for. The routed lead's list named the winner."""
    script = arena.Script([arena._task(), "DONE"], {OPUS: arena._sub("    return a - b  # BROKEN"), SOL: arena._sub()})
    session, project = arena._session(tmp_path, script, compete="complex")
    session.run(max_tasks=3)
    assert arena._held(session)["winner_model"] == SOL
    assert session._route_peers == [OPUS] and session._comparison_reviewer(SOL) == OPUS


def test_a_packet_extension_request_is_not_a_delivery():
    request = "PACKET EXTENSION: 20000\nNECESSARY: the map notes\nWHY NOT SHORTER: every note is cited"
    assert not _delivers(request, [OPUS, SOL])
    assert _delivers("Added sub.\nPACKET EXTENSION: was not needed\nCHANGED: []", [OPUS, SOL])


# -- 3. a call refused to keep the finish ------------------------------------------------------

@pytest.mark.parametrize("refused, winner", [(SOL, OPUS), (OPUS, SOL)])
def test_an_entrant_refused_to_keep_the_finish_only_loses_and_the_run_goes_on(tmp_path, refused, winner):
    """FinishReserved spends and latches nothing. Read as the run's stop, a
    challenger's refusal ended a run the routed lead alone carries on."""
    drafts = {OPUS: arena._sub(), SOL: arena._sub(), refused: arena._raises(FinishReserved(REFUSED))}
    script = arena.Script([arena._task(), "DONE"], drafts)
    session, project = arena._session(tmp_path, script, compete="complex")
    session.run(max_tasks=3)
    record = arena._held(session)
    assert record["decided_by"] == "completed" and record["winner_model"] == winner and record["stopped"] == ""
    assert session.finish_refusals == [dict(task="t1", what=f"contest draft by {refused}", detail=REFUSED)]
    facts = session.task_outcomes[-1].facts
    assert [(f.kind, f.terminal) for f in facts if "contest draft" in f.detail] == [("unverified", False)]
    assert not any(f.kind == "budget" for f in facts), "one record of the refusal, as a finish refusal"
    assert "def sub" in (project / "calc.py").read_text() and session.completed


def test_both_entrants_refused_is_the_task_a_single_lead_would_not_have_started(tmp_path):
    stop = arena._raises(FinishReserved(REFUSED))
    script = arena.Script([arena._task(), "DONE"], {OPUS: stop, SOL: stop})
    session, project = arena._session(tmp_path, script, compete="complex")
    session.run(max_tasks=3)
    record = arena._held(session)
    assert record["winner"] is None and record["decided_by"] == "none usable" and record["merged"] == []
    assert [entry["task"] for entry in session.not_started] == ["t1"]
    assert [r["what"] for r in session.finish_refusals] == [f"first editing call by {OPUS}"]
    assert (project / "calc.py").read_text() == arena.START and not session.completed


def test_a_judge_refused_to_keep_the_finish_is_a_tie_nobody_broke(tmp_path, monkeypatch):
    from quadratus import registry
    monkeypatch.setitem(registry.MODE_ROSTERS["adversarial"], "peers", [OPUS, SOL, GROK])
    longer = "    difference = a - b\n    return difference"
    script = arena.Script([arena._task(), "DONE"], {OPUS: arena._sub(longer), SOL: arena._sub(), GROK: arena._sub()},
                          judge=FinishReserved(REFUSED))
    session, project = arena._session(tmp_path, script, compete="complex")
    session.run(max_tasks=3)
    record = arena._held(session)
    assert record["decided_by"] == "size" and record["winner_model"] == SOL and record["stopped"] == ""
    assert record["judge"] == dict(model=GROK, verdict=None, reason="",
                                   note="no verdict: the judge's call was not started, to keep the run's finish")
    assert dict(task="t1", what="contest judge", detail=REFUSED) in session.finish_refusals
    assert len(script.of("judge")) == 1 and session.completed


def test_a_contest_forms_only_when_the_budget_covers_it_after_the_held_finish():
    """The development line holds one call and one full reserve for each
    finishing judgment. A contest's three calls are counted after them."""
    limits = dict(max_reported_tokens=1500, reserve_tokens_per_call=300)
    calls = RunBudget(RunLimits(max_calls=4, **limits))
    assert calls.covers(3)
    calls.protect_finish(("goal", "audit"))
    assert not calls.covers(3) and calls.covers(2), "two of the four calls are the finish's"
    tokens = RunBudget(RunLimits(max_calls=9, max_reported_tokens=1400, reserve_tokens_per_call=300))
    tokens.protect_finish(("goal", "audit"))
    assert not tokens.covers(3) and tokens.covers(2), "five reserves of 300 do not fit in 1400"
    enough = RunBudget(RunLimits(max_calls=5, **limits))
    enough.protect_finish(("goal", "audit"))
    assert enough.covers(3) and enough.snapshot()["reserved_attempts"] == 0
    enough.release_finish("goal", "the goal question will not be asked")
    assert enough.covers(4), "a stage the run stopped holding is not counted"
    short = RunBudget(RunLimits(max_calls=9, wall_seconds=200, **limits))
    short.protect_finish(("goal", "audit"))
    assert not short.covers(1), "the wall held for the finish leaves an ordinary call no time"


# -- 4. the judge's seat under the development line's role tables ------------------------------

def test_the_judge_is_a_judging_seat_for_the_effort_policy():
    decision = effort.select(role=arena.contest.JUDGE_ROLE, vendor="grok", complexity="complex", kind="backend")
    assert decision.level == "high" and "review" in decision.reasons[0]
    assert not any("unclassified" in reason for reason in decision.reasons)


def test_the_judge_makes_no_native_delegation(monkeypatch, tmp_path):
    from quadratus.config import Settings
    from quadratus.delegation import invocation
    from quadratus.runtime import JUDGE_ROLE, NO_NATIVE_DELEGATION_ROLES, Fleet
    assert JUDGE_ROLE in NO_NATIVE_DELEGATION_ROLES
    monkeypatch.setattr("shutil.which", lambda _: "/unused/cli")
    views = []
    monkeypatch.setattr(Fleet, "_generate",
                        lambda self, key, provider, prompt, system, **kw: views.append(provider) or "ok")
    (tmp_path / "a.py").write_text("x = 1\n")
    fleet = Fleet(Settings(backend="cli", lead_max_turns=20), project=tmp_path)
    with invocation("t1", JUDGE_ROLE):
        assert fleet.invoke(OPUS, "p") == "ok"
    assert views[0].native_fanout_off is True and views[0].max_turns == 20

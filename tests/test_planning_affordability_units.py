"""Cycle03 (PR73 6710677736): every planner re-ask inside one planning
decision is priced against the continuation it plans, and a refused re-ask
buys no call. Completion-cycle02's own numbers: 6,000,000 tokens, a 200,000
reserve, a 500,000 write-granted lead exposure, the goal question and the
audit held, 4,600,131 reported before planning call 36, which reported
297,576 and left 702,293; call 37 (a FETCH re-ask) reported 347,776.

A real Session plans with a scripted invoke; the budget grows by the
planning call's size per call, as the real one does. The lifecycle controls
(real controller and RunBudget) are in tests/lifecycle/test_affordable_planning.py."""
from types import SimpleNamespace

import pytest

from quadratus.artifacts import ArtifactStore
from quadratus.session import _REASK_CLOSED, PlanningUnaffordable, Session, SessionConfig

PLANNING = 297_576
BEFORE_36 = 4_600_131
FETCH_OFFER = "reply with exactly 'FETCH: <artifact-id>'"


class _Exhausted(RuntimeError):
    window_exhausted = True


def _session(tmp_path, replies, *, base=BEFORE_36, editing_work=True, ask=None, exhaust_first=False):
    calls, prompts = [], []

    def invoke(key, prompt, *a, **k):
        prompts.append(prompt)
        if exhaust_first and not calls and len(prompts) == 1:
            raise _Exhausted("window spent")
        calls.append(key)
        return replies.pop(0)
    session = Session("goal", ArtifactStore(tmp_path / "a"), invoke,
                      config=SessionConfig(project=None, ask_operator=ask))
    session.history.append(SimpleNamespace(task_id="t5", summary="closed with its check failing", description="x"))

    class Budget:
        def snapshot(self):
            return dict(limits=dict(max_calls=60, max_reported_tokens=6_000_000, reserve_tokens_per_call=200_000,
                                    wall_seconds=5400),
                        reserved_attempts=35 + len(calls), reported_tokens=base + PLANNING * len(calls),
                        elapsed_seconds=2000.0, finish_protection=dict(pending=["audit", "goal"],
                                                                       editing_exposure=500_000,
                                                                       held_wall_seconds=300))

        def finish_pending(self):
            return ["audit", "goal"]
    session._budget = lambda: Budget()
    session._last_planning_tokens = lambda: PLANNING
    session._writes = lambda: True
    session._max_tasks, session._slots_used = 8, 5
    session._editing_work = (lambda: ["task t5's last check still fails (attempt 2)"]) if editing_work else (lambda: [])
    return session, calls, prompts


def test_cycle02s_fetch_is_refused_after_the_planning_call_with_no_second_call(tmp_path):
    session, calls, prompts = _session(tmp_path, ["FETCH: 0e0ec5132f1b", "never"])
    with pytest.raises(PlanningUnaffordable, match=r"planner re-ask \(FETCH\)"):
        session.next_task()
    assert len(calls) == 1, "call 37 is not bought"
    (refusal,) = session.planning_refusals
    # The spent planning call is in the reported total once: 702,293 left,
    # exactly cycle02's figure, and only future calls are priced.
    assert refusal["reported_tokens"] == BEFORE_36 + PLANNING
    assert "702,293 tokens" in refusal["detail"]
    assert refusal["terminal_short"] == {"tokens": PLANNING + 700_000 - 702_293}
    assert refusal["stages"][0] == "planner re-ask (FETCH)"
    assert session._planning_decision is False, "the decision flag is cleared on the way out"


def test_the_same_fetch_with_room_for_the_continuation_is_served(tmp_path):
    session, calls, _ = _session(tmp_path, ["FETCH: 0e0ec5132f1b", "DONE"], base=3_900_000)
    assert session.next_task() is None
    assert len(calls) == 2 and session.planning_refusals == []


def test_with_no_payable_continuation_and_no_known_editing_work_the_budget_decides(tmp_path):
    # Nothing to protect and nothing only an edit can settle: a decision may
    # still come of the re-ask, so the quote refuses nothing.
    session, calls, _ = _session(tmp_path, ["FETCH: 0e0ec5132f1b", "DONE"], base=5_300_000, editing_work=False)
    assert session.next_task() is None
    assert len(calls) == 2 and session.planning_refusals == []


def test_with_no_payable_continuation_and_known_editing_work_the_re_ask_is_refused(tmp_path):
    session, calls, _ = _session(tmp_path, ["FETCH: 0e0ec5132f1b", "never"], base=5_300_000)
    with pytest.raises(PlanningUnaffordable):
        session.next_task()
    assert len(calls) == 1


def test_an_evidence_request_is_priced_before_it_is_served(tmp_path, monkeypatch):
    request = ("EVIDENCE: R1 - the helper closes early\nFILE: tests/test_probe.py\n```python\n"
               "def test_probe():\n    assert True\n```")
    session, calls, _ = _session(tmp_path, [request, "never"])
    served = []
    monkeypatch.setattr(session, "_serve_evidence", lambda r: served.append(r) or "receipt")
    with pytest.raises(PlanningUnaffordable, match=r"\(evidence\)"):
        session.next_task()
    assert served == [] and len(calls) == 1, "nothing run, no reassessment, no re-ask"


def test_an_operator_answer_is_recorded_and_its_re_ask_is_priced(tmp_path):
    asked = []
    session, calls, _ = _session(tmp_path, ["ASK: may the public interface change?", "never"],
                                 ask=lambda q: asked.append(q) or "no")
    with pytest.raises(PlanningUnaffordable, match="the operator's answer is recorded as a ruling"):
        session.next_task()
    assert asked == ["may the public interface change?"], "the operator's authority is untouched"
    assert session.memory.ledger.rulings == ["Q: may the public interface change? -- A: no"]
    assert len(calls) == 1


def test_a_correction_re_ask_is_priced(tmp_path):
    session, calls, _ = _session(tmp_path, ["KIND: backend simple\nKIND: frontend simple\nFix it.", "never"])
    with pytest.raises(PlanningUnaffordable, match=r"\(correction\)"):
        session.next_task()
    assert len(calls) == 1


def test_a_fallback_seat_is_held_to_the_same_rule(tmp_path, monkeypatch):
    session, calls, prompts = _session(tmp_path, ["FETCH: 0e0ec5132f1b", "never"], exhaust_first=True)
    seats = iter([SimpleNamespace(key="anthropic:fable"), SimpleNamespace(key="openai:astra")])
    monkeypatch.setattr(session, "seat", lambda: next(seats))
    with pytest.raises(PlanningUnaffordable, match=r"\(FETCH\)"):
        session.next_task()
    assert calls == ["openai:astra"] and len(prompts) == 2, "the fallback's FETCH bought no third call"


def test_the_offer_is_closed_in_words_only_when_the_rule_would_refuse(tmp_path):
    # Before call 36: 999,869 left. The naming call and lead + review fit on
    # the terminal route (997,576); one more planner call first does not.
    session, _, prompts = _session(tmp_path, ["DONE"])
    assert session.next_task() is None
    assert _REASK_CLOSED.split("{why}")[0] in prompts[0] and FETCH_OFFER not in prompts[0]
    open_session, _, open_prompts = _session(tmp_path / "open", ["DONE"], base=3_900_000)
    assert open_session.next_task() is None
    assert FETCH_OFFER in open_prompts[0] and "not available in this decision" not in open_prompts[0]
    idle, _, idle_prompts = _session(tmp_path / "idle", ["DONE"], base=5_300_000, editing_work=False)
    assert idle.next_task() is None and FETCH_OFFER in idle_prompts[0], "no editing work: offered"


def test_planning_is_forfeited_only_with_known_editing_work_and_nothing_payable(tmp_path):
    session, calls, _ = _session(tmp_path, [], base=5_300_000)
    forfeit = session._planning_forfeit(8, 5)
    assert forfeit["site"] == "before planning" and forfeit["slots_unused"] == 3 and not calls
    assert session.planning_refusals[-1]["work"] == ["task t5's last check still fails (attempt 2)"]
    payable, _, _ = _session(tmp_path / "p", [], base=BEFORE_36)
    assert payable._planning_forfeit(8, 5) is None, "the terminal alternative is payable: plan"
    nothing, _, _ = _session(tmp_path / "n", [], base=5_300_000, editing_work=False)
    assert nothing._planning_forfeit(8, 5) is None, "no known editing work: a DONE or the audit may settle it"
    first, _, _ = _session(tmp_path / "f", [], base=5_300_000)
    first.history.clear()
    assert first._planning_forfeit(8, 0) is None, "the first plan is never forfeited"
    unbudgeted, _, _ = _session(tmp_path / "u", [], base=5_300_000)
    unbudgeted._budget = lambda: None
    assert unbudgeted._planning_forfeit(8, 5) is None
    listed, _, _ = _session(tmp_path / "l", [], base=5_300_000)
    listed._explicit_tasks = ["one"]
    assert listed._planning_forfeit(8, 5) is None


def test_the_quote_prices_the_lead_as_dispatch_does(tmp_path):
    # 800,000 left: naming 297,576 + review 200,000 + a write-granted lead at
    # 500,000 does not fit; a read-only lead at the 200,000 reserve does.
    session, _, _ = _session(tmp_path, [], base=4_800_000)
    granted = session._continuation_admission(True, scope="planning")
    session._writes = lambda: False
    read_only = session._continuation_admission(True, scope="planning")
    assert granted["route"] == "write-granted" and not granted["feasible"]
    assert read_only["route"] == "read-only" and read_only["feasible"], "a read-only lead is at the reserve"


@pytest.mark.parametrize("kind", ["FETCH", "evidence", "operator answer", "correction"])
def test_root_without_known_editing_work_no_re_ask_is_refused_in_the_middle_band(tmp_path, monkeypatch, kind):
    """Root review of d8225fc, item 1, through every re-ask: 702,293 left after
    the planning call pays lead + review (700,000) now, not a re-ask first
    (997,576). With no known editing work nothing is refused: the shared
    budget's admission decides."""
    first = {"FETCH": "FETCH: 0e0ec5132f1b",
             "evidence": ("EVIDENCE: R1 - the helper closes early\nFILE: tests/test_probe.py\n```python\n"
                          "def test_probe():\n    assert True\n```"),
             "operator answer": "ASK: may the public interface change?",
             "correction": "KIND: backend simple\nKIND: frontend simple\nFix it."}[kind]
    session, calls, prompts = _session(tmp_path, [first, "DONE"], editing_work=False, ask=lambda q: "no")
    served = []
    monkeypatch.setattr(session, "_serve_evidence", lambda r: served.append(r) or "receipt")
    assert session.next_task() is None
    assert len(calls) == 2 and session.planning_refusals == [], session.planning_refusals
    assert FETCH_OFFER in prompts[0], "offered: nothing known needs an edit"
    assert len(served) == (1 if kind == "evidence" else 0)


def test_with_known_editing_work_the_same_band_still_refuses(tmp_path):
    session, calls, _ = _session(tmp_path, ["FETCH: 0e0ec5132f1b", "never"])
    with pytest.raises(PlanningUnaffordable):
        session.next_task()
    assert len(calls) == 1

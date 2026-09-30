"""J41: a survey run continues through distinct failures and records each
(Davis, 2026-09-30; SessionConfig.survey).

The operator sets the recovery allowance in advance and the harness spends
it; the orchestrator states a HYPOTHESIS on every re-plan after a failure,
which is recorded and never acted on; the two-unfinished breaker gives way
to a same-cause repeat stop; result.json carries a survey section apart from
the acceptance verdict. Whole-controller replays.
"""

import json

from quadratus.session import SurveyConfig
from tests.lifecycle import harness as H
from tests.lifecycle.test_lifecycle_matrix import DECL_T1, FILES, FIXED, T1, Script

WIDE_T1 = {**T1, "permitted_paths": T1["permitted_paths"] + ["README.md"]}


def _continue(after, hypothesis, text="Finish add."):
    return ("KIND: architect complex\nSCOPE: " + json.dumps(WIDE_T1) + f"\n{text}\nCONTINUES: {after}"
            + (f"\nHYPOTHESIS: {hypothesis}" if hypothesis else ""))


def _plan(*replies):
    replies = list(replies)

    def orchestrator(call, replay):
        return replies.pop(0) if replies else "DONE"
    return orchestrator


def _task(replay, tid):
    return next(t for t in replay.workflow["tasks"] if t["task_id"] == tid)


def _survey(replay):
    return H.result_json(replay)["survey"]


def test_distinct_failures_keep_the_run_going_and_are_all_recorded(tmp_path, monkeypatch):
    """t1 overruns its scope, t2 answers with an unparsed request, t3 finishes."""
    def lead(call, replay):
        if call.task == "t1":
            H.write(call, {"app.py": FIXED, "other.py": "x\n"})
            return 'Did both.\nCHANGED: ["other.py", "app.py"]'
        if call.task == "t2":
            return 'WORKER {"errand":"code"}'
        H.write(call, {"other.py": ""})
        return 'Cleaned up.\nCHANGED: ["other.py"]'
    plan = _plan(DECL_T1,
                 _continue("t1", "t1 wrote outside its scope; t2 keeps to the listed files"),
                 _continue("t2", "t2 sent a malformed worker request; t3 does the work directly", "Remove other.py."))
    replay = H.run(tmp_path, monkeypatch, Script(orchestrator=plan, lead=lead), files=FILES, max_tasks=5,
                   survey=SurveyConfig(recovery_tasks=4), record_complete=False)
    assert [c.task for c in replay.of("lead")] == ["t1", "t2", "t3"], "no breaker after two distinct failures"
    assert _task(replay, "t1")["closed_as"] == "failed" and _task(replay, "t2")["closed_as"] == "failed"
    survey = _survey(replay)
    assert survey["recovery_tasks"] == 4 and survey["recovery_used"] == 2
    assert survey["unique_causes"] == ["channel", "scope"]
    assert [h["task"] for h in survey["hypotheses"]] == ["t2", "t3"]
    assert survey["hypotheses"][0]["after"] == "t1" and "outside its scope" in survey["hypotheses"][0]["text"]
    assert survey["repeats"] == []
    assert replay.artifacts("hypothesis")
    prompt = replay.of("orchestrator")[1].prompt
    assert "HYPOTHESIS:" in prompt and "4 continuation or repair task(s) remain" in prompt
    assert "HYPOTHESIS" not in replay.of("orchestrator")[0].prompt, "only after a failure"


def test_a_missing_hypothesis_after_a_failure_is_sent_back_once(tmp_path, monkeypatch):
    def lead(call, replay):
        if call.task == "t1":
            H.write(call, {"app.py": FIXED, "other.py": "x\n"})
            return 'Did both.\nCHANGED: ["other.py", "app.py"]'
        H.write(call, {"other.py": ""})
        return 'Cleaned up.\nCHANGED: ["other.py"]'
    plan = _plan(DECL_T1, _continue("t1", None), _continue("t1", "t1 overran; keep to the files"))
    replay = H.run(tmp_path, monkeypatch, Script(orchestrator=plan, lead=lead), files=FILES, max_tasks=5,
                   survey=SurveyConfig(recovery_tasks=4), record_complete=False)
    prompts = [c.prompt for c in replay.of("orchestrator")]
    assert "--- TASK SENT BACK ---" in prompts[2] and "HYPOTHESIS" in prompts[2]
    assert [c.task for c in replay.of("lead")] == ["t1", "t2"]


def test_a_same_cause_repeat_with_nothing_new_changed_stops_the_run(tmp_path, monkeypatch):
    def lead(call, replay):
        H.write(call, {"app.py": FIXED, "other.py": f"{call.task}\n"})
        return 'Did both.\nCHANGED: ["other.py", "app.py"]'
    plan = _plan(DECL_T1, _continue("t1", "try again"), _continue("t2", "and again"))
    replay = H.run(tmp_path, monkeypatch, Script(orchestrator=plan, lead=lead), files=FILES, max_tasks=5,
                   survey=SurveyConfig(recovery_tasks=4), record_complete=False)
    assert [c.task for c in replay.of("lead")] == ["t1", "t2"], "the repeat stops before a third"
    assert replay.result.error.startswith("SurveyRepeatStop: t2 continued t1 and failed the same way (scope)")
    assert _survey(replay)["repeats"] and not replay.result.completed


def test_the_allowance_stops_the_run_before_one_repair_too_many(tmp_path, monkeypatch):
    def lead(call, replay):
        if call.task == "t1":
            H.write(call, {"app.py": FIXED, "other.py": "x\n"})
            return 'Did both.\nCHANGED: ["other.py", "app.py"]'
        return 'WORKER {"errand":"code"}'
    plan = _plan(DECL_T1, _continue("t1", "h1"), _continue("t2", "h2"))
    replay = H.run(tmp_path, monkeypatch, Script(orchestrator=plan, lead=lead), files=FILES, max_tasks=5,
                   survey=SurveyConfig(recovery_tasks=1), record_complete=False)
    assert [c.task for c in replay.of("lead")] == ["t1", "t2"]
    assert replay.result.error.startswith("SurveyAllowanceSpent: the recovery allowance of 1 task(s) is spent")
    assert _survey(replay)["recovery_used"] == 1


def test_an_ordinary_run_has_no_survey_section_and_keeps_its_breaker(tmp_path, monkeypatch):
    replay = H.run(tmp_path, monkeypatch, Script(), files=FILES, max_tasks=1)
    assert H.result_json(replay)["survey"] is None
    assert "HYPOTHESIS" not in replay.of("orchestrator")[0].prompt

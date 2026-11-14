"""Truth canaries for an audit that did not run (2026-11-10 review,
reports/session_second_half.md item 4).

An audit that did not happen is its own result: the run stops under the name
AuditUnavailable with every requirement still "covered". The independent
check (tools/truth_check.py) must never read that as success, from a real
run or from a record that claims more than it shows. Sibling of
test_truth_canaries.py; offline, scripted replies below the vendor CLIs.
"""

import pytest

from tests.lifecycle import harness as H
from tests.lifecycle.replies import judging_scenarios
from tests.lifecycle.test_check_debt import IMPLEMENT, MET, REQS, _fixes, _plan
from tests.lifecycle.test_lifecycle_matrix import ADD_PROOF, ADD_SHOWN, FILES, Script
from tests.lifecycle.test_truth_canaries import _record, truth

pytestmark = pytest.mark.requirements_ledger

RAN = dict(auditor="openai:gpt-5.6-sol", verdicts={"R1": dict(met=True, why="tests/test_a.py")}, lowered=[])
MISSED = dict(auditor="openai:gpt-5.6-sol", result="the auditor call failed: ProviderError: down", ran=False)


def _run(tmp_path, monkeypatch, auditor):
    # The scripted auditor also writes the SUFFICIENT line the development
    # line's audit asks for on the task's executed scenario.
    overrides = dict(orchestrator=_plan(REQS + IMPLEMENT), lead=_fixes,
                     **{"requirements-review": lambda call, replay: "COMPLETE",
                        "auditor": judging_scenarios(auditor, ADD_PROOF, basis=ADD_SHOWN)})
    return H.run(tmp_path, monkeypatch, Script(**overrides), files=FILES, max_tasks=4)


#: An auditor that answers but judges nothing. (A raised transport error
#: carries no usage, which the replay's run budget stops on first; the
#: failed-call route is covered in tests/test_review1110_loop_c.py.)
NO_VERDICT = "I could not open the working directory, so I cannot judge the requirements."


def _down(call, replay):
    return NO_VERDICT


def test_a_real_run_whose_audit_never_ran_stops_named_and_is_never_verified(tmp_path, monkeypatch):
    replay = _run(tmp_path, monkeypatch, _down)
    assert not replay.result.completed
    assert replay.result.error.startswith("AuditUnavailable: the independent requirements audit did not run "
                                          "in 2 attempt(s) at DONE"), replay.result.error
    record = H.result_json(replay)
    assert record["requirements"]["status"] == {"R1": "covered by t1"}, "nothing was written NOT MET"
    assert [a.get("ran") for a in record["requirements"]["audits"]] == [False, False]
    assert len(replay.of("auditor")) == 2 and len(replay.of("lead")) == 1, "retried once; no task spent on it"
    stop = (replay.workflow["run"]["facts"] or [{}])[-1]
    assert (stop["kind"], stop["legacy"]) == ("operator", "AuditUnavailable")
    result = truth.judge(replay.result.run_dir, replay.project)
    assert result["verdict"] != truth.VERIFIED, result
    assert any("the last requirements audit did not run" in r for r in result["unverified"]), result
    assert any("R1 has no met status" in r for r in result["unverified"]), result


def test_a_real_run_whose_audit_ran_on_the_retry_completes_and_is_verified(tmp_path, monkeypatch):
    def flaky(call, replay):
        return NO_VERDICT if len(replay.of("auditor")) == 1 else MET
    replay = _run(tmp_path, monkeypatch, flaky)
    assert replay.result.completed, replay.result.error
    record = H.result_json(replay)
    assert record["requirements"]["status"]["R1"] == "met (audited)"
    assert [("verdicts" in a, a.get("ran")) for a in record["requirements"]["audits"]] == [(False, False),
                                                                                           (True, None)]
    assert len(replay.of("lead")) == 1, "finished work was not sent to be covered again"
    assert truth.judge(replay.result.run_dir, replay.project)["verdict"] == truth.VERIFIED


# Record-level: what a run could write and the check must not accept.

def _requirements(status, audits):
    return dict(listed={"R1": "x"}, status={"R1": status}, audits=audits)


def test_a_record_whose_last_audit_did_not_run_is_unverified_whatever_the_status_says(tmp_path):
    """An earlier audit judged an earlier source; it does not stand in for
    the one that did not run."""
    result = truth.judge(_record(tmp_path, requirements=_requirements("met (audited)", [RAN, MISSED])))
    assert result["verdict"] == truth.UNVERIFIED
    assert any("the last requirements audit did not run (the auditor call failed" in r
               for r in result["unverified"]), result


def test_a_covered_requirement_with_only_a_missed_audit_is_unverified(tmp_path):
    result = truth.judge(_record(tmp_path, requirements=_requirements("covered by t1", [MISSED])))
    assert result["verdict"] == truth.UNVERIFIED
    assert any("R1 has no met status" in r for r in result["unverified"])
    assert any("did not run" in r for r in result["unverified"])


def test_an_audit_that_ran_after_a_miss_is_still_verified(tmp_path):
    """The check is not passing by refusing every record with a miss in it."""
    assert truth.judge(_record(tmp_path, requirements=_requirements("met (audited)", [MISSED, RAN])))[
        "verdict"] == truth.VERIFIED

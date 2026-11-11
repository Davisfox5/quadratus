"""A requirement list nobody reviewed is not a requirement that was not met.

Found by the first run of the session engine against a real CLI after the
2026-11-10 review batch (2026-11-11): one vendor was signed in, so no second
vendor could compare the requirement list with the goal. The task closed, its
check passed, and the run ended ``RequirementsUnmet`` after three more
orchestrator calls, although nothing had judged a requirement. The rule the
batch settled for the audit ("an audit that did not run is not NOT MET") now
holds one step earlier too: DONE goes back once, a second miss is the operator
stop ``AuditUnavailable``, and no status is written.

Offline: scripted replies below the vendor CLIs, as in
test_review1110_loop_c_truth.py.
"""

import pytest

from quadratus.session import Session
from tests.lifecycle import harness as H
from tests.lifecycle.test_check_debt import IMPLEMENT, MET, REQS, _fixes, _plan
from tests.lifecycle.test_lifecycle_matrix import FILES, Script
from tests.lifecycle.test_truth_canaries import truth

pytestmark = pytest.mark.requirements_ledger

#: A reviewer that answers and reviews nothing: neither COMPLETE nor only
#: ADD / AMBIGUOUS lines.
NO_REVIEW = "I could not open the working directory, so I cannot compare the list with the goal."


def _run(tmp_path, monkeypatch, review, auditor=lambda call, replay: MET, **kw):
    overrides = dict(orchestrator=_plan(REQS + IMPLEMENT), lead=_fixes,
                     **{"requirements-review": review, "auditor": auditor})
    return H.run(tmp_path, monkeypatch, Script(**overrides), files=FILES, **{"max_tasks": 4, **kw})


def _stop(replay):
    return (replay.workflow["run"]["facts"] or [{}])[-1]


def test_a_list_no_reviewer_read_stops_for_the_operator_and_is_never_called_unmet(tmp_path, monkeypatch):
    replay = _run(tmp_path, monkeypatch, lambda call, replay: NO_REVIEW)
    assert not replay.result.completed
    error = replay.result.error
    assert error.startswith("AuditUnavailable: the independent requirements audit did not run "
                            "in 2 attempt(s) at DONE"), error
    assert "the requirement list was not reviewed against the goal by another vendor" in error
    assert "No requirement was judged met or not met" in error
    assert "RequirementsUnmet" not in error
    record = H.result_json(replay)
    assert record["requirements"]["status"] == {"R1": "covered by t1"}, "nothing was written NOT MET"
    assert len(replay.of("requirements-review")) == 3, "when the list was numbered, at DONE, and once more"
    assert len(replay.of("auditor")) == 0, "no audit over a list nobody reviewed"
    assert len(replay.of("lead")) == 1, "finished work was not sent to be covered again"
    assert (_stop(replay)["kind"], _stop(replay)["legacy"]) == ("operator", "AuditUnavailable")
    assert truth.judge(replay.result.run_dir, replay.project)["verdict"] != truth.VERIFIED


def test_with_no_second_vendor_at_all_the_stop_names_that(tmp_path, monkeypatch):
    """The live case: no reviewer exists, so no review call is made."""
    monkeypatch.setattr(Session, "_auditor", lambda self: None)
    replay = _run(tmp_path, monkeypatch, lambda call, replay: "COMPLETE")
    assert replay.result.error.startswith("AuditUnavailable: "), replay.result.error
    assert "another vendor: no reviewer available" in replay.result.error
    assert len(replay.of("requirements-review")) == 0 and len(replay.of("auditor")) == 0
    record = H.result_json(replay)
    assert [r["result"] for r in record["requirements"]["reviews"]] == ["no reviewer available"] * 3
    assert record["requirements"]["status"] == {"R1": "covered by t1"}


def test_the_send_back_tells_the_orchestrator_nothing_needs_covering_again(tmp_path, monkeypatch):
    replay = _run(tmp_path, monkeypatch, lambda call, replay: NO_REVIEW)
    after = replay.of("orchestrator")[-1].prompt
    assert "The requirement list has not been reviewed against the goal by another vendor" in after
    assert "No requirement was judged, so none is recorded as not met" in after
    assert "Reply DONE to have the review tried once more" in after


def test_a_review_that_runs_on_the_retry_lets_the_run_complete(tmp_path, monkeypatch):
    def flaky(call, replay):
        # Misses when the list is numbered and at the first DONE; runs at the second.
        return NO_REVIEW if len(replay.of("requirements-review")) <= 2 else "COMPLETE"
    replay = _run(tmp_path, monkeypatch, flaky)
    assert replay.result.completed, replay.result.error
    record = H.result_json(replay)
    assert record["requirements"]["status"]["R1"] == "met (audited)"
    assert len(replay.of("requirements-review")) == 3 and len(replay.of("lead")) == 1
    assert [r["result"].split(":")[0] for r in record["requirements"]["reviews"]] == ["failed", "failed",
                                                                                      "reviewed"]
    assert truth.judge(replay.result.run_dir, replay.project)["verdict"] == truth.VERIFIED


def test_a_review_miss_does_not_spend_the_audits_own_retry(tmp_path, monkeypatch):
    """The review misses once, then runs; the audit then misses once and is
    still asked again, because a review miss is not an audit miss."""
    def flaky_review(call, replay):
        return NO_REVIEW if len(replay.of("requirements-review")) <= 2 else "COMPLETE"

    def flaky_audit(call, replay):
        return "I cannot judge the requirements." if len(replay.of("auditor")) == 1 else MET
    replay = _run(tmp_path, monkeypatch, flaky_review, flaky_audit, max_tasks=6)
    assert replay.result.completed, replay.result.error
    assert len(replay.of("requirements-review")) == 3 and len(replay.of("auditor")) == 2


def test_at_the_task_cap_an_unreviewed_list_is_the_same_operator_stop(tmp_path, monkeypatch):
    replay = _run(tmp_path, monkeypatch, lambda call, replay: NO_REVIEW, max_tasks=1)
    error = replay.result.error
    assert error.startswith("AuditUnavailable: "), error
    assert "RequirementsUnmet" not in error
    assert H.result_json(replay)["requirements"]["status"] == {"R1": "covered by t1"}

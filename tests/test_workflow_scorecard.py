"""Conservative scoring from saved, synthetic records only."""
import hashlib
import json
from copy import deepcopy

import pytest

from quadratus.outcome import TaskOutcome
from tools.workflow_scorecard import score


def _write(run, *, completed, events, attempts=None, seconds=10):
    run.mkdir()
    if attempts is None:
        attempts = sum(event.get("invoked") is True for event in events)
    task = TaskOutcome(task_id="t1", intent="implementation", lead="Sol",
                       contract={"task_id": "t1", "owner": "Sol", "intended_state": "state-1",
                                 "required": {"checks": True, "design_review": True,
                                              "design_collaboration_applicable": True,
                                              "security_verification": False, "settlement": False,
                                              "design_evidence": "none", "design_instruction": "none",
                                              "security_verdict": "none", "operator_limits": "none",
                                              "requirements_ledger": True}},
                       dispatch={"state": "dispatched", "owner": "Sol"},
                       stages=["checks"], edges={"checks": True, "delivered": True, "reviewer": True},
                       checks=[{"passed": True, "output_artifact": "artifact-1", "receipts": []}],
                       source_before="source-1", source_after="source-1", dependency="stable",
                       partial={"changed": []}, closed_as="closed")
    (run / "result.json").write_text(json.dumps({
        "completed": completed, "error": "", "source_fingerprint": "source-1",
        "workflow": {"run": {"done_accepted": completed, "facts": [], "readiness": []},
            "tasks": [task.to_dict()],
            "parity": {"agree": True, "complete": True, "typed_completed": completed,
                       "problems": [], "missing": []}},
        "requirements": {"listed": {"R1": "example"}, "status": {"R1": "met (audited)"}},
        "findings": [], "checks": [{"passed": True}],
        "budget": {"reserved_attempts": attempts, "elapsed_seconds": seconds,
                   "unknown_usage_attempts": 0},
    }))
    (run / "budget.json").write_text(json.dumps({"reserved_attempts": attempts, "elapsed_seconds": seconds,
                                                   "unknown_usage_attempts": 0}))
    (run / "invocations.jsonl").write_text("".join(json.dumps(event) + "\n" for event in events))
    (run / "review.txt").write_text("reviewed checks, evidence, and requirements")
    return run


def _attest(run):
    return {run.name: {"status": "verified", "reviewer": "independent reviewer",
                       "evidence": "review.txt", "source_fingerprint": "source-1",
                       "result_sha256": hashlib.sha256((run / "result.json").read_bytes()).hexdigest()}}


def test_unknown_until_external_attestation_and_matching_source(tmp_path):
    run = _write(tmp_path / "a", completed=True, events=[], attempts=0)
    assert score([run], {})["runs"][0]["verification"] == "unknown"
    claim = _attest(run)
    assert score([run], claim)["runs"][0]["verification"] == "verified"
    claim[run.name]["source_fingerprint"] = "other"
    assert score([run], claim)["runs"][0]["verification"] == "unknown"
    claim = _attest(run)
    (run / "review.txt").unlink()
    assert score([run], claim)["runs"][0]["verification"] == "unknown"
    (run / "review.txt").write_text("")
    assert score([run], claim)["runs"][0]["verification"] == "unknown"


@pytest.mark.parametrize("reviewer", [None, True, 42, [], {}, "", " \t\n"])
def test_verified_attestation_requires_meaningful_reviewer_string(tmp_path, reviewer):
    run = _write(tmp_path / "a", completed=True, events=[], attempts=0)
    claim = _attest(run)
    claim[run.name]["reviewer"] = reviewer
    assert score([run], claim)["runs"][0]["verification"] == "unknown"
    claim[run.name]["reviewer"] = " independent reviewer "
    assert score([run], claim)["runs"][0]["verification"] == "verified"


def test_claim_cannot_override_incomplete_run(tmp_path):
    run = _write(tmp_path / "a", completed=False, events=[], attempts=0)
    assert score([run], _attest(run))["runs"][0]["verification"] == "unknown"


def test_failed_attempts_in_numerator_and_cache_not_double_counted(tmp_path):
    failed = _write(tmp_path / "failed", completed=False, events=[
        {"invoked": True, "outcome": "error", "input_tokens": 100, "output_tokens": 10,
         "cached_input_tokens": 80, "diagnostics": {"vendor_cost_usd": 2}},
    ], attempts=1, seconds=7)
    done = _write(tmp_path / "done", completed=True, events=[
        {"invoked": True, "outcome": "ok", "input_tokens": 200, "output_tokens": 20,
         "cached_input_tokens": 150, "diagnostics": {"vendor_cost_usd": 3}},
    ], attempts=1, seconds=8)
    report = score([failed, done], _attest(done))["summary"]
    assert report["verified_completions"] == 1
    assert report["failed_invoked_calls"] == 1
    assert report["input_tokens"] == 300
    assert report["cached_input_tokens_subset"] == 230
    assert report["budget_elapsed_seconds"] == 15
    assert report["budget_elapsed_seconds_per_verified_completion"] == 15
    assert report["invoked_calls_per_verified_completion"] == 2
    assert report["input_tokens_per_verified_completion"] == 300
    assert report["vendor_spend_per_verified_completion_usd"] == 5


def test_missing_vendor_cost_keeps_spend_unknown_not_seed_price(tmp_path):
    run = _write(tmp_path / "a", completed=True, events=[
        {"invoked": True, "outcome": "ok", "input_tokens": 12, "output_tokens": 4,
         "diagnostics": {}, "cost_usd": 999},
    ], attempts=1)
    row = score([run], _attest(run))["runs"][0]
    summary = score([run], _attest(run))["summary"]
    assert row["observed_vendor_spend_usd"] is None
    assert row["vendor_spend_complete"] is False
    assert summary["vendor_spend_per_verified_completion_usd"] is None


def test_missing_call_and_budget_records_are_unknown(tmp_path):
    run = tmp_path / "empty"
    run.mkdir()
    row = score([run], {})["runs"][0]
    assert row["invoked_calls"] is None
    assert row["budget_elapsed_seconds"] is None
    assert row["verification"] == "unknown"


def test_missing_attempt_event_prevents_full_usage_and_spend(tmp_path):
    run = _write(tmp_path / "a", completed=True, events=[
        {"invoked": True, "outcome": "ok", "input_tokens": 12, "output_tokens": 4,
         "diagnostics": {"vendor_cost_usd": 1}},
    ], attempts=2)
    row = score([run], _attest(run))["runs"][0]
    assert row["invoked_calls"] == 1
    assert row["reserved_attempts"] == 2
    assert row["attempt_event_coverage_complete"] is False
    assert row["input_tokens"] is None
    assert row["vendor_spend_complete"] is False


def test_missing_or_contradictory_workflow_cannot_be_verified(tmp_path):
    run = _write(tmp_path / "a", completed=True, events=[])
    path = run / "result.json"
    original = json.loads(path.read_text())
    for mutation in (
        lambda item: item.pop("workflow"),
        lambda item: item["workflow"].update(error="serialization failed"),
        lambda item: item["workflow"]["parity"].update(agree=False, problems=["mismatch"]),
        lambda item: item["workflow"]["parity"].update(mismatches=["mismatch"]),
        lambda item: item["workflow"]["tasks"][0]["edges"].update(reviewer=False),
        lambda item: item["workflow"]["tasks"][0]["edges"].pop("reviewer"),
        lambda item: item["workflow"]["tasks"][0]["edges"].pop("delivered"),
        lambda item: item["workflow"]["tasks"][0]["checks"].append({"passed": False}),
        lambda item: item["workflow"]["tasks"][0].pop("checks"),
        lambda item: item.pop("checks"),
        lambda item: item.update(checks=[]),
        lambda item: item["requirements"]["status"].update(R1="NOT MET"),
        lambda item: item["findings"].append({"status": "open"}),
    ):
        changed = json.loads(json.dumps(original))
        mutation(changed)
        path.write_text(json.dumps(changed))
        assert score([run], _attest(run))["runs"][0]["verification"] == "unknown"


def test_final_budget_elapsed_and_stale_disagreement(tmp_path):
    run = _write(tmp_path / "a", completed=True, events=[], attempts=0, seconds=30)
    (run / "budget.json").write_text(json.dumps({"reserved_attempts": 0, "elapsed_seconds": 5,
                                                  "unknown_usage_attempts": 0}))
    assert score([run], _attest(run))["runs"][0]["budget_elapsed_seconds"] == 30
    result = json.loads((run / "result.json").read_text())
    result["budget"]["elapsed_seconds"] = 4
    (run / "result.json").write_text(json.dumps(result))
    assert score([run], _attest(run))["runs"][0]["budget_elapsed_seconds"] is None


def test_diagnostics_cache_and_conflicting_cache_are_conservative(tmp_path):
    run = _write(tmp_path / "a", completed=True, events=[
        {"invoked": True, "outcome": "ok", "input_tokens": 100, "output_tokens": 4,
         "cached_input_tokens": None, "diagnostics": {"cached_input_tokens": 80}},
    ])
    assert score([run], _attest(run))["runs"][0]["cached_input_tokens_subset"] == 80
    events = json.loads((run / "invocations.jsonl").read_text())
    events["cached_input_tokens"] = 90
    (run / "invocations.jsonl").write_text(json.dumps(events) + "\n")
    assert score([run], _attest(run))["runs"][0]["cached_input_tokens_subset"] is None


def test_adversarial_completion_matrix(tmp_path):
    run = _write(tmp_path / "a", completed=True, events=[])
    path = run / "result.json"
    baseline = json.loads(path.read_text())
    claim = _attest(run)
    assert score([run], claim)["runs"][0]["verification"] == "verified"
    def drop_review(r, flag):
        required = r["workflow"]["tasks"][0]["contract"]["required"]
        if flag is None:
            required.pop("design_review")
        else:
            required["design_review"] = flag
        edges = r["workflow"]["tasks"][0]["edges"]
        edges.pop("delivered")
        edges.pop("reviewer")

    mutations = (
        lambda r: drop_review(r, None),
        lambda r: drop_review(r, False),
        lambda r: r["workflow"]["tasks"][0]["contract"]["required"].update(checks=None),
        lambda r: r["checks"][-1].update(receipts=[{"id": "required", "required": True,
                                                   "status": "failed"}]),
        lambda r: r["checks"][-1].update(receipts=[{"id": "unknown", "status": "passed"}]),
        lambda r: r["workflow"]["tasks"][0]["facts"].append(
            {"kind": "operator", "detail": "stop", "stage": "checks", "terminal": True,
             "recovered": False, "legacy_route": False, "legacy": None}),
        lambda r: r["workflow"]["run"]["facts"].append(
            {"kind": "operator", "detail": "stop", "stage": "run", "terminal": True,
             "recovered": False, "legacy_route": False, "legacy": None}),
        lambda r: r["workflow"]["tasks"][0].update(closed_as="stopped:ProviderRefusal"),
        lambda r: r.update(error="stopped:ProviderRefusal"),
        lambda r: r["workflow"]["tasks"][0].pop("dispatch"),
    )
    for mutate in mutations:
        record = deepcopy(baseline)
        mutate(record)
        path.write_text(json.dumps(record))
        assert score([run], claim)["runs"][0]["verification"] == "unknown"
        if mutate not in mutations[:2]:
            assert score([run], _attest(run))["runs"][0]["verification"] == "unknown"


def test_valid_continuation_discharges_predecessor_edge(tmp_path):
    run = _write(tmp_path / "a", completed=True, events=[])
    path = run / "result.json"
    record = json.loads(path.read_text())
    first = record["workflow"]["tasks"][0]
    first["closed_as"] = "turn_limited"
    first["edges"]["reviewer"] = False
    first["unsatisfied"] = ["reviewer"]
    second = deepcopy(first)
    second["task_id"] = second["contract"]["task_id"] = "t2"
    second["continues"] = "t1"
    second["closed_as"] = "closed"
    second["edges"]["reviewer"] = True
    second["unsatisfied"] = []
    record["workflow"]["tasks"].append(second)
    path.write_text(json.dumps(record))
    assert score([run], _attest(run))["runs"][0]["verification"] == "verified"
    second["contract"]["intended_state"] = "other-state"
    path.write_text(json.dumps(record))
    assert score([run], _attest(run))["runs"][0]["verification"] == "unknown"


@pytest.mark.parametrize("mutate", [
    lambda r: r["checks"][-1].update(receipts=[{"required": True, "status": "passed",
                                                "source_hash": "other-source"}]),
    lambda r: r["checks"][-1].update(receipts=[{"required": True, "status": "passed",
                                                "source_hash": []}]),
    lambda r: r["workflow"]["tasks"][0]["checks"][-1].update(source="other-source"),
    lambda r: r["workflow"]["tasks"][0]["checks"][-1].update(source=[]),
    lambda r: r["workflow"]["tasks"][0]["checks"][-1].update(
        source="source-1", receipts=[{"status": "passed", "source_hash": "other-source"}]),
    lambda r: r["workflow"]["tasks"][0].update(source_after=[]),
    lambda r: r["findings"].append({"id": "F1", "status": "resolved", "task": []}),
    lambda r: r["findings"].append({"id": "F1", "status": "resolved", "task": ["t1"]}),
    lambda r: r["findings"].append({"id": "F1", "status": "resolved", "task": ""}),
    lambda r: r["findings"].append({"id": "F1", "status": "resolved", "task": "unknown"}),
    lambda r: r["findings"].append({"id": [], "status": "resolved", "task": "t1"}),
    lambda r: r["findings"].append({"status": "resolved", "task": "t1"}),
    lambda r: r["findings"].append({"id": "F1", "status": "resolved", "task": "t1",
                                     "resolved_by": []}),
    lambda r: r["findings"].extend([{"id": "F1", "status": "resolved", "task": "t1"},
                                     {"id": "F1", "status": "resolved", "task": "t1"}]),
])
def test_source_and_finding_schema_rejects_reattested_bad_records(tmp_path, mutate):
    run = _write(tmp_path / "a", completed=True, events=[])
    path = run / "result.json"
    record = json.loads(path.read_text())
    mutate(record)
    path.write_text(json.dumps(record))
    assert score([run], _attest(run))["runs"][0]["verification"] == "unknown"


def test_source_receipts_bind_to_their_own_task_state(tmp_path):
    run = _write(tmp_path / "a", completed=True, events=[])
    path = run / "result.json"
    record = json.loads(path.read_text())
    earlier = record["workflow"]["tasks"][0]
    earlier["source_after"] = "source-before-later-task"
    earlier["checks"][-1].update(source="source-before-later-task", receipts=[
        {"status": "passed", "source_hash": "source-before-later-task"}])
    later = deepcopy(earlier)
    later["task_id"] = later["contract"]["task_id"] = "t2"
    later["source_after"] = "source-1"
    later["checks"][-1].update(source="source-1", receipts=[
        {"status": "passed", "source_hash": "source-1"}])
    record["workflow"]["tasks"].append(later)
    record["checks"][-1]["receipts"] = [
        {"required": True, "status": "passed", "source_hash": "source-1"}]
    path.write_text(json.dumps(record))
    assert score([run], _attest(run))["runs"][0]["verification"] == "verified"


def test_budget_snapshots_and_zero_vendor_cost(tmp_path):
    run = _write(tmp_path / "a", completed=True, events=[
        {"invoked": True, "outcome": "ok", "input_tokens": 10, "output_tokens": 2,
         "cached_input_tokens": 1, "diagnostics": {"vendor_cost_usd": 0}},
    ], attempts=1, seconds=30)
    report = score([run], _attest(run))
    assert report["summary"]["vendor_spend_known_partial_usd"] == 0
    path = run / "result.json"
    original = json.loads(path.read_text())
    for final_budget, persisted_budget in (
        (None, {"elapsed_seconds": 5, "reserved_attempts": 1, "unknown_usage_attempts": 0}),
        ({"elapsed_seconds": 30, "reserved_attempts": 1, "unknown_usage_attempts": 0},
         {"elapsed_seconds": 5, "reserved_attempts": 2, "unknown_usage_attempts": 0}),
        ({"elapsed_seconds": 30, "reserved_attempts": 1, "unknown_usage_attempts": 0},
         {"elapsed_seconds": 5, "reserved_attempts": 1, "unknown_usage_attempts": 1}),
    ):
        changed = deepcopy(original)
        changed["budget"] = final_budget
        path.write_text(json.dumps(changed))
        (run / "budget.json").write_text(json.dumps(persisted_budget))
        row = score([run], _attest(run))["runs"][0]
        if final_budget is None:
            assert row["budget_elapsed_seconds"] is None
            assert row["reserved_attempts"] is None
        assert row["attempt_event_coverage_complete"] is False
        assert row["input_tokens"] is None
        assert row["vendor_spend_complete"] is False


def test_historical_collaboration_absence_stays_unknown(tmp_path):
    run = _write(tmp_path / "historical", completed=True, events=[], attempts=0)
    path = run / "result.json"
    result = json.loads(path.read_text())
    del result["workflow"]["tasks"][0]["contract"]["required"]["design_collaboration_applicable"]
    path.write_text(json.dumps(result))
    before = path.read_bytes()
    report = score([run], _attest(run))
    assert report["runs"][0]["verification"] == "unknown"
    assert report["summary"]["verified_completions"] == 0
    assert path.read_bytes() == before

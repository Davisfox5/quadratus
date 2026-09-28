"""Conservative scoring from saved, synthetic records only."""
import json

from tools.workflow_scorecard import score


def _write(run, *, completed, events, attempts=None, seconds=10):
    run.mkdir()
    if attempts is None:
        attempts = sum(event.get("invoked") is True for event in events)
    (run / "result.json").write_text(json.dumps({
        "completed": completed, "source_fingerprint": "source-1",
        "workflow": {"run": {"done_accepted": completed}, "tasks": [
            {"edges": {"delivered": True, "reviewer": True}, "mismatches": [], "unsatisfied": []}],
            "parity": {"agree": True, "complete": True, "typed_completed": completed}},
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
                       "evidence": "review.txt", "source_fingerprint": "source-1"}}


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

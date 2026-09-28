"""Conservative scoring from saved, synthetic records only."""
import json

from tools.workflow_scorecard import score


def _write(run, *, completed, events, attempts=None, seconds=10):
    run.mkdir()
    if attempts is None:
        attempts = sum(event.get("invoked") is True for event in events)
    (run / "result.json").write_text(json.dumps({"completed": completed, "source_fingerprint": "source-1"}))
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
    assert report["elapsed_seconds"] == 15
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
    assert row["elapsed_seconds"] is None
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

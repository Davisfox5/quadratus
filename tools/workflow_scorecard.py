"""Offline, conservative scorecard for saved Quadratus run directories.

No provider calls, project execution, or acceptance inference occur here.
"""
from __future__ import annotations

import argparse
import json
import math
from decimal import Decimal
from pathlib import Path


def _json(path: Path):
    return json.loads(path.read_text(encoding="utf-8")) if path.is_file() else None


def _jsonl(path: Path):
    if not path.is_file():
        return None
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]


def _number(value):
    return value if isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value) and value >= 0 else None


def _sum_complete(rows, key):
    values = [_number(row.get(key)) for row in rows]
    return sum(values) if all(value is not None for value in values) else None


def _verification(run: Path, result: dict | None, attestations: dict):
    """A saved run cannot certify its own independent completion."""
    claim = attestations.get(run.name)
    if claim is None:
        return "unknown", "no independent attestation"
    if not isinstance(claim, dict):
        return "unknown", "malformed independent attestation"
    if claim.get("status") != "verified":
        return "unverified" if claim.get("status") == "unverified" else "unknown", "external verdict"
    if not result or result.get("completed") is not True:
        return "unknown", "run did not record completion"
    if claim.get("source_fingerprint") != result.get("source_fingerprint") or not claim.get("source_fingerprint"):
        return "unknown", "source identity mismatch"
    evidence = claim.get("evidence")
    if not isinstance(evidence, str) or not evidence:
        return "unknown", "no review evidence"
    evidence_path = Path(evidence)
    if not evidence_path.is_absolute():
        evidence_path = run / evidence_path
    if not evidence_path.is_file():
        return "unknown", "review evidence missing"
    if not claim.get("reviewer"):
        return "unknown", "reviewer missing"
    return "verified", "independent attestation with matching source identity"


def score_run(run: Path, attestations: dict):
    result = _json(run / "result.json")
    budget = _json(run / "budget.json")
    if budget is None and result:
        budget = result.get("budget")
    events = _jsonl(run / "invocations.jsonl")
    verdict, reason = _verification(run, result, attestations)
    invoked = [event for event in events or [] if event.get("invoked") is True]
    failed = [event for event in invoked if event.get("outcome") != "ok"]
    attempts = _number((budget or {}).get("reserved_attempts"))
    usage_unknown = _number((budget or {}).get("unknown_usage_attempts"))
    coverage_complete = events is not None and attempts == len(invoked)
    vendor_costs = [_number((event.get("diagnostics") or {}).get("vendor_cost_usd")) for event in invoked]
    # An explicit zero is meaningful; missing per-call cost is not zero.
    known_spend = sum(Decimal(str(value)) for value in vendor_costs if value is not None)
    complete_spend = coverage_complete and all(value is not None for value in vendor_costs)
    complete_usage = coverage_complete and usage_unknown == 0
    inputs = _sum_complete(invoked, "input_tokens") if complete_usage else None
    outputs = _sum_complete(invoked, "output_tokens") if complete_usage else None
    cached = _sum_complete(invoked, "cached_input_tokens") if complete_usage else None
    # Cache is a subset of input, never added to the total again.
    return {
        "run": str(run), "verification": verdict, "verification_reason": reason,
        "run_completed_claim": result.get("completed") if result else None,
        "elapsed_seconds": _number((budget or {}).get("elapsed_seconds")),
        "reserved_attempts": attempts,
        "invoked_calls": len(invoked) if events is not None else None,
        "failed_invoked_calls": len(failed) if events is not None else None,
        "input_tokens": inputs, "output_tokens": outputs,
        "cached_input_tokens_subset": cached,
        "unknown_usage_attempts": usage_unknown,
        "attempt_event_coverage_complete": coverage_complete,
        "observed_vendor_spend_usd": float(known_spend) if any(value is not None for value in vendor_costs) else None,
        "vendor_spend_complete": complete_spend,
        "missing_vendor_cost_calls": sum(value is None for value in vendor_costs) if events is not None else None,
    }


def score(runs: list[Path], attestations: dict):
    rows = [score_run(run, attestations) for run in runs]
    verified = sum(row["verification"] == "verified" for row in rows)
    fully_costed = all(row["vendor_spend_complete"] for row in rows)
    spend = sum(Decimal(str(row["observed_vendor_spend_usd"])) for row in rows if row["observed_vendor_spend_usd"] is not None)
    return {
        "runs": rows,
        "summary": {
            "runs": len(rows), "verified_completions": verified,
            "unknown_verdicts": sum(row["verification"] == "unknown" for row in rows),
            "elapsed_seconds": _sum_complete(rows, "elapsed_seconds"),
            "reserved_attempts": _sum_complete(rows, "reserved_attempts"),
            "invoked_calls": _sum_complete(rows, "invoked_calls"),
            "failed_invoked_calls": _sum_complete(rows, "failed_invoked_calls"),
            "input_tokens": _sum_complete(rows, "input_tokens"),
            "output_tokens": _sum_complete(rows, "output_tokens"),
            "cached_input_tokens_subset": _sum_complete(rows, "cached_input_tokens_subset"),
            "observed_vendor_spend_usd": float(spend) if fully_costed else None,
            "vendor_spend_complete": fully_costed,
            "vendor_spend_known_partial_usd": float(spend) if spend else None,
            "vendor_spend_per_verified_completion_usd": float(spend / verified) if fully_costed and verified else None,
        },
        "method": "All selected runs, including failed attempts, enter aggregate totals. Unknown cells remain null. Vendor spend requires per-invocation vendor-reported cost; API-price counterfactual and subscription marginal spend are excluded. Independent completion requires external evidence bound to the saved source fingerprint.",
    }


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("runs", type=Path, nargs="+", help="saved run directories")
    parser.add_argument("--attestations", type=Path, help="external JSON object keyed by run directory name")
    args = parser.parse_args()
    attestations = _json(args.attestations) if args.attestations else {}
    if not isinstance(attestations, dict):
        parser.error("attestations must be a JSON object")
    if len({run.name for run in args.runs}) != len(args.runs):
        parser.error("run directory names must be unique for attestation matching")
    print(json.dumps(score(args.runs, attestations), indent=2))


if __name__ == "__main__":
    main()

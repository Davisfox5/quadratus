"""Offline, conservative scorecard for saved Quadratus run directories.

No provider calls, project execution, or acceptance inference occur here.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import math
from dataclasses import fields
from decimal import Decimal
from pathlib import Path

from quadratus.outcome import Fact, RunOutcome, TaskOutcome, completion_blockers, typed_completed


def _json(path: Path):
    return json.loads(path.read_text(encoding="utf-8")) if path.is_file() else None


def _jsonl(path: Path):
    if not path.is_file():
        return None
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]


def _number(value):
    return value if isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value) and value >= 0 else None


def _count(value):
    return value if type(value) is int and value >= 0 else None


def _sum_complete(rows, key):
    values = [_number(row.get(key)) for row in rows]
    return sum(values) if all(value is not None for value in values) else None


def _saved_outcomes(workflow):
    """Restore only complete, well-shaped serialized outcomes for the engine's rule.

    Dataclass defaults are useful during execution but must not turn an omitted
    saved field into apparent completion evidence.
    """
    run_record, task_records = workflow.get("run"), workflow.get("tasks")
    if not isinstance(run_record, dict) or not isinstance(task_records, list):
        return None
    run_keys = {field.name for field in fields(RunOutcome)}
    task_keys = {field.name for field in fields(TaskOutcome)}
    if not run_keys <= run_record.keys() or not task_records:
        return None
    try:
        if (type(run_record["done_accepted"]) is not bool or
                not isinstance(run_record["facts"], list) or
                not isinstance(run_record["readiness"], list)):
            return None
        def restore_facts(records):
            required = {field.name for field in fields(Fact)}
            if not isinstance(records, list) or any(
                    not isinstance(record, dict) or not required <= record.keys() or
                    type(record["terminal"]) is not bool or type(record["recovered"]) is not bool
                    for record in records):
                raise ValueError("malformed facts")
            return [Fact(**{key: record[key] for key in required}) for record in records]

        run = RunOutcome(facts=restore_facts(run_record["facts"]),
                         done_accepted=run_record["done_accepted"],
                         readiness=run_record["readiness"])
        tasks = []
        for record in task_records:
            if (not isinstance(record, dict) or not task_keys <= record.keys() or
                    not isinstance(record.get("unsatisfied"), list)):
                return None
            contract, dispatch = record["contract"], record["dispatch"]
            if (not isinstance(contract, dict) or not isinstance(dispatch, dict) or
                    not isinstance(record["edges"], dict) or
                    not isinstance(record["checks"], list) or
                    not isinstance(record["mismatches"], list) or
                    not isinstance(record["owner_changes"], list) or
                    not isinstance(record["stages"], list) or
                    not isinstance(record["continues"], str) or
                    not isinstance(record["open_at_close"], dict) or
                    not isinstance(record["closed_as"], str) or
                    not isinstance(record["task_id"], str) or
                    not isinstance(record["intent"], str)):
                return None
            if record["closed_as"] not in ("closed", "turn_limited"):
                return None
            if not isinstance(record["source_after"], str) or not record["source_after"]:
                return None
            required = contract.get("required")
            if (not isinstance(required, dict) or
                    any(type(required.get(key)) is not bool for key in
                        ("checks", "design_review", "security_verification", "settlement")) or
                    required.get("design_evidence") not in ("harness", "self", "disabled", "none")):
                return None
            data = {key: record[key] for key in task_keys}
            data["facts"] = restore_facts(record["facts"])
            task = TaskOutcome(**data)
            if record["unsatisfied"] != task.unsatisfied():
                return None
            tasks.append(task)
        if len({task.task_id for task in tasks}) != len(tasks):
            return None
        return run, tasks
    except (TypeError, ValueError, KeyError, AttributeError):
        return None


def _final_check_problem(checks, *, required, source, run_level=False):
    if not isinstance(checks, list) or (required and not checks):
        return "check receipts unavailable"
    if not checks:
        return None
    final = checks[-1]
    if not isinstance(final, dict) or final.get("passed") is not True:
        return "final check failed"
    # Task attempts carry their own source identity. A task can legitimately
    # precede later edits, so its check is compared to its task-close state.
    if not run_level and "source" in final and (
            not isinstance(final["source"], str) or not final["source"] or
            final["source"] != source):
        return "check source mismatch"
    receipts = final.get("receipts", [])
    if not isinstance(receipts, list):
        return "check receipts malformed"
    expected = source if run_level else final.get("source", source)
    if any(isinstance(receipt, dict) and "source_hash" in receipt and (
            not isinstance(receipt["source_hash"], str) or not receipt["source_hash"] or
            receipt["source_hash"] != expected) for receipt in receipts):
        return "check receipt source mismatch"
    # Run gate receipts carry `required`; task receipts omit it. A task's
    # passed wrapper is authoritative for optional task gate failures.
    if run_level and any(not isinstance(receipt, dict) or
                         type(receipt.get("required")) is not bool for receipt in receipts):
        return "check receipt requirement unavailable"
    if all(isinstance(receipt, dict) and type(receipt.get("required")) is bool
           for receipt in receipts):
        if any(receipt["required"] and receipt.get("status") != "passed"
               for receipt in receipts):
            return "required receipt failed"
    elif any("required" in receipt for receipt in receipts if isinstance(receipt, dict)):
        return "check receipt requirement malformed"
    elif any(not isinstance(receipt, dict) or not isinstance(receipt.get("status"), str)
             for receipt in receipts):
        return "check receipts malformed"
    return None


def _workflow_problem(result):
    workflow = result.get("workflow")
    if not isinstance(workflow, dict):
        return "workflow record unavailable"
    if workflow.get("error"):
        return "workflow record error"
    parity = workflow.get("parity")
    if not isinstance(parity, dict) or not all(parity.get(key) is True for key in
                                                  ("agree", "complete", "typed_completed")):
        return "workflow parity incomplete or contradictory"
    if (not isinstance(parity.get("problems"), list) or
            not isinstance(parity.get("missing"), list) or
            parity["problems"] or parity["missing"] or parity.get("mismatches")):
        return "workflow parity reports debt"
    restored = _saved_outcomes(workflow)
    if restored is None:
        return "workflow outcome schema incomplete"
    run, tasks = restored
    if not run.done_accepted or not typed_completed(run, tasks, []):
        return "workflow acceptance or active stop contradicts completion"
    if not isinstance(result.get("error"), str) or result["error"]:
        return "completed run carries error"
    findings = result.get("findings")
    if not isinstance(findings, list):
        return "findings record unavailable"
    audit_findings = {}
    task_ids = {task.task_id for task in tasks}
    finding_ids = set()
    for finding in findings:
        if not isinstance(finding, dict) or finding.get("status") != "resolved":
            return "unresolved finding"
        if (not isinstance(finding.get("id"), str) or not finding["id"].strip() or
                finding["id"] in finding_ids or
                not isinstance(finding.get("task"), str) or
                finding["task"] not in task_ids or
                ("resolved_by" in finding and (
                    not isinstance(finding["resolved_by"], str) or
                    finding["resolved_by"] not in task_ids))):
            return "finding identity malformed"
        finding_ids.add(finding["id"])
        audit_findings.setdefault(finding["task"], []).append(finding["status"])
    try:
        blockers = completion_blockers(tasks, audit_findings=audit_findings)
    except (TypeError, ValueError, KeyError, AttributeError):
        return "workflow outcome malformed"
    if blockers:
        return "task workflow debt"
    for task in tasks:
        problem = _final_check_problem(task.checks, required=task.contract["required"]["checks"],
                                       source=task.source_after)
        if problem:
            return "task " + problem
    requirements = result.get("requirements")
    if not isinstance(requirements, dict) or not isinstance(requirements.get("listed"), dict) or not isinstance(requirements.get("status"), dict):
        return "requirements record unavailable"
    if any(not str(requirements["status"].get(rid, "")).startswith(("covered", "met"))
           for rid in requirements["listed"]):
        return "unmet requirement"
    checks = result.get("checks")
    source = result.get("source_fingerprint")
    if not isinstance(source, str) or not source:
        return "run source unavailable"
    problem = _final_check_problem(checks, required=any(
        task.contract["required"]["checks"] for task in tasks), source=source,
        run_level=True)
    if problem:
        return "run " + problem
    return None


def _verification(run: Path, result: dict | None, attestations: dict):
    """A saved run cannot certify its own independent completion."""
    claim = attestations.get(run.name)
    if claim is None:
        return "unknown", "no independent attestation"
    if not isinstance(claim, dict):
        return "unknown", "malformed independent attestation"
    if claim.get("status") != "verified":
        return "unverified" if claim.get("status") == "unverified" else "unknown", "external verdict"
    saved_result = run / "result.json"
    if (not saved_result.is_file() or
            claim.get("result_sha256") != hashlib.sha256(saved_result.read_bytes()).hexdigest()):
        return "unknown", "saved result identity mismatch"
    if not result or result.get("completed") is not True:
        return "unknown", "run did not record completion"
    problem = _workflow_problem(result)
    if problem:
        return "unknown", problem
    if claim.get("source_fingerprint") != result.get("source_fingerprint") or not claim.get("source_fingerprint"):
        return "unknown", "source identity mismatch"
    evidence = claim.get("evidence")
    if not isinstance(evidence, str) or not evidence:
        return "unknown", "no review evidence"
    evidence_path = Path(evidence)
    if not evidence_path.is_absolute():
        evidence_path = run / evidence_path
    if not evidence_path.is_file() or evidence_path.stat().st_size == 0:
        return "unknown", "review evidence missing"
    if not claim.get("reviewer"):
        return "unknown", "reviewer missing"
    return "verified", "independent attestation with matching source identity"


def score_run(run: Path, attestations: dict):
    result = _json(run / "result.json")
    persisted_budget = _json(run / "budget.json")
    final_budget = result.get("budget") if isinstance(result, dict) else None
    # A saved final result is authoritative. Its explicit null cannot be
    # replaced by an earlier budget checkpoint.
    budget = final_budget if isinstance(result, dict) else persisted_budget
    budget = budget if isinstance(budget, dict) else None
    final_elapsed = _number((budget or {}).get("elapsed_seconds"))
    persisted_elapsed = _number((persisted_budget or {}).get("elapsed_seconds"))
    attempts = _count((budget or {}).get("reserved_attempts"))
    usage_unknown = _count((budget or {}).get("unknown_usage_attempts"))
    persisted_attempts = _count((persisted_budget or {}).get("reserved_attempts"))
    persisted_unknown = _count((persisted_budget or {}).get("unknown_usage_attempts"))
    budget_consistent = budget is not None
    if isinstance(result, dict) and isinstance(persisted_budget, dict):
        if persisted_elapsed is not None and (final_elapsed is None or final_elapsed < persisted_elapsed):
            final_elapsed = None
            budget_consistent = False
        if persisted_attempts is not None and (attempts is None or attempts < persisted_attempts):
            attempts = None
            budget_consistent = False
        if persisted_unknown is not None and (usage_unknown is None or usage_unknown < persisted_unknown):
            usage_unknown = None
            budget_consistent = False
    events = _jsonl(run / "invocations.jsonl")
    verdict, reason = _verification(run, result, attestations)
    invoked = [event for event in events or [] if event.get("invoked") is True]
    failed = [event for event in invoked if event.get("outcome") != "ok"]
    coverage_complete = (events is not None and budget_consistent and attempts is not None
                         and usage_unknown is not None and attempts == len(invoked))
    vendor_costs = [_number((event.get("diagnostics") or {}).get("vendor_cost_usd")) for event in invoked]
    # An explicit zero is meaningful; missing per-call cost is not zero.
    known_spend = sum(Decimal(str(value)) for value in vendor_costs if value is not None)
    complete_spend = coverage_complete and all(value is not None for value in vendor_costs)
    complete_usage = coverage_complete and usage_unknown == 0
    inputs = _sum_complete(invoked, "input_tokens") if complete_usage else None
    outputs = _sum_complete(invoked, "output_tokens") if complete_usage else None
    cached_values = []
    for event in invoked:
        top = _number(event.get("cached_input_tokens"))
        detail = _number((event.get("diagnostics") or {}).get("cached_input_tokens"))
        cache = top if top is not None else detail
        count = _number(event.get("input_tokens"))
        cached_values.append(cache if cache is not None and count is not None
                             and cache <= count and (top is None or detail is None or top == detail)
                             else None)
    cached = sum(cached_values) if complete_usage and all(value is not None for value in cached_values) else None
    # Cache is a subset of input, never added to the total again.
    return {
        "run": str(run), "verification": verdict, "verification_reason": reason,
        "run_completed_claim": result.get("completed") if result else None,
        "budget_elapsed_seconds": final_elapsed,
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
    totals = {key: _sum_complete(rows, key) for key in (
        "budget_elapsed_seconds", "reserved_attempts", "invoked_calls", "failed_invoked_calls",
        "input_tokens", "output_tokens", "cached_input_tokens_subset")}
    ratios = {f"{key}_per_verified_completion": value / verified if value is not None and verified else None
              for key, value in totals.items()}
    return {
        "runs": rows,
        "summary": {
            "runs": len(rows), "verified_completions": verified,
            "unknown_verdicts": sum(row["verification"] == "unknown" for row in rows),
            **totals, **ratios,
            "observed_vendor_spend_usd": float(spend) if fully_costed else None,
            "vendor_spend_complete": fully_costed,
            "vendor_spend_known_partial_usd": float(spend) if any(
                row["observed_vendor_spend_usd"] is not None for row in rows) else None,
            "vendor_spend_per_verified_completion_usd": float(spend / verified) if fully_costed and verified else None,
        },
        "method": "All selected runs, including failed attempts, enter aggregate totals. Budget elapsed is the run budget clock, beginning after repository scan; it includes session readiness and plan-gate wait, and its final snapshot includes trace/report I/O. It is not an external supervisor clock. Unknown cells remain null. Vendor spend requires per-invocation vendor-reported cost; API-price counterfactual and subscription marginal spend are excluded. Independent completion requires external evidence bound to the saved source fingerprint, exact result digest, and a consistent saved workflow record.",
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

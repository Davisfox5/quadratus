"""Compare finished runs from their records: what each cost and what it showed.

Reads ``.quadratus/runs/<id>`` folders (``result.json``, ``invocations.jsonl``,
``activity.jsonl``) of any mode: a project run, an explicit task list, or a
baseline run of one model alone (``quadratus.baseline``). It imports nothing
from ``quadratus`` and calls no model, like ``tools/truth_check.py``, whose own
``judge`` gives the truth-check verdict here; its rules are not repeated.

For each run it prints the mode, the models that were called, calls, reported
tokens, wall seconds, the stop reason, whether the run changed any source, the
final gate with executed, failed and skipped counts, the truth-check verdict,
requirements audited met where
the mode has an audit, and, where a grade is given, graders passed of total
and reported tokens per grader-passed requirement. Then the differences
between the first run and each other one.

Anything a record does not show prints as ``unknown``, never as zero. A token
total that leaves out calls which reported no usage says so.

Usage: ``python -I tools/compare_runs.py RUN_DIR RUN_DIR [...]
[--grade FILE ...] [--json]``. One ``--grade`` per run, in the order of the
runs, ``-`` for a run with none. A grade file is a JSON object with whole
numbers ``passed`` and ``total`` (what ``tools/jev_stage_b/pairs.py`` writes as
a cell's ``grade``); an object that holds one under ``grade`` is read too.
"""

from __future__ import annotations

import argparse
import importlib.util
import json
import sys
from datetime import datetime
from pathlib import Path

UNKNOWN = "unknown"
#: Nothing here is read past this size; a larger file reads as unknown.
MAX_BYTES = 64 * 1024 * 1024
#: The numeric fields compared between runs, in print order, with their labels.
NUMBERS = (("calls", "calls"), ("reported_tokens", "reported tokens"), ("wall_seconds", "wall seconds"),
           ("model_seconds", "model seconds"), ("gate_executed", "gate cases executed"),
           ("gate_failed", "gate cases failed"), ("gate_skipped", "gate cases skipped"),
           ("requirements_met", "requirements audited met"), ("grader_passed", "graders passed"),
           ("tokens_per_passed_requirement", "tokens per grader-passed requirement"))
WORDS = (("mode", "mode"), ("models", "models"), ("claim", "claim"), ("stop_reason", "stop reason"),
         ("source_changed", "source changed"), ("gate", "final gate"), ("truth_check", "truth check"))


def _truth_check():
    """``tools/truth_check.py`` loaded by path, so this file runs under
    ``python -I`` (no script directory on the path) as that one does."""
    name = "quadratus_tools_truth_check"
    if name not in sys.modules:
        spec = importlib.util.spec_from_file_location(name, Path(__file__).with_name("truth_check.py"))
        module = importlib.util.module_from_spec(spec)
        sys.modules[name] = module
        spec.loader.exec_module(module)
    return sys.modules[name]


def _whole(value):
    """``value`` if it is a whole number of at least zero, else None."""
    return value if type(value) is int and value >= 0 else None


def _number(value):
    return value if type(value) in (int, float) and value == value and value >= 0 else None


def _read(path: Path):
    try:
        if not path.is_file() or path.stat().st_size > MAX_BYTES:
            return None
        return path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError):
        return None


def _json(path: Path):
    text = _read(path)
    if text is None:
        return None
    try:
        data = json.loads(text)
    except (ValueError, RecursionError):
        return None
    return data if isinstance(data, dict) else None


def _rows(path: Path):
    """The object lines of a JSONL file, or None when it cannot be read."""
    text = _read(path)
    if text is None:
        return None
    rows = []
    for line in text.splitlines():
        try:
            row = json.loads(line)
        except (ValueError, RecursionError):
            continue
        if isinstance(row, dict):
            rows.append(row)
    return rows


def _first_line(text, limit=200) -> str:
    lines = str(text or "").strip().splitlines()
    return lines[0][:limit] if lines else ""


def _mode(record) -> str:
    mode = record.get("mode")
    if isinstance(mode, str) and mode:
        return mode
    return "explicit task list" if isinstance(record.get("explicit_tasks"), dict) else "project run"


def _claim(record) -> str:
    """What the run's own record says of the goal, in the record's terms."""
    if record.get("mode") == "baseline":
        return "not judged (baseline)"
    explicit = record.get("explicit_tasks")
    if record.get("completed") is True:
        if isinstance(explicit, dict) and explicit.get("goal_judged") is not True:
            return "listed tasks complete; goal not judged"
        return "goal reported complete"
    return "incomplete"


def _stop(record) -> str:
    error = _first_line(record.get("error"))
    if error:
        return error
    budget = record.get("budget")
    stop = budget.get("stop_reason") if isinstance(budget, dict) else None
    return f"budget: {_first_line(stop)}" if stop else "none recorded"


def _source_changed(record) -> str:
    """Whether the run left any source change, as its record says: "yes",
    "no", or unknown when the record does not say. A final gate that passed
    on a run that changed nothing describes the starting source, so the two
    are printed side by side."""
    changed = record.get("source_changed")
    return {True: "yes", False: "no"}.get(changed, UNKNOWN) if isinstance(changed, bool) else UNKNOWN


def _usage(run_dir: Path, record):
    """Models, calls, reported tokens and model seconds from the invocation
    record. Tokens are summed over the calls that reported both counts; the
    calls that reported neither are counted, never guessed."""
    out = dict(models=UNKNOWN, calls=UNKNOWN, reported_tokens=UNKNOWN, unknown_usage_calls=UNKNOWN,
               model_seconds=UNKNOWN)
    rows = _rows(run_dir / "invocations.jsonl")
    if rows is None:
        budget = record.get("budget") if isinstance(record.get("budget"), dict) else {}
        if _whole(budget.get("reserved_attempts")) is not None:
            out["calls"] = budget["reserved_attempts"]
        if _whole(budget.get("reported_tokens")) is not None and _whole(budget.get("unknown_usage_attempts")) == 0:
            out["reported_tokens"] = budget["reported_tokens"]
        return out
    invoked = [r for r in rows if r.get("invoked") is True]
    models = sorted({str(m) for r in invoked
                     for m in [r.get("canonical_model") or r.get("resolved_model") or r.get("requested_model")] if m})
    known = [r["input_tokens"] + r["output_tokens"] for r in invoked
             if _whole(r.get("input_tokens")) is not None and _whole(r.get("output_tokens")) is not None]
    seconds = [_number(r.get("seconds")) for r in invoked]
    out.update(models=", ".join(models) if models else UNKNOWN, calls=len(invoked),
               unknown_usage_calls=len(invoked) - len(known))
    if known:
        out["reported_tokens"] = sum(known)
    if invoked and all(s is not None for s in seconds):
        out["model_seconds"] = round(sum(seconds), 1)
    return out


def _wall(run_dir: Path, record):
    """Wall seconds as the run recorded them: the budget's clock, a baseline's
    own figure, else the span of its activity record. Never a file time."""
    budget = record.get("budget") if isinstance(record.get("budget"), dict) else {}
    if _number(budget.get("elapsed_seconds")) is not None:
        return round(budget["elapsed_seconds"], 1)
    info = record.get("baseline") if isinstance(record.get("baseline"), dict) else {}
    if _number(info.get("seconds")) is not None:
        return round(info["seconds"], 1)
    stamps = []
    for row in _rows(run_dir / "activity.jsonl") or []:
        try:
            stamps.append(datetime.fromisoformat(str(row.get("at")).replace("Z", "+00:00")).timestamp())
        except (ValueError, TypeError, OverflowError):
            continue
    return round(max(stamps) - min(stamps), 1) if len(stamps) > 1 else UNKNOWN


def _gate(record):
    """The last recorded check: its status, and the cases its commands'
    structured reports count. A command with no parsed report gives its
    executed count when the runner printed one and nothing else."""
    out = dict(gate=UNKNOWN, gate_executed=UNKNOWN, gate_failed=UNKNOWN, gate_skipped=UNKNOWN)
    checks = record.get("checks")
    if not isinstance(checks, list):
        return out
    if not checks:
        out["gate"] = "no check ran"
        return out
    last = checks[-1] if isinstance(checks[-1], dict) else {}
    if last.get("passed") is True:
        out["gate"] = "passed"
    elif last.get("passed") is False:
        out["gate"] = "failed"
    if out["gate"] != UNKNOWN and last.get("observed_after_stop") is True:
        out["gate"] += " (run once after the stop)"
    source, final = last.get("source"), record.get("source_fingerprint")
    if out["gate"] != UNKNOWN and source and final and source != final:
        # The source changed after this check, so it does not describe what was left.
        out["gate"] += " (on an earlier source, not the final one)"
    listed = last.get("receipts") if isinstance(last.get("receipts"), list) else []
    receipts = [r for r in listed if isinstance(r, dict)] or [last]
    ran = [r for r in receipts if r.get("status") != "skipped"]
    totals = dict(executed=0, failed=0, skipped=0)
    for row in ran:
        report = row.get("report") if isinstance(row.get("report"), dict) else {}
        counts = report.get("counts") if report.get("state") == "parsed" else None
        if isinstance(counts, dict) and all(_whole(counts.get(k)) is not None
                                            for k in ("passed", "failed", "skipped", "errors")):
            found = dict(executed=counts["passed"] + counts["failed"],
                         failed=counts["failed"] + counts["errors"], skipped=counts["skipped"])
        else:
            found = dict(executed=_whole(row.get("tests")), failed=None, skipped=None)
        for key, value in found.items():
            totals[key] = None if value is None or totals[key] is None else totals[key] + value
    if ran:
        for key, value in totals.items():
            if value is not None:
                out[f"gate_{key}"] = value
    return out


def _audit(record):
    """Requirements the last audit that ran marked met, of those listed."""
    out = dict(requirements_met=UNKNOWN, requirements_listed=UNKNOWN, audit="unknown")
    if record.get("mode") == "baseline":
        out["audit"] = "not applicable: a baseline has no audit"
        return out
    requirements = record.get("requirements") if isinstance(record.get("requirements"), dict) else None
    if requirements is None:
        return out
    listed = requirements.get("listed") if isinstance(requirements.get("listed"), dict) else {}
    out["requirements_listed"] = len(listed)
    recorded = requirements.get("audits") if isinstance(requirements.get("audits"), list) else []
    audits = [a for a in recorded if isinstance(a, dict)]
    if not audits:
        out["audit"] = "no audit on record"
        return out
    final = audits[-1].get("verdicts")
    if not isinstance(final, dict):
        out["audit"] = "the last audit did not run"
        return out
    out["requirements_met"] = sum(1 for rid in listed
                                  if isinstance(final.get(rid), dict) and final[rid].get("met") is True)
    out["audit"] = "audited"
    return out


def read_grade(path):
    """``(passed, total)`` from a grade file, or ``(UNKNOWN, UNKNOWN)`` with
    why. Never a guess: counts that are not whole, or more passed than
    there are, are unknown."""
    if path in (None, "", "-"):
        return UNKNOWN, UNKNOWN, "no grade given"
    data = _json(Path(path))
    if data is None:
        return UNKNOWN, UNKNOWN, f"grade file {path} is not a readable JSON object"
    if isinstance(data.get("grade"), dict):
        data = data["grade"]
    passed, total = _whole(data.get("passed")), _whole(data.get("total"))
    if passed is None or total is None or passed > total:
        return UNKNOWN, UNKNOWN, f"grade file {path} carries no usable passed and total"
    return passed, total, ""


def summarize(run_dir, grade=None) -> dict:
    """One run as a flat record. Every field is a value or ``UNKNOWN``."""
    run_dir = Path(run_dir)
    record = _json(run_dir / "result.json")
    out = dict(run=str(run_dir), mode=UNKNOWN, models=UNKNOWN, calls=UNKNOWN, reported_tokens=UNKNOWN,
               unknown_usage_calls=UNKNOWN, wall_seconds=UNKNOWN, model_seconds=UNKNOWN, claim=UNKNOWN,
               stop_reason=UNKNOWN, source_changed=UNKNOWN, gate=UNKNOWN, gate_executed=UNKNOWN,
               gate_failed=UNKNOWN,
               gate_skipped=UNKNOWN, requirements_met=UNKNOWN, requirements_listed=UNKNOWN, audit=UNKNOWN)
    if record is None:
        out["problem"] = "no readable result.json"
    usage = _usage(run_dir, record or {})
    out.update(usage)
    if record is not None:
        out.update(mode=_mode(record), claim=_claim(record), stop_reason=_stop(record),
                   source_changed=_source_changed(record),
                   wall_seconds=_wall(run_dir, record), **_gate(record), **_audit(record))
        if usage["models"] == UNKNOWN and isinstance(record.get("model"), str) and record["model"]:
            out["models"] = record["model"]
    try:
        verdict = _truth_check().judge(run_dir)
        out["truth_check"] = verdict["verdict"]
        out["truth_check_reasons"] = [*verdict["not_met"], *verdict["unverified"]][:12]
    except Exception as exc:  # noqa: BLE001 -- a record it cannot read has no verdict
        out["truth_check"] = UNKNOWN
        out["truth_check_reasons"] = [f"the truth check could not read this record ({type(exc).__name__})"]
    passed, total, problem = read_grade(grade)
    out.update(grader_passed=passed, grader_total=total, grade_problem=problem,
               tokens_per_passed_requirement=UNKNOWN, tokens_per_passed_requirement_note="")
    tokens = out["reported_tokens"]
    if passed == UNKNOWN:
        out["tokens_per_passed_requirement_note"] = problem
    elif tokens == UNKNOWN:
        out["tokens_per_passed_requirement_note"] = "reported tokens are unknown"
    elif passed == 0:
        # No requirement passed, so there is nothing to divide by: unknown, not zero.
        out["tokens_per_passed_requirement_note"] = "no requirement passed the graders"
    else:
        out["tokens_per_passed_requirement"] = round(tokens / passed)
        if out["unknown_usage_calls"] not in (0, UNKNOWN):
            out["tokens_per_passed_requirement_note"] = (
                f"a lower bound: {out['unknown_usage_calls']} call(s) reported no usage")
    return out


def differences(first: dict, other: dict) -> dict:
    """``other`` against ``first``: for a number the difference and the ratio
    when both are known, for a word whether it is the same."""
    out = {}
    for key, _ in NUMBERS:
        a, b = first.get(key, UNKNOWN), other.get(key, UNKNOWN)
        entry = dict(first=a, other=b, difference=UNKNOWN, ratio=UNKNOWN)
        if UNKNOWN not in (a, b):
            entry["difference"] = round(b - a, 1) if isinstance(b - a, float) else b - a
            if a > 0:
                entry["ratio"] = round(b / a, 2)
        out[key] = entry
    for key, _ in WORDS:
        a, b = first.get(key, UNKNOWN), other.get(key, UNKNOWN)
        out[key] = dict(first=a, other=b, same=UNKNOWN if UNKNOWN in (a, b) else a == b)
    return out


def compare(run_dirs, grades=()) -> dict:
    grades = list(grades or ())
    runs = [summarize(run, grades[i] if i < len(grades) else None) for i, run in enumerate(run_dirs)]
    return dict(runs=runs, differences=[dict(run=other["run"], against=runs[0]["run"],
                                             fields=differences(runs[0], other)) for other in runs[1:]])


def _show(value) -> str:
    if value == UNKNOWN:
        return UNKNOWN
    return f"{value:,}" if type(value) in (int, float) else str(value)


def render(result: dict) -> str:
    lines = []
    for index, run in enumerate(result["runs"], 1):
        tokens = _show(run["reported_tokens"])
        if run["unknown_usage_calls"] not in (0, UNKNOWN):
            tokens += f" (not counting {run['unknown_usage_calls']} call(s) that reported no usage)"
        gate = (f"{run['gate']} (executed {_show(run['gate_executed'])}, failed {_show(run['gate_failed'])}, "
                f"skipped {_show(run['gate_skipped'])})")
        met = (f"{_show(run['requirements_met'])} of {_show(run['requirements_listed'])}"
               if run["audit"] == "audited" else run["audit"])
        grade = (f"{run['grader_passed']} of {run['grader_total']} passed" if run["grader_passed"] != UNKNOWN
                 else f"{UNKNOWN} ({run['grade_problem']})")
        per = _show(run["tokens_per_passed_requirement"])
        if run["tokens_per_passed_requirement_note"]:
            per += f" ({run['tokens_per_passed_requirement_note']})"
        reasons = "; ".join(run["truth_check_reasons"][:3])
        lines += [f"Run {index}: {run['run']}"]
        if run.get("problem"):
            lines += [f"  problem: {run['problem']}"]
        lines += [f"  mode: {run['mode']}", f"  models: {run['models']}", f"  calls: {_show(run['calls'])}",
                  f"  reported tokens: {tokens}", f"  wall seconds: {_show(run['wall_seconds'])}",
                  f"  model seconds: {_show(run['model_seconds'])}", f"  claim: {run['claim']}",
                  f"  stop reason: {run['stop_reason']}", f"  source changed: {run['source_changed']}",
                  f"  final gate: {gate}",
                  f"  truth check: {run['truth_check']}" + (f" ({reasons})" if reasons else ""),
                  f"  requirements audited met: {met}", f"  graders: {grade}",
                  f"  tokens per grader-passed requirement: {per}", ""]
    for index, entry in enumerate(result["differences"], 2):
        fields = entry["fields"]
        lines.append(f"Run {index} against run 1:")
        for key, label in WORDS:
            field = fields[key]
            same = {True: "same", False: "differs", UNKNOWN: UNKNOWN}[field["same"]]
            lines.append(f"  {label}: {same}" + ("" if field["same"] is True
                                                 else f" ({field['other']} against {field['first']})"))
        for key, label in NUMBERS:
            field = fields[key]
            if field["difference"] == UNKNOWN:
                lines.append(f"  {label}: {UNKNOWN} ({_show(field['other'])} against {_show(field['first'])})")
                continue
            sign = "+" if field["difference"] > 0 else ""
            ratio = f", {field['ratio']}x" if field["ratio"] != UNKNOWN else ""
            lines.append(f"  {label}: {sign}{_show(field['difference'])}{ratio} "
                         f"({_show(field['other'])} against {_show(field['first'])})")
        lines.append("")
    return "\n".join(lines).rstrip() + "\n"


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("run_dirs", nargs="+", metavar="RUN_DIR", help="two or more run folders")
    parser.add_argument("--grade", action="append", default=[], metavar="FILE",
                        help="a grade JSON for the run in the same position; - for none")
    parser.add_argument("--json", action="store_true")
    # Intermixed, so ``RUN --grade A RUN --grade B`` reads as it is written.
    args = parser.parse_intermixed_args(argv)
    if len(args.run_dirs) < 2:
        parser.error("give at least two run folders to compare")
    if len(args.grade) > len(args.run_dirs):
        parser.error("more grades than runs")
    result = compare(args.run_dirs, args.grade)
    print(json.dumps(result, indent=2) if args.json else render(result), end="" if not args.json else "\n")
    return 0


if __name__ == "__main__":
    sys.exit(main())

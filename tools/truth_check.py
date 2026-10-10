"""Independent truth check of one finished run's record.

Reads ``.quadratus/runs/<id>/result.json`` (and ``changes.diff``) and decides,
from the record alone, whether the run's success claim is backed by evidence.
It imports nothing from ``quadratus``: a bug in the engine's own accounting
must not also be a bug in the check of it. No model is called.

Three verdicts, and missing evidence is never success:

- ``verified``: completed, every listed requirement met by an audit, the final
  passing check ran test cases against the final source, nothing required was
  skipped, nothing reported NOT RUN still stands, no finding is open, and every
  UI change carries approved capture evidence bound by digest to the bytes
  the reviewer was handed.
- ``unverified``: the run says it succeeded but some of that is not shown.
- ``not-met``: the record itself says the run failed, stopped or left a
  requirement unmet.

Usage: ``python -I tools/truth_check.py RUN_DIR [--project PATH] [--json]``.
Exit status 0 verified, 1 unverified, 2 not met. Adopted from Codex's review
of the efficiency plan (2026-10-10): the check must look at the right tests,
the right source, skipped checks and UI capture evidence, not "a test passed".
"""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import sys
from pathlib import Path

VERIFIED, UNVERIFIED, NOT_MET = "verified", "unverified", "not-met"
EXIT = {VERIFIED: 0, UNVERIFIED: 1, NOT_MET: 2}
#: Changed files that are rendered UI; one of these without capture evidence
#: is an unshown UI claim.
UI_SUFFIXES = (".html", ".htm", ".css", ".scss", ".sass", ".less", ".jsx", ".tsx", ".vue", ".svelte")
VIEWS = ("desktop", "mobile")
_DIFF_FILE = re.compile(r"^\+\+\+ b/(.+)$", re.MULTILINE)
_PATH = re.compile(r"[\w./-]+\.\w+")


def _last_check(record):
    """The latest check the workflow recorded, with the source it ran on."""
    latest = None
    for task in (record.get("workflow") or {}).get("tasks") or []:
        for check in task.get("checks") or []:
            latest = check
    return latest


def _reported_passes(report) -> int:
    counts = (report or {}).get("counts") if isinstance(report, dict) else None
    passed = (counts or {}).get("passed")
    return passed if isinstance(passed, int) else 0


def _ran_tests(check) -> bool:
    """A test count, a structured report's passes or executed case names."""
    receipts = check.get("receipts") or []
    if any((isinstance(r.get("tests"), int) and r["tests"] > 0) or _reported_passes(r.get("report"))
           for r in receipts):
        return True
    return _reported_passes(check.get("report")) > 0 or bool((check.get("cases") or {}).get("executed"))


def _ui_files(diff: str):
    return sorted({p for p in _DIFF_FILE.findall(diff or "") if p.lower().endswith(UI_SUFFIXES)})


def _approved_digests(record, task: str):
    """The bytes the approving design reviewer was handed, as the record
    bound them (``workflow.tasks[].delivery.files``), or None."""
    for entry in (record.get("workflow") or {}).get("tasks") or []:
        if entry.get("task_id") == task:
            files = (entry.get("delivery") or {}).get("files")
            return files if isinstance(files, dict) else None
    return None


def _screens_problem(record, project, task: str):
    """A problem with the renders an approval covers, or None.

    The approval is bound to the digests recorded when the renders were
    delivered to the reviewer; a render with no recorded digest is not shown
    to be what was approved, and with ``project`` every bound file must still
    hold those bytes (a PNG signature alone proves nothing)."""
    files = _approved_digests(record, task)
    folder = f".quadratus/design-evidence/{task}"
    names = [f"{folder}/{view}/page.png" for view in VIEWS]
    if not files or not all(isinstance(files.get(n), str) for n in names):
        return "no approved digest of the renders on record"
    if project is None:
        return None
    root = Path(project)
    for name, expected in sorted(files.items()):
        if not name.startswith(folder + "/"):
            continue
        path = root / name
        try:
            if path.is_symlink():
                return f"{name} is a symlink, not the approved render"
            actual = hashlib.sha256(path.read_bytes()).hexdigest()
        except OSError:
            return f"{name} is missing"
        if actual != expected:
            return f"{name} is not the approved bytes"
    return None


def _executed_keys(check):
    """Case names a passing check printed as executed, its own and its passed
    receipts'; names only, never a command line."""
    keys = set((check.get("cases") or {}).get("executed") or {})
    for receipt in check.get("receipts") or []:
        if receipt.get("status") == "passed":
            keys |= set((receipt.get("cases") or {}).get("executed") or {})
    return keys


def _not_run_cleared(entry, checks) -> bool:
    """Cleared only by a later passing check that names the execution: every
    reported file as the file of an executed case, or every skipped case as
    executed. A command line that mentions a path, or the engine's own
    discharge mark, is not execution (Codex review of 47bfbfd)."""
    if entry.get("kind") == "skipped":
        wanted = list(entry.get("cases") or [])
        files = []
    else:
        wanted = []
        files = [p[2:] if p.startswith("./") else p for p in _PATH.findall(entry.get("item") or "")]
    if not wanted and not files:
        return False
    start = entry.get("after_check") if isinstance(entry.get("after_check"), int) else 0
    for check in checks[start:]:
        if not check.get("passed"):
            continue
        keys = _executed_keys(check)
        ran_files = {k.split("::", 1)[0] for k in keys if "::" in k}
        if all(c in keys for c in wanted) and all(f in ran_files for f in files):
            return True
    return False


def judge(run_dir, project=None) -> dict:
    run_dir = Path(run_dir)
    failed, unshown = [], []
    try:
        record = json.loads((run_dir / "result.json").read_text(encoding="utf-8"))
        if not isinstance(record, dict):
            raise ValueError("not an object")
    except (OSError, ValueError) as exc:
        return dict(verdict=UNVERIFIED, run=str(run_dir), not_met=[], unverified=[f"no readable result.json ({exc})"])
    try:
        diff = (run_dir / "changes.diff").read_text(encoding="utf-8")
    except OSError:
        diff = None

    # What the record itself says failed.
    if not record.get("completed"):
        failed.append("the run did not complete")
    if record.get("error"):
        failed.append(f"the run stopped with an error: {str(record['error'])[:200]}")
    if record.get("in_flight"):
        failed.append("work was still in flight when the run stopped")
    if record.get("failed_tasks"):
        failed.append(f"failed tasks: {', '.join(map(str, record['failed_tasks']))[:200]}")
    checks = record.get("checks") or []
    if checks and not checks[-1].get("passed"):
        failed.append(f"the last check failed: {str(checks[-1].get('command'))[:160]}")
    requirements = record.get("requirements") or {}
    listed = requirements.get("listed") or {}
    status = requirements.get("status") or {}
    for rid in sorted(listed):
        state = str(status.get(rid, ""))
        if state.upper().startswith("NOT MET"):
            failed.append(f"{rid} is not met: {state[:160]}")

    # What a success claim would need and the record does not show.
    if record.get("explicit_tasks") is not None:
        unshown.append("explicit task list: the listed tasks ran, the goal was never judged")
    if record.get("turn_limited_tasks"):
        unshown.append(f"turn-limited tasks: {', '.join(map(str, record['turn_limited_tasks']))[:200]}")
    if not checks:
        unshown.append("no check ran")
    last = _last_check(record)
    if checks and last is None:
        unshown.append("the checks carry no record of the source they ran on")
    elif last is not None:
        if not last.get("passed"):
            unshown.append("the latest recorded check did not pass")
        if not last.get("source") or last.get("source") != record.get("source_fingerprint"):
            unshown.append("the final source was not the source the last check ran on")
        if not _ran_tests(last):
            unshown.append("the last check shows no test case running")
        for receipt in last.get("receipts") or []:
            if receipt.get("required", True) and receipt.get("status") != "passed":
                unshown.append(f"required check {receipt.get('id')} {receipt.get('status')}: "
                               f"{str(receipt.get('reason'))[:120]}")
            elif receipt.get("required", True) and receipt.get("tests") == 0:
                unshown.append(f"required check {receipt.get('id')} ran zero tests")
    for run in record.get("original_test_runs") or []:
        if run.get("passed") is not True:
            unshown.append(f"task {run.get('task')}: the run-start tests did not pass "
                           f"({str(run.get('problem') or run.get('passed'))[:120]})")
    if not listed:
        unshown.append("no requirements were recorded, so none was audited")
    audits = [a for a in requirements.get("audits") or [] if isinstance(a.get("verdicts"), dict)]
    final = audits[-1]["verdicts"] if audits else {}
    for rid in sorted(listed):
        verdict = final.get(rid) or {}
        if not str(status.get(rid, "")).lower().startswith("met"):
            if not str(status.get(rid, "")).upper().startswith("NOT MET"):
                unshown.append(f"{rid} has no met status ({str(status.get(rid))[:80]})")
        elif verdict.get("met") is not True:
            unshown.append(f"{rid} has no independent audit verdict of met")
    for entry in record.get("unexecuted_acceptance") or []:
        if not _not_run_cleared(entry, checks):
            unshown.append(f"task {entry.get('task')} reported NOT RUN and no later check shows it ran: "
                           f"{str(entry.get('item'))[:120]}")
    for finding in record.get("findings") or []:
        if finding.get("status") == "open":
            unshown.append(f"open finding {finding.get('id')}: {str(finding.get('message'))[:120]}")
    designs = record.get("design_checks") or []
    for design in designs:
        task = design.get("task")
        if design.get("verified") is not True:
            unshown.append(f"task {task}: design evidence not verified ({str(design.get('problem'))[:120]})")
        elif not (design.get("final_review") or {}).get("approved"):
            unshown.append(f"task {task}: no approving independent design review")
        else:
            problem = _screens_problem(record, project, str(task))
            if problem:
                unshown.append(f"task {task}: {problem}")
    if diff is None:
        unshown.append("no changes.diff, so UI changes cannot be ruled out")
    elif _ui_files(diff) and not designs:
        unshown.append(f"UI files changed with no capture evidence: {', '.join(_ui_files(diff))[:200]}")

    verdict = NOT_MET if failed else UNVERIFIED if unshown else VERIFIED
    return dict(verdict=verdict, run=str(run_dir), not_met=failed, unverified=unshown)


def render(result: dict) -> str:
    lines = [f"{result['verdict'].upper()}: {result['run']}"]
    lines += [f"  not met: {r}" for r in result["not_met"]]
    lines += [f"  unverified: {r}" for r in result["unverified"]]
    return "\n".join(lines)


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("run_dir")
    parser.add_argument("--project", help="project folder, to confirm the capture files exist")
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args(argv)
    result = judge(args.run_dir, args.project)
    print(json.dumps(result, indent=2) if args.json else render(result))
    return EXIT[result["verdict"]]


if __name__ == "__main__":
    sys.exit(main())

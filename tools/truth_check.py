"""Independent truth check of one finished run's record.

Reads ``.quadratus/runs/<id>/result.json`` (and ``changes.diff``) and decides,
from the record alone, whether the run's success claim is backed by evidence.
It imports nothing from ``quadratus``: a bug in the engine's own accounting
must not also be a bug in the check of it. No model is called.

Three verdicts, and missing evidence is never success:

- ``verified``: completed, every listed requirement met by an audit, the final
  passing check ran test cases against the final source, nothing required was
  skipped, nothing reported NOT RUN still stands, no finding is open, no
  authorized test repair is left unreviewed, and every
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


_CASE_ID = re.compile(r"^[\w./-]+\.\w+::(?:[A-Za-z_]\w*::)*test\w*(?:\[[^\]]*\])?$")
_SCENARIO_ID = re.compile(r"^P[0-9]+$")
#: Read whole, as the engine reads them: a bracketed parameter kept entire
#: and an id that must end at a boundary, so a prefix never stands in.
_ID_END = r"(?=$|[\s,;)]|\.(?:\s|$))"
_EXPLICIT_ID = re.compile(r"([\w./-]+\.[A-Za-z]\w*::(?:[A-Za-z_]\w*::)*test\w*(?:\[[^\]]*\])?)" + _ID_END)
_NAMED_CASE = re.compile(r"(?<![\w/.:-])((?:[A-Za-z_]\w*(?:\.|::))?test\w*(?:\[[^\]]*\])?)(?![\w/\[:-])"
                         r"(?!\.(?!\s|$))")
_SCENARIO_REF = re.compile(r"(?<![\w./:-])(P[0-9]+)(?![\w./:-])")
_SOURCE_FILE = re.compile(r"\.(?:py|pyi|js|mjs|cjs|ts|tsx|jsx|rb|go|rs|java|kt|cs|php|sh|feature)$")


def _claim(item: str) -> str:
    """What a NOT RUN item claims, read from its whole text: ``identity``
    (a scenario, an explicit id, a class or bare case name, or anything
    shaped like a case id the reader cannot take whole), ``paths`` (only
    path operands) or ``none``. The claim, never a stored kind, decides
    which route may clear it."""
    if _SCENARIO_REF.search(item) or _EXPLICIT_ID.search(item):
        return "identity"
    rest = _EXPLICIT_ID.sub(" ", item)
    if _NAMED_CASE.search(rest) or "::" in rest or "[" in rest or "]" in rest:
        return "identity"
    if any(re.search(r"\btest\w*", tok) for tok in rest.split() if "/" not in tok and not _SOURCE_FILE.search(tok)):
        return "identity"
    return "paths" if _PATH.findall(item) else "none"


def _identity_ok(entry, check, record) -> bool:
    """The check-level identity every route demands: a passing check on
    the final source, under the entry's own gate and environment where the
    entry records them (a record without them is held to the source
    alone). The per-command facts are read per row (``_row_ok``)."""
    if not (check.get("passed") and check.get("source") and check.get("source") == record.get("source_fingerprint")):
        return False
    for key in ("gate", "environment"):
        if key in entry and (not isinstance(entry.get(key), str) or check.get(key) != entry[key]):
            return False
    return True


def _paths_cleared(entry, checks, record) -> bool:
    """A path-only item: cleared by a later check of the right identity
    whose command names every path as a whole operand and whose own
    passed cases include a case of each file. Never an identity claim."""
    if _claim(str(entry.get("item") or "")) != "paths":
        return False
    import shlex
    files = [p[2:] if p.startswith("./") else p for p in _PATH.findall(str(entry.get("item") or ""))]
    start = entry.get("after_check") if isinstance(entry.get("after_check"), int) else None
    if start is None or not files:
        return False
    for check in checks[start:]:
        if not _identity_ok(entry, check, record):
            continue
        for row in _rows(check):
            if not _row_ok(row):
                continue
            try:
                operands = [a[2:] if a.startswith("./") else a for a in shlex.split(row["command"])]
            except ValueError:
                continue
            passed_files = {c.split("::", 1)[0] for c in _row_passed_cases(row) if "::" in c}
            if all(f in operands and f in passed_files for f in files):
                return True
    return False


def _filtered(command: str) -> bool:
    """Whether a command line selects cases: ``-k``, a pytest ``-m`` marker
    (not the interpreter's ``-m module``), ``--deselect``, ``--lf`` or
    ``--ff``. Unparsable is filtered (fail closed)."""
    import shlex
    try:
        argv = shlex.split(command)
    except ValueError:
        return True
    if not argv:
        return True
    rest = argv[1:]
    if re.fullmatch(r"python(\d+(\.\d+)*)?", Path(argv[0]).name) and rest[:1] == ["-m"]:
        rest = rest[2:]
    return any(a in ("-k", "-m", "--deselect", "--lf", "--ff") or a.startswith(("-k", "--deselect="))
               for a in rest)


def _rows(check):
    """A check's command records in one shape, read from the record alone:
    with ``receipts`` (a gate suite: the aggregate command is the literal
    ``gate suite`` and the aggregate report is None), one row per receipt
    with its own status, required flag, command, cwd, selection, report
    and count; without them (a single-command gate), one row from the
    check's own fields. Nothing is fabricated across the two shapes."""
    receipts = check.get("receipts") or []
    if receipts:
        rows = []
        for r in receipts:
            if not isinstance(r, dict):
                continue
            selection = str(r.get("selection") or "")
            if r.get("id") in ("check", "original-tests:check") and check.get("selection"):
                selection = "; ".join(x for x in (selection, str(check.get("selection"))) if x)
            rows.append(dict(id=r.get("id"), passed=r.get("status") == "passed", required=r.get("required", True),
                             command=str(r.get("command") or ""), cwd=str(r.get("cwd") or check.get("command_cwd") or "."),
                             selection=selection, report=r.get("report"), tests=r.get("tests")))
        return rows
    return [dict(id="check", passed=bool(check.get("passed")), required=True,
                 command=str(check.get("command") or ""), cwd=str(check.get("command_cwd") or "."),
                 selection=str(check.get("selection") or ""), report=check.get("report"), tests=None)]


def _row_ok(row) -> bool:
    """One command record that shows a whole successful run: passed,
    required, no selection, no argv filter, from the project root, a parsed
    report whose counts are known with zero skipped, zero failed, zero
    errors and at least one passed. Unknown is a refusal (fail closed)."""
    if not (row["passed"] and row["required"]) or row["selection"] or _filtered(row["command"]):
        return False
    if row["cwd"] not in ("", "."):
        return False
    report = row["report"]
    counts = report.get("counts") if isinstance(report, dict) else None
    if not isinstance(counts, dict):
        return False
    for key in ("skipped", "failed", "errors", "passed"):
        if not isinstance(counts.get(key), int) or isinstance(counts.get(key), bool):
            return False
    if counts["skipped"] or counts["failed"] or counts["errors"] or counts["passed"] <= 0:
        return False
    return bool(report.get("passed_cases"))


def _row_passed_cases(row):
    """The case ids a row's own report shows passed, each with and without
    its parametrization."""
    out = set()
    report = row["report"]
    for name in (report.get("passed_cases") or ()) if isinstance(report, dict) else ():
        name = str(name)
        out.add(name)
        out.add(name.split("[", 1)[0])
    return out


def _bound_not_run_cleared(entry, checks, record) -> bool:
    """A NOT RUN item the engine bound to scenario or case identities
    (``binding``, ``cases``), validated here from the saved record alone and
    never from an engine mark: the binding's provenance (a declared scenario
    of the same task whose acceptance row binds exactly these cases, or an
    item that itself names each bound case), then a later check on the final
    source, under the entry's own gate and environment, required and passed
    from the project root with no filter and no skip, whose own report shows
    every bound case passed. Anything missing, malformed, mismatched or on
    another identity is not cleared (Codex's consumer inventory, 2026-10-29)."""
    binding = entry.get("binding")
    cases = entry.get("cases")
    if not isinstance(binding, dict) or not isinstance(cases, list) or not cases:
        return False
    if not all(isinstance(c, str) and _CASE_ID.match(c) for c in cases):
        return False
    task, item = entry.get("task"), str(entry.get("item") or "")
    kind = binding.get("kind")
    if kind not in ("scenario", "case", "mixed"):
        return False
    named = re.findall(r"(?<![\w./:-])(P[0-9]+)(?![\w./:-])", item)
    declared = [x.strip() for x in str(binding.get("scenario") or "").split(",") if x.strip()]
    if kind == "case" and (named or declared):
        return False
    if kind != "case" and (not named or sorted(set(named)) != sorted(set(declared))):
        return False
    from_scenarios = set()
    for scenario in dict.fromkeys(named):
        if not _SCENARIO_ID.match(scenario):
            return False
        rows = [o for o in record.get("evidence_obligations") or []
                if isinstance(o, dict) and o.get("task") == task and o.get("kind") == "acceptance"
                and o.get("proof_id") == scenario and not o.get("superseded_by")]
        if not rows or not rows[-1].get("cases"):
            return False
        from_scenarios |= {str(c) for c in rows[-1]["cases"]}
    if not from_scenarios <= set(cases):
        return False
    # Every bound case outside the scenarios' own must be spelled by the item
    # itself (an explicit id, Class.test, Class::test or a bare name with the
    # same parameter), so a forged case list never rides a genuine item.
    for case in set(cases) - from_scenarios:
        base, bracket, param = case.partition("[")
        file, _, tail = base.partition("::")
        parts = tail.split("::")
        suffix = bracket + param
        forms = [case, f"{parts[-1]}{suffix}"]
        if len(parts) >= 2:
            forms += [f"{parts[-2]}.{parts[-1]}{suffix}", f"{parts[-2]}::{parts[-1]}{suffix}"]
        if not any(re.search(r"(?<![\w/.:-])" + re.escape(form) + r"(?![\w/.:-])", item) for form in forms):
            return False
    if kind == "scenario" and set(cases) != from_scenarios:
        return False
    # And the other direction: every case the item itself spells (an explicit
    # id, Class.test, Class::test or a bare name) must be among the bound
    # cases, so a mixed claim bound on one grammar alone is rejected.
    stripped = _EXPLICIT_ID.sub(" ", item)
    spelled = [m.group(1) for m in _EXPLICIT_ID.finditer(item)]
    spelled += [m.group(1) for m in _NAMED_CASE.finditer(stripped)]
    if kind == "scenario" and spelled:
        return False
    for name in spelled:
        base, bracket, param = name.partition("[")
        suffix = bracket + param
        if "::" in base and ("/" in base.split("::")[0] or "." in base.split("::")[0]):
            if name not in cases:
                return False
            continue
        tail = "::".join(base.replace("::", ".").split("."))
        if not any(c == f"{c.split('::')[0]}::{tail}{suffix}" or c.endswith(f"::{tail}{suffix}") for c in cases):
            return False
    gate, environment = entry.get("gate"), entry.get("environment")
    if not isinstance(gate, str) or not isinstance(environment, str):
        return False
    start = entry.get("after_check") if isinstance(entry.get("after_check"), int) else None
    if start is None:
        return False
    current = record.get("source_fingerprint")
    for check in checks[start:]:
        if not (check.get("passed") and check.get("source") and check.get("source") == current
                and check.get("gate") == gate and check.get("environment") == environment):
            continue
        for row in _rows(check):
            if _row_ok(row) and set(cases) <= _row_passed_cases(row):
                return True
    return False


def _not_run_cleared(entry, checks, record=None) -> bool:
    """Cleared only by a later passing check that names the execution. The
    item's whole text decides the route (``_claim``): an identity claim (a
    scenario, an explicit id, a class or bare case name, or anything shaped
    like a case id) is cleared only by ``_bound_not_run_cleared`` on the
    engine's saved binding, so a record with the key missing, empty or
    reading ``paths`` never rides file execution (root's controls on
    d88d8c0); a path-only item is cleared by ``_paths_cleared`` with every
    identity check; a skip fact by the older executed-case route. A command
    line that mentions a path, or the engine's own discharge mark, is not
    execution (Codex review of 47bfbfd)."""
    if entry.get("kind") != "skipped":
        if record is None:
            return False
        claim = _claim(str(entry.get("item") or ""))
        binding = entry.get("binding")
        if claim == "identity":
            if not isinstance(binding, dict) or binding.get("kind") not in ("scenario", "case", "mixed"):
                return False
            return _bound_not_run_cleared(entry, checks, record)
        if claim == "paths":
            if binding is not None and not (isinstance(binding, dict) and binding.get("kind") == "paths"):
                return False
            return _paths_cleared(entry, checks, record)
        return False
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
        if not _not_run_cleared(entry, checks, record):
            unshown.append(f"task {entry.get('task')} reported NOT RUN and no later check shows it ran: "
                           f"{str(entry.get('item'))[:120]}")
    for repair in record.get("test_repairs") or []:
        # Kept case names and executed cases do not show a repaired test's
        # assertions were not weakened; only an independent review can.
        if repair.get("assertions_verified") is not True:
            unshown.append(f"authorized test repair {repair.get('path')}: its assertions are not independently "
                           "checked against the kept original")
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

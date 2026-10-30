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
_ID_END = r"(?=$|[\s,;)\]\"'`]|[.:](?:\s|$))"
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
            modules = _unittest_modules(_python_args(row["command"]))
            if modules:
                # A whole module is its file, as the engine reads it.
                operands = [m.replace(".", "/") + ".py" for m in modules]
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
                             selection=selection, report=r.get("report"), tests=r.get("tests"),
                             interpreter=r.get("interpreter")))
        return rows
    return [dict(id="check", passed=bool(check.get("passed")), required=True,
                 command=str(check.get("command") or ""), cwd=str(check.get("command_cwd") or "."),
                 selection=str(check.get("selection") or ""), report=check.get("report"), tests=None,
                 interpreter=check.get("interpreter"))]


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
    if not isinstance(report, dict) or report.get("state") != "parsed":
        # A report that did not parse (or says it is invalid, truncated or
        # anything but parsed) carries no counts and no cases the reader
        # may credit, whatever fields it still holds.
        return False
    counts = report.get("counts")
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


def _historical_paths_cleared(entry, checks, record) -> bool:
    """A path-only record written before bindings existed (no ``binding``
    key): the older reading, a later passing check whose executed case
    names (its printed ``cases.executed`` or a row's own parsed report)
    include a case of every reported file, held to the final source
    wherever the check records one. An identity claim never comes here."""
    files = [p[2:] if p.startswith("./") else p for p in _PATH.findall(str(entry.get("item") or ""))]
    start = entry.get("after_check") if isinstance(entry.get("after_check"), int) else 0
    if not files:
        return False
    for check in checks[start:]:
        if not check.get("passed"):
            continue
        if check.get("source") and check.get("source") != record.get("source_fingerprint"):
            continue
        if any(key in entry and check.get(key) != entry.get(key) for key in ("gate", "environment")):
            continue
        keys = _executed_keys(check)
        for row in _rows(check):
            if _row_ok(row):
                keys |= _row_passed_cases(row)
        ran_files = {k.split("::", 1)[0] for k in keys if "::" in k}
        if all(f in ran_files for f in files):
            return True
    return False


_PYTHON_HEAD = re.compile(r"python(\d+(\.\d+)*)?")
_UNITTEST_FLAGS = frozenset({"-v", "--verbose", "-q", "--quiet", "-b", "--buffer", "-f", "--failfast", "-c", "--catch"})
_MODULE_NAME = re.compile(r"^[A-Za-z_]\w*(?:\.[A-Za-z_]\w*)*$")


def _python_args(command: str):
    """The arguments after a python interpreter head, the engine's report
    token left out; None for anything else."""
    import shlex
    try:
        argv = shlex.split(str(command or ""))
    except ValueError:
        return None
    if not argv or not _PYTHON_HEAD.fullmatch(Path(argv[0]).name):
        return None
    return [a for a in argv[1:] if not a.startswith("--quadratus-report")]


def _unittest_modules(args):
    """The modules of a plain ``-m unittest <module>...`` argument list (the
    runner's plain flags and the engine's report token aside), or None for
    any other form: the same reading the engine makes."""
    if not isinstance(args, list) or args[:2] != ["-m", "unittest"]:
        return None
    modules = []
    for token in args[2:]:
        if token in _UNITTEST_FLAGS or token.startswith("--quadratus-report"):
            continue
        if token.startswith("-") or token == "discover" or not _MODULE_NAME.match(token):
            return None
        modules.append(token)
    return modules or None


_CLASS_ID = re.compile(r"^[\w./-]+\.py::(?!test)[A-Za-z_]\w*$")
_CLASS_MEMBER = re.compile(r"^test\w*(?:\[.*\])?$")
_PYTEST_VALUE_FLAGS = frozenset({"-p", "-o", "-c", "-W", "-n", "-r", "--tb", "--rootdir", "--confcutdir", "--basetemp",
                                 "--junitxml", "--maxfail", "--durations", "--color", "--override-ini",
                                 "--import-mode", "--ignore", "--ignore-glob", "--deselect", "--log-level",
                                 "--log-file", "--timeout"})


def _collects_whole_file(command: str, file: str) -> bool:
    """Whether a recorded command line, run from the project root, collects
    the test file whole, reconstructed here from the command text alone
    (no disk, no engine): pytest naming the file as a whole operand, a
    directory operand that is a path prefix of it, or no path operand at
    all, with no ``-o``/``--override-ini``; or the plain ``python -m
    unittest <module>`` form whose module is this file. A ``::`` operand, a
    filter, a selection inside a module, ``unittest discover`` (its receipt
    records an unestablished selection), a wrapper or any other shape
    establishes nothing. The
    engine additionally reads the project's pytest configuration, which
    the record does not carry; this reading is as strong as the record."""
    import posixpath
    import shlex
    try:
        argv = shlex.split(command)
    except ValueError:
        return False
    if not argv or _filtered(command) or not file.endswith(".py"):
        return False
    head = Path(argv[0]).name
    is_python = bool(_PYTHON_HEAD.fullmatch(head))
    if head in ("pytest", "py.test") or (is_python and argv[1:3] == ["-m", "pytest"]):
        tokens = argv[1:] if head in ("pytest", "py.test") else argv[3:]
        if any(t in ("-o", "--override-ini") or t.startswith(("-o", "--override-ini=")) for t in tokens):
            return False
        operands, skip = [], False
        for token in tokens:
            if skip:
                skip = False
                continue
            if token.startswith("--quadratus-report"):
                continue
            if token.startswith("-"):
                skip = token in _PYTEST_VALUE_FLAGS
                continue
            operands.append(token[2:] if token.startswith("./") else token)
        if not operands:
            return True
        for operand in operands:
            if "::" in operand:
                return False
            if operand == file or operand in (".", "") or file.startswith(posixpath.normpath(operand).rstrip("/") + "/"):
                return True
        return False
    if is_python and argv[1:3] == ["-m", "unittest"]:
        modules = _unittest_modules(_python_args(command))
        return bool(modules) and file in [m.replace(".", "/") + ".py" for m in modules]
    return False


def _class_not_run_cleared(entry, checks, record) -> bool:
    """A NOT RUN item the engine bound to a whole test class
    (``binding.kind == "class"``), validated from the saved record alone
    and never from an engine mark: the item is exactly ``file::Class``
    and the binding names that file and class; the declared floor is a
    non-empty list of case ids directly under the class and the typed
    cases are exactly that floor (a subset is a forgery); then a later
    check on the final source under the entry's own gate and environment
    holds a whole successful row from the project root whose command
    collects the file whole (``_collects_whole_file``, reconstructed from
    the command text), whose parsed report says its passed list is
    complete, and whose passed cases include at least one case directly
    under the class and every declared one. A selective command, a
    truncated report, a case under a longer class name, a nested class or
    a missing declared method never clears it (class-claim batch,
    2026-10-30)."""
    binding = entry.get("binding")
    if not isinstance(binding, dict) or binding.get("kind") != "class":
        return False
    file, class_name = binding.get("file"), binding.get("class_name")
    if not isinstance(file, str) or not isinstance(class_name, str) or not file or not class_name:
        return False
    class_id = f"{file}::{class_name}"
    if not _CLASS_ID.match(class_id) or binding.get("class_id") != class_id:
        return False
    item = str(entry.get("item") or "").strip()
    if (item[2:] if item.startswith("./") else item) != class_id:
        return False
    declared, cases = binding.get("declared"), entry.get("cases")
    if not isinstance(declared, list) or not declared or not isinstance(cases, list) or sorted(cases) != sorted(declared):
        return False
    prefix = class_id + "::"
    for case in declared:
        if not isinstance(case, str) or not case.startswith(prefix) or not _CASE_ID.match(case):
            return False
        member = case[len(prefix):]
        if "::" in member or "[" in member or not _CLASS_MEMBER.match(member):
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
            if not _row_ok(row) or row["report"].get("passed_truncated") is not False:
                continue
            if not _collects_whole_file(row["command"], file):
                continue
            shown = set()
            for name in row["report"].get("passed_cases") or ():
                name = str(name)
                if not name.startswith(prefix):
                    continue
                member = name[len(prefix):]
                if "::" in member or not _CLASS_MEMBER.match(member):
                    continue
                shown.add(f"{prefix}{member.split('[', 1)[0]}")
            if shown and set(declared) <= shown:
                return True
    return False


def _command_not_run_cleared(entry, checks, record) -> bool:
    """A NOT RUN item the engine bound to a ``python -m unittest <module>``
    command (``binding.kind == "command"``), validated from the saved record
    alone: the item is that command (plain runner flags only), every named
    module is one of the binding's files, every bound case is a case id in
    one of those files, and a later check on the final source under the
    entry's own gate and environment holds a whole successful row of exactly
    that command, from the project root, whose own report shows every bound
    case passed. The same cases under another command never clear it
    (completion workflow, 2026-10-30)."""
    binding = entry.get("binding")
    cases = entry.get("cases")
    if not isinstance(binding, dict) or binding.get("kind") != "command":
        return False
    if not isinstance(cases, list) or not cases or not all(isinstance(c, str) and _CASE_ID.match(c) for c in cases):
        return False
    files = binding.get("files")
    if not isinstance(files, list) or not files or not all(isinstance(f, str) and f.endswith(".py") for f in files):
        return False
    args = _python_args(str(entry.get("item") or ""))
    declared = _python_args(str(binding.get("command") or ""))
    if args is None or declared is None or args != declared:
        return False
    modules = _unittest_modules(args)
    if not modules or sorted(m.replace(".", "/") + ".py" for m in modules) != sorted(files):
        return False
    # The interpreter the engine resolved when it bound the item; a row
    # matches only with the same resolved interpreter recorded when it ran
    # (never a basename).
    interpreter = binding.get("interpreter")
    if not isinstance(interpreter, str) or not interpreter:
        return False
    if any(c.split("::", 1)[0] not in files for c in cases):
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
            if (_row_ok(row) and _python_args(row["command"]) == declared and row.get("interpreter") == interpreter
                    and set(cases) <= _row_passed_cases(row)):
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
        binding = entry.get("binding")
        if isinstance(binding, dict) and binding.get("kind") == "command":
            # The engine's command binding is validated whole by its own
            # route; the item's text alone never decides it.
            return _command_not_run_cleared(entry, checks, record)
        if isinstance(binding, dict) and binding.get("kind") == "class":
            return _class_not_run_cleared(entry, checks, record)
        claim = _claim(str(entry.get("item") or ""))
        if claim == "identity":
            if not isinstance(binding, dict) or binding.get("kind") not in ("scenario", "case", "mixed"):
                return False
            return _bound_not_run_cleared(entry, checks, record)
        if claim == "paths":
            if "binding" not in entry:
                return _historical_paths_cleared(entry, checks, record)
            if not (isinstance(binding, dict) and binding.get("kind") == "paths"):
                return False
            return _paths_cleared(entry, checks, record)
        return False
    if entry.get("kind") == "skipped":
        return _skipped_cleared(entry, checks, record)
    return False


def _typed_skip_row(entry, record):
    """The typed obligation the engine registered for this skip fact (kind
    ``skipped``, same task and item), read from the saved record; None for
    a record written before typed rows existed."""
    for row in (record or {}).get("evidence_obligations") or []:
        if (isinstance(row, dict) and row.get("kind") == "skipped" and row.get("task") == entry.get("task")
                and (row.get("entry") or {}).get("item") == entry.get("item")):
            return row
    return None


def _count(value):
    """A counter the record may be trusted with: a non-negative int that is
    not a bool; anything else is None (malformed)."""
    return value if isinstance(value, int) and not isinstance(value, bool) and value >= 0 else None


def _skipped_tally(row):
    """The skipped total the runner itself reported for a command, read
    independently of the case record: the parsed report's integer count,
    else the runner's own summary line (node ``# skipped N`` or
    ``ℹ skipped N``, pytest ``N skipped``, unittest ``skipped=N``), else
    None (unknown, never zero)."""
    report = row.get("report")
    if isinstance(report, dict) and report.get("state") == "parsed":
        counts = report.get("counts")
        return _count(counts.get("skipped")) if isinstance(counts, dict) else None
    output = str(row.get("output") or "")
    node = re.findall(r"(?m)^(?:#|\u2139)\s*(?:skipped|skip) (\d+)\s*$", output)
    if node:
        return int(node[-1])
    for line in reversed(output.splitlines()):
        found = re.findall(r"\b(\d+) (passed|failed|skipped|deselected|errors?)\b", line)
        if found:
            return sum(int(n) for n, kind in found if kind == "skipped")
    unittest = re.findall(r"skipped=(\d+)", output)
    return int(unittest[-1]) if unittest else None


def _complete_cases(row):
    """The command's whole case record, or None when it cannot be trusted
    to be complete: ``cases`` must be a mapping with ``executed`` and
    ``skipped`` mappings whose every counter is a non-negative int (no
    bool, no negative, no text), and the skipped counters must account
    for exactly the skipped total the runner reported (an unknown total
    is unknown). A record that lists fewer skips than the runner counted
    cannot show that a named case was not among the missing ones (root's
    control on 5ce8ebc: ``cases.skipped`` emptied under a raw ``skipped
    1`` cleared the fact)."""
    cases = row.get("cases")
    if not isinstance(cases, dict):
        return None
    executed, skipped = cases.get("executed"), cases.get("skipped")
    if not isinstance(executed, dict) or not isinstance(skipped, dict):
        return None
    other = cases.get("other") if cases.get("other") is not None else {}
    if not isinstance(other, dict):
        return None
    for mapping in (executed, skipped, other):
        for name, value in mapping.items():
            if not isinstance(name, str) or _count(value) is None:
                return None
    tally = _skipped_tally(row)
    if tally is None or sum(skipped.values()) != tally:
        return None
    return dict(executed=executed, skipped=skipped)


def _counts_executed(cases, wanted) -> bool:
    """Every wanted name executed at least as many times as the fact names
    it (the validated record's counters, never a set of keys) and skipped
    zero times; the record itself must be complete (``_complete_cases``)."""
    from collections import Counter
    if cases is None:
        return False
    for name, times in Counter(wanted).items():
        if cases["executed"].get(name, 0) < times or cases["skipped"].get(name, 0) > 0:
            return False
    return True


def _skipped_cleared(entry, checks, record) -> bool:
    """A skip fact (the harness's own record of a required command that
    passed with named cases skipped) is cleared only by a later run of the
    same command (the receipt id the fact names) that passed, was required,
    selected nothing, and whose own printed case counters show every named
    case executed as many times as the fact names it and skipped zero
    times. A fact with a typed row in the record is held to that row's
    current identity, read independently: the check must record the final
    source, the row's gate and environment, and the row's requirement
    revisions. A record written before typed rows existed (no typed row,
    no identity keys on the entry) is the separate historical rule: the
    same command's counters on the final source wherever the check
    records one. Another command's run, a name printed elsewhere, a fact
    without names, or a count the record does not carry never clears
    (root's review of e1d9c01: counters were read as sets and the typed
    identity was not enforced)."""
    wanted = list(entry.get("cases") or [])
    receipt_id = entry.get("receipt")
    if not wanted or not receipt_id:
        return False
    typed = _typed_skip_row(entry, record)
    historical = typed is None and "gate" not in entry and "environment" not in entry
    if typed is None and not historical:
        return False
    start = entry.get("after_check") if isinstance(entry.get("after_check"), int) else 0
    final = (record or {}).get("source_fingerprint")
    for check in checks[start:]:
        if not check.get("passed"):
            continue
        if historical:
            if check.get("source") and final and check.get("source") != final:
                continue
        else:
            if not (final and check.get("source") == final):
                continue
            if check.get("gate") != typed.get("gate") or check.get("environment") != typed.get("environment"):
                continue
            revisions = check.get("requirements") if isinstance(check.get("requirements"), dict) else None
            bound = [(str(r[0]), r[1]) for r in (typed.get("requirements") or ()) if isinstance(r, (list, tuple)) and len(r) == 2]
            if revisions is None or not bound or any(revisions.get(rid) != rev for rid, rev in bound):
                continue
        receipts = check.get("receipts") or []
        if receipts:
            rows = [r for r in receipts if isinstance(r, dict) and r.get("id") == receipt_id]
        else:
            rows = [dict(status="passed" if check.get("passed") else "failed", required=True,
                         command=check.get("command"), selection=check.get("selection"),
                         cases=check.get("cases"), output=check.get("output"),
                         report=check.get("report"))] if receipt_id == "check" else []
        for row in rows:
            if row.get("status") != "passed" or not row.get("required", True) or row.get("selection"):
                continue
            if _filtered(str(row.get("command") or "")):
                continue
            if _counts_executed(_complete_cases(row), wanted):
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
        cleared = _not_run_cleared(entry, checks, record)
        if entry.get("kind") != "skipped" and entry.get("discharge_display") == "bound/1":
            # The engine's own display of the discharge must say what this
            # reading of the record says (completion workflow, 2026-10-30);
            # a mark the record does not justify, or a clearance the display
            # does not show, is a record that disagrees with itself.
            marked = entry.get("discharged_at") is not None
            if marked != cleared:
                unshown.append(f"task {entry.get('task')}: the engine shows the NOT RUN item "
                               f"{'discharged' if marked else 'not discharged'} but the record "
                               f"{'does not show it ran' if marked else 'shows it ran'}: {str(entry.get('item'))[:120]}")
        if not cleared:
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

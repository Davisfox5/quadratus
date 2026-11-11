"""Independent truth check of one finished run's record.

Reads ``.quadratus/runs/<id>/result.json`` (and ``changes.diff``) and decides,
from the record alone, whether the run's success claim is backed by evidence.
It imports nothing from ``quadratus``: a bug in the engine's own accounting
must not also be a bug in the check of it. No model is called.

Three verdicts, and missing evidence is never success:

- ``verified``: completed, every listed requirement met by an audit and the
  last audit asked for actually ran, the final
  passing check ran test cases against the final source, nothing required was
  skipped, nothing reported NOT RUN still stands, no finding is open, no
  authorized test repair is left unreviewed, no editing call left a file in
  a folder the measured diff does not read, the run did not start over a
  watched path the operator accepted, and every
  UI change carries approved capture evidence bound by digest to the bytes
  the reviewer was handed.
- ``unverified``: the run says it succeeded but some of that is not shown.
- ``not-met``: the record itself says the run failed, stopped or left a
  requirement unmet.

A baseline run (``mode: "baseline"``, one model alone) is never verified: it
has no judge, so it is unverified when it ran clean and its gate passed, and
not met when it stopped or its last check failed.

Usage: ``python -I tools/truth_check.py RUN_DIR [--project PATH] [--json]``.
Exit status 0 verified, 1 unverified, 2 not met. Adopted from Codex's review
of the efficiency plan (2026-10-10): the check must look at the right tests,
the right source, skipped checks and UI capture evidence, not "a test passed".
"""

from __future__ import annotations

import argparse
import ast
import hashlib
import json
import os
import re
import sys
from pathlib import Path

VERIFIED, UNVERIFIED, NOT_MET = "verified", "unverified", "not-met"
EXIT = {VERIFIED: 0, UNVERIFIED: 1, NOT_MET: 2}
#: Changed files that are rendered UI; one of these without capture evidence
#: is an unshown UI claim.
UI_SUFFIXES = (".html", ".htm", ".css", ".scss", ".sass", ".less", ".jsx", ".tsx", ".vue", ".svelte")
VIEWS = ("desktop", "mobile")
_DIFF_FILE = re.compile(r"^(?:\+\+\+|---) (.+)$")
_PATH = re.compile(r"[\w./-]+\.\w+")


def _check_records(value):
    """``(checks, problem)`` for a record's ``checks``: the list when it is a
    list of objects, else an empty list and why nothing was read from it. A
    record is written by a run this tool does not control, so its shape is
    read before any field of it is."""
    if value is None:
        return [], ""
    if not isinstance(value, list):
        return [], (f"the record's checks are not a list (found {type(value).__name__}), so no check "
                    "was read from it")
    strays = sum(1 for check in value if not isinstance(check, dict))
    if strays:
        return [], (f"{strays} of the record's {len(value)} check(s) are not check records, so no check "
                    "was read from it")
    return value, ""


def _last_check(record):
    """The latest check the workflow recorded, with the source it ran on."""
    latest = None
    workflow = record.get("workflow")
    tasks = workflow.get("tasks") if isinstance(workflow, dict) else None
    for task in tasks if isinstance(tasks, list) else []:
        checks = task.get("checks") if isinstance(task, dict) else None
        for check in checks if isinstance(checks, list) else []:
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


def _diff_headers(diff: str):
    """Lines outside unified-diff hunks, parsed independently of the engine.

    An added line whose text begins ``++ b/page.html`` is hunk payload,
    even though its diff representation looks exactly like a file header.
    The hunk's old/new line counts decide when headers may be read again.
    """
    old = new = 0
    for line in (diff or "").splitlines():
        if old > 0 or new > 0:
            if line.startswith("-"):
                old -= 1
            elif line.startswith("+"):
                new -= 1
            elif line.startswith(" "):
                old -= 1
                new -= 1
            continue
        hunk = re.match(r"^@@ -\d+(?:,(\d+))? \+\d+(?:,(\d+))? @@", line)
        if hunk:
            old, new = (int(n) if n is not None else 1 for n in hunk.groups())
            continue
        yield line


def _ui_files(diff: str):
    # Both sides matter: a deleted template has only an ``a/`` path, and
    # Git C-quotes paths with tabs, quotes or non-ASCII bytes by default.
    # Looking only for ``+++ b/`` silently treated those UI changes as code.
    paths = set()
    for line in _diff_headers(diff):
        if line.startswith(("Binary file changed: ", "Empty file added/deleted: ")):
            path = line.partition(": ")[2]
            if path.lower().endswith(UI_SUFFIXES):
                paths.add(path)
            continue
        header = _DIFF_FILE.match(line)
        if header is None:
            continue
        path = header.group(1)
        if path.startswith('"'):
            try:
                path = ast.literal_eval(path)
            except (ValueError, SyntaxError):
                continue
        if isinstance(path, str) and path.startswith(("a/", "b/")):
            path = path[2:]
            if path.lower().endswith(UI_SUFFIXES):
                paths.add(path)
    return sorted(paths)


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


def _approved_test_cases(name, data, *, unique=True):
    """Literal case identities, independently of the workflow's parser.

    Python retains bodies for unchanged-case checks. JavaScript names need
    literal, unique declarations: computed names and escaped strings are not
    evidence of a runner's displayed identity.
    """
    text = data.decode('utf-8')
    if name.endswith('.py'):
        nodes = [n for n in ast.walk(ast.parse(text))
                 if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef)) and n.name.startswith('test_')]
        names = [n.name for n in nodes]
        bodies = {n.name: ast.dump(n, include_attributes=False) for n in nodes}
    elif re.search(r'\.(?:test|spec)\.[cm]?[jt]sx?$', name):
        # Comments and unrelated strings cannot declare cases. Keep string
        # tokens so the argument following test/it can be checked literally.
        token = re.compile(r'''//[^\n]*|/\*[\s\S]*?\*/|"(?:\\.|[^"\\])*"|'(?:\\.|[^'\\])*'|`(?:\\.|[^`\\])*`|[A-Za-z_$][\w$]*|[^\s]''')
        tokens = [m.group() for m in token.finditer(text) if not m.group().startswith(('//', '/*'))]
        names = []
        for index, item in enumerate(tokens):
            if item not in ('test', 'it') or (index and tokens[index - 1] in ('.', '?.')):
                continue
            tail = tokens[index + 1: index + 4]
            if tail and tail[0] == '.':
                raise ValueError('modified JavaScript test declarations need independent inspection')
            if not tail or tail[0] != '(':
                continue
            if (len(tail) != 3 or tail[1][:1] not in ('"', "'") or '\\' in tail[1]
                    or tail[2] not in (',', ')')):
                raise ValueError('JavaScript test names must be literal')
            names.append(tail[1][1:-1])
        bodies = dict.fromkeys(names)
    else:
        raise ValueError('unsupported test file')
    if (unique and len(names) != len(set(names))) or any(not name for name in names):
        raise ValueError('test case identities are ambiguous')
    return bodies


def _approval_project_tests(project):
    """Current case names for attribution of file-less JS runner output."""
    result = {}

    def unreadable(error):
        raise error

    for base, folders, files in os.walk(project, followlinks=False, onerror=unreadable):
        folders[:] = [n for n in folders if n not in
                      ('.git', '.quadratus', '.venv', 'venv', 'node_modules', '__pycache__')
                      and not (Path(base) / n).is_symlink()]
        for name in files:
            if not re.search(r'(?:^test_.*\.py$|_test\.py$|\.(?:test|spec)\.[cm]?[jt]sx?$)', name):
                continue
            path = Path(base) / name
            if path.is_symlink() or not path.is_file():
                raise ValueError('test source is not a regular project file')
            result[path.relative_to(project).as_posix()] = _approved_test_cases(name, path.read_bytes(), unique=False)
    return result


def _test_change_approvals(record, run_dir, project):
    """Independently validate exact human decisions, retained bytes and execution.

    A historical original-test failure is superseded only by this evidence,
    never merely by the engine's 'approved' label.
    """
    if not record.get('test_change_decisions'):
        return {}, []

    def require(condition, message):
        if not condition:
            raise ValueError(message)

    valid, problems, history = {}, [], []
    entries = record.get('test_change_decisions') or []
    latest = {e.get('path'): e for e in entries if isinstance(e, dict) and e.get('status') == 'approved'}
    fields = ('goal', 'path', 'original_sha256', 'current_sha256', 'reason', 'cases')
    last = (record.get('checks') or [{}])[-1]
    if not isinstance(last, dict):
        return {}, ['test changes have no readable final check']
    pools = [last, *(last.get('receipts') or [])]
    executed, skipped, failed = set(), set(), set()
    for row in pools:
        if not isinstance(row, dict):
            continue
        cases = row.get('cases') or {}
        if not isinstance(cases, dict):
            cases = {}
        skipped.update(k for k, n in (cases.get('skipped') or {}).items() if n)
        failed.update(k for k, n in (cases.get('failed') or {}).items() if n)
        if (row.get('passed') is True or row.get('status') == 'passed') and row.get('required') is not False:
            executed.update(k for k, n in (cases.get('executed') or {}).items()
                            if isinstance(k, str) and type(n) is int and n > 0)
            report = row.get('report') or {}
            counts = report.get('counts') if isinstance(report, dict) else None
            passed = report.get('passed_cases') if isinstance(report, dict) else None
            if (isinstance(report, dict) and report.get('state') == 'parsed'
                    and type(report.get('exitstatus')) is int and report['exitstatus'] == 0
                    and report.get('passed_truncated') is False and report.get('truncated') is False
                    and report.get('collect_errors') == 0 and isinstance(counts, dict)
                    and all(type(counts.get(k)) is int and counts[k] == 0 for k in ('failed', 'errors', 'skipped'))
                    and isinstance(passed, list) and all(isinstance(k, str) and k for k in passed)
                    and type(counts.get('passed')) is int
                    and len(passed) == len(set(passed)) == counts.get('passed')):
                executed.update(passed)
    for entry in record.get('test_change_decisions') or []:
        if not isinstance(entry, dict) or entry.get('status') != 'approved':
            continue
        name = str(entry.get('path') or '')
        try:
            proposal = {k: entry[k] for k in fields}
            digest = hashlib.sha256(json.dumps(proposal, sort_keys=True).encode()).hexdigest()
            require(entry.get('id') == digest and proposal['goal'] == record.get('goal'), 'missing or inconsistent evidence')
            require(proposal['reason'] and isinstance(proposal['cases'], list) and proposal['cases'], 'missing or inconsistent evidence')
            if entry.get('approval') == 'operator reply':
                require(str(entry.get('answer', '')).strip() == f'APPROVE TEST CHANGE {digest}', 'missing or inconsistent evidence')
                require(f"Test change {digest} for {name}: {entry['answer']}" in ((record.get('requirements') or {}).get('operator_rulings') or []), 'missing or inconsistent evidence')
            else:
                require(entry.get('approval') == 'operator exact bytes', 'missing or inconsistent evidence')
                require(proposal in (record.get('test_change_authorizations') or []), 'missing or inconsistent evidence')
            bodies = {}
            for label in ('original', 'delivered'):
                aid = entry.get(label + '_artifact')
                require(isinstance(aid, str) and re.fullmatch('[0-9a-f]{12}', aid), 'missing or inconsistent evidence')
                path = Path(run_dir) / 'artifacts' / (aid + '.txt')
                require(not path.parent.is_symlink() and (not path.is_symlink()) and path.is_file(), 'missing or inconsistent evidence')
                bodies[label] = path.read_bytes()
                key = 'original_sha256' if label == 'original' else 'current_sha256'
                require(hashlib.sha256(bodies[label]).hexdigest() == proposal[key], 'missing or inconsistent evidence')
            old = _approved_test_cases(name, bodies['original'])
            new = _approved_test_cases(name, bodies['delivered'])
            changes = {c['case']: c for c in proposal['cases']}
            require(old and set(changes) <= set(old) and (len(changes) == len(proposal['cases'])), 'missing or inconsistent evidence')
            if entry is not latest[name]:
                history.append((digest, name, proposal['original_sha256']))
                continue
            require(last.get('passed') and last.get('source') == record.get('source_fingerprint'), 'missing or inconsistent evidence')
            require(entry.get('checked_source') == record.get('source_fingerprint'), 'missing or inconsistent evidence')
            if project is not None:
                require(not name.startswith('/') and '..' not in Path(name).parts, 'missing or inconsistent evidence')
                target = Path(project) / name
                require(not target.is_symlink() and hashlib.sha256(target.read_bytes()).hexdigest() == proposal['current_sha256'], 'missing or inconsistent evidence')
            wanted = set()
            for case, body in old.items():
                change = changes.get(case)
                if change is None:
                    require(new.get(case) == body, 'missing or inconsistent evidence')
                    wanted.add((name, case))
                elif change['action'] == 'retire':
                    require(case not in new and change['replacements'] == [], 'missing or inconsistent evidence')
                else:
                    require(change['action'] == 'replace' and change['replacements'], 'missing or inconsistent evidence')
                    wanted.update((r['path'], r['case']) for r in change['replacements'])
            for path, case in wanted:
                require(isinstance(path, str) and not path.startswith('/') and '..' not in Path(path).parts,
                        'replacement test path is not project relative')
                declared = new if path == name else None
                if project is not None:
                    target = Path(project)
                    for part in Path(path).parts:
                        target /= part
                        require(not target.is_symlink(), 'replacement test path is a symlink')
                    require(target.resolve().is_relative_to(Path(project).resolve()), 'replacement is outside project')
                    declared = _approved_test_cases(path, target.read_bytes())
                require(declared is not None and case in declared, 'replacement test declaration is not established')
                js = not path.endswith('.py')
                if js and project is not None:
                    all_cases = _approval_project_tests(Path(project))
                    require(sum(case in cases for cases in all_cases.values()) == 1,
                            'JavaScript test name is ambiguous in the project')
                def matches(key, p=path, c=case, js=js):
                    if js:
                        return key == f'{p}::{c}' or (key == c and project is not None)
                    return key.split('::')[0] == p and key.split('::')[-1].split('[', 1)[0] == c
                require(any((matches(k) for k in executed))
                        and not any((matches(k) for k in skipped | failed)), 'missing or inconsistent evidence')
            valid[digest] = name
        except (AssertionError, KeyError, TypeError, ValueError, OSError, SyntaxError) as exc:
            problems.append(f'authorized test change {name}: approval, bytes or current execution not established'
                            + (f' ({exc})' if str(exc) else ''))
    for digest, name, original in history:
        current = latest[name]
        if current.get('id') in valid and current.get('original_sha256') == original:
            valid[digest] = name
    return valid, problems



def judge(run_dir, project=None) -> dict:
    """The verdict on one run folder. Never raises on what the record holds:
    a record that is not shaped as a run record is unverified, with why, and
    whatever it had already shown as failed still counts."""
    run_dir = Path(run_dir)
    failed, unshown = [], []
    try:
        early = _judge(run_dir, project, failed, unshown)
    except (AttributeError, TypeError, KeyError, IndexError, ValueError) as exc:
        early = None
        unshown.append(f"the record is not shaped as a run record ({type(exc).__name__}: {str(exc)[:120]}), "
                       "so the rest of it was not read")
    if early is not None:
        return early
    verdict = NOT_MET if failed else UNVERIFIED if unshown else VERIFIED
    return dict(verdict=verdict, run=str(run_dir), not_met=failed, unverified=unshown)


def _judge(run_dir, project, failed, unshown):
    """Read the record into ``failed`` and ``unshown``. Returns a finished
    verdict only when there is no record to read."""
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

    # What the record itself says failed. A baseline run (one model alone,
    # ``mode: "baseline"``) never claims completion, so "did not complete" is
    # not its failure: it is unshown by construction, and can never read as
    # verified whatever else its record says.
    if record.get("mode") == "baseline":
        unshown.append("baseline run: one model alone; nothing judged the goal or its requirements")
    elif not record.get("completed"):
        failed.append("the run did not complete")
    if record.get("error"):
        failed.append(f"the run stopped with an error: {str(record['error'])[:200]}")
    if record.get("in_flight"):
        failed.append("work was still in flight when the run stopped")
    if record.get("failed_tasks"):
        failed.append(f"failed tasks: {', '.join(map(str, record['failed_tasks']))[:200]}")
    checks, misshapen = _check_records(record.get("checks"))
    if misshapen:
        unshown.append(misshapen)
        # Every later reader of the record's checks gets the list just read.
        record = dict(record, checks=checks)
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
    if not checks and not misshapen:
        unshown.append("no check ran")
    last = _last_check(record)
    if last is not None and not isinstance(last, dict):
        unshown.append("the latest check the workflow recorded is not a check record")
        last = None
    elif checks and last is None:
        unshown.append("the checks carry no record of the source they ran on")
    if last is not None:
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
    migrations, migration_problems = _test_change_approvals(record, run_dir, project)
    unshown.extend(migration_problems)
    original_runs = record.get("original_test_runs") or []
    for index, run in enumerate(original_runs):
        if run.get("passed") is not True:
            superseded = run.get('superseded_tests') or {}
            approved = {name for name, rid in superseded.items() if migrations.get(rid) == name}
            remaining = set(run.get('changed') or []) - approved
            if approved and (not remaining or any(later.get('passed') is True
                                                  and remaining <= set(later.get('changed') or [])
                                                  for later in original_runs[index + 1:])):
                continue
            unshown.append(f"task {run.get('task')}: the run-start tests did not pass "
                           f"({str(run.get('problem') or run.get('passed'))[:120]})")
    if not listed:
        unshown.append("no requirements were recorded, so none was audited")
    recorded = [a for a in requirements.get("audits") or [] if isinstance(a, dict)]
    audits = [a for a in recorded if isinstance(a.get("verdicts"), dict)]
    final = audits[-1]["verdicts"] if audits else {}
    if recorded and not isinstance(recorded[-1].get("verdicts"), dict):
        # The last audit the run asked for did not run (no auditor, a failed
        # call, a tree that moved). An earlier audit judged an earlier source,
        # so its verdicts do not stand in for it.
        unshown.append(f"the last requirements audit did not run ({str(recorded[-1].get('result'))[:120]})")
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
    for repair in record.get("test_repairs") or []:
        if repair.get("path") in migrations.values():
            continue
        # Kept case names and executed cases do not show a repaired test's
        # assertions were not weakened; only an independent review can.
        if repair.get("assertions_verified") is not True:
            unshown.append(f"authorized test repair {repair.get('path')}: its assertions are not independently "
                           "checked against the kept original")
    for finding in record.get("findings") or []:
        if finding.get("status") == "open":
            unshown.append(f"open finding {finding.get('id')}: {str(finding.get('message'))[:120]}")
    # A path outside the measured source that an editing call of this run
    # changed (a git hook, an env file, a link). No diff or check covers it.
    watch = record.get("project_watch")
    watched = watch.get("paths") if isinstance(watch, dict) else None
    if isinstance(watched, list) and watched:
        named = ", ".join(str(w.get("path")) for w in watched[:5] if isinstance(w, dict))
        unshown.append(f"an editing call changed {len(watched)} watched path(s) outside the measured "
                       f"source, which no diff or check covers: {named[:160]}")
    # Files an editing call changed in folders the measured diff does not read
    # (build output, report folders, tool environments). The checks ran with
    # them present and nothing on the record says what they hold, so the last
    # check did not run on the measured source alone. Any name counts. The
    # first rule went by file type, and a run whose tests passed only because
    # of build/helper.txt read verified: a check reads a text file, an archive
    # or a native module as readily as a .py file. The engine also keeps a
    # bounded list, so the names here are not always all of them.
    unmeasured = record.get("unmeasured_changes")
    if unmeasured is not None:
        names, readable = set(), isinstance(unmeasured, dict)
        for listed in unmeasured.values() if readable else ():
            if isinstance(listed, list):
                names.update(str(name) for name in listed)
            else:
                readable = False
        if not readable:
            unshown.append("the record's unmeasured_changes is not a list of names for each task, so what "
                           "the editing calls left outside the measured diff is not known")
        if names:
            unshown.append("editing calls changed files in folders the measured diff does not read "
                           f"({len(names)} listed), and the checks ran with them present: "
                           f"{', '.join(sorted(names)[:5])[:160]}")
    # A watched path an earlier run left changed, which the operator accepted
    # at this run's start. Their call to make; no diff or check of this run
    # covers what that path holds.
    trust = record.get("trust")
    accepted = trust.get("watched_paths_accepted") if isinstance(trust, dict) else None
    if (trust is not None and not isinstance(trust, dict)) or (
            accepted is not None and not isinstance(accepted, list)):
        unshown.append("the record's trust section is not readable, so whether the run started over an "
                       "accepted watched path is not known")
    elif accepted:
        named = ", ".join(str(a.get("path")) for a in accepted[:5] if isinstance(a, dict))
        unshown.append(f"the run started over {len(accepted)} watched path(s) an earlier run left changed, "
                       f"accepted by the operator and covered by no diff or check: {named[:160]}")
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
    return None


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

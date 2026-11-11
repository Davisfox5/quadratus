"""What the session engine reads out of a recorded check: its tally and
source, the runner behind a command, the identity of a gate, which cases
ran or were skipped, and which test file holds a named case.

Plain functions over receipts and test-file text; none of them runs a
command or calls a model. Moved out of session.py unchanged on 2026-11-11;
quadratus.session imports every name here again.
"""
from __future__ import annotations

import dataclasses
import hashlib
import json
import re
from typing import List, Optional


def _check_tally(report, tests) -> str:
    counts = report.get("counts") if isinstance(report, dict) and report.get("state") == "parsed" else None
    if isinstance(counts, dict):
        return (f"{counts.get('passed', 0)} passed, {counts.get('failed', 0) + counts.get('errors', 0)} "
                f"failed, {counts.get('skipped', 0)} skipped")
    if isinstance(tests, int):
        return f"{tests} tests run, skips and failures not parsed"
    return "counts unknown"


def _check_source(source, current) -> str:
    if source and current and source == current:
        return "on the delivered source"
    if source and current:
        return "on an EARLIER source, not the delivered one"
    return "source not established"


def _audit_check_facts(checks, current) -> str:
    """The harness's recorded checks as facts an auditor can weigh: per
    command id, its status, executed/skipped/failed counts when known, and
    whether it ran on the delivered source. Never a command line (a gate
    can name a file seats must not open; GateResult.for_models).

    Overnight r02 (20261011T134747Z-b14aa88b): the audit saw only "gate
    suite: passed", ran the browser tests in its own copy without
    Playwright, and marked R6 NOT MET against a same-source harness pass
    of 121 Python and 64 Node with none skipped.

    A reason is exception text and can carry a command's path (a missing
    runner: ``runner unavailable: [Errno 2] ... '/private/grader/run.py'``),
    so it is redacted like check output before it is shortened."""
    from .integration import redact_command_paths
    lines = []
    for check in checks:
        paths = _check_command_paths(check)
        receipts = check.get("receipts") or []
        if not receipts:
            lines.append(f"- check: {'passed' if check.get('passed') else 'FAILED'}; "
                         f"{_check_tally(check.get('report'), None)}; {_check_source(check.get('source'), current)}")
            continue
        for r in receipts:
            lines.append(f"- {r.get('id')} ({'required' if r.get('required', True) else 'optional'}): "
                         f"{r.get('status')}: {redact_command_paths(str(r.get('reason') or ''), paths)[:80]}; "
                         f"{_check_tally(r.get('report'), r.get('tests'))}; {_check_source(r.get('source_hash'), current)}")
    return "\n".join(lines)


#: Runner labels a model may see: public program and module names only,
#: never an operand, a path or a project script's name (root review of
#: d51ae79: a script path can be a file seats must not open).
_PUBLIC_RUNNERS = frozenset({
    "pytest", "unittest", "python", "python3", "node", "nodejs", "npm", "npx", "pnpm", "yarn", "bun",
    "deno", "jest", "vitest", "mocha", "playwright", "tox", "nox", "make", "go", "cargo", "ruby", "rspec",
    "rake", "bundle", "mvn", "gradle", "dotnet", "bash", "sh", "uv", "poetry", "hatch"})


#: A Python interpreter by its program name: python, python3, python3.12,
#: pypy3, and the Windows launcher py.
_PYTHON_PROGRAM = re.compile(r"(?:python|pypy|py)[\d.]*(?:\.exe)?", re.IGNORECASE)
#: Programs that only start another command: the real one follows them.
_COMMAND_WRAPPERS = frozenset({"uv", "poetry", "hatch", "pdm", "pipenv", "env", "xvfb-run", "timeout", "nice",
                               "time"})
#: What may stand between a wrapper and the command it starts: its "run"
#: word, an option, an environment assignment, a duration.
_WRAPPER_WORD = re.compile(r"run|-.*|[A-Za-z_][A-Za-z0-9_]*=.*|\d+(?:\.\d+)?[smhd]?")


def _program_name(token: str) -> str:
    return re.split(r"[\\/]", token)[-1]


def _python_starts_at(argv) -> Optional[int]:
    """Where Python's own options begin: after the interpreter the command
    starts, or after ``coverage run``, which takes ``-m`` the same way. Only
    wrappers may come first (``uv run python``, ``env X=1 python``,
    ``timeout 600 python``). None when the command starts anything else, so
    a path that merely ends in "python" further along is never taken for the
    interpreter."""
    for index, token in enumerate(argv):
        name = _program_name(token)
        if _PYTHON_PROGRAM.fullmatch(name):
            return index + 1
        if name == "coverage" and argv[index + 1:index + 2] == ["run"]:
            return index + 2
        if name not in _COMMAND_WRAPPERS and not _WRAPPER_WORD.fullmatch(token):
            return None
    return None


def _check_runner(argv) -> str:
    """A command's runner as a public label: the module a Python
    interpreter runs with ``-m``, else the program's basename, when it is a
    known public runner; otherwise "a project command".

    ``-m`` names a module only as Python's own option. Read from any
    program it made ``pytest -m slow tests`` "a project command", because
    "slow" is no runner. The interpreter may stand behind a wrapper:
    ``uv run python -m pytest`` and ``coverage run -m pytest`` run pytest,
    and a goal that requires pytest must find it in the label."""
    argv = [str(a) for a in argv or ()]
    if not argv:
        return "a project command"
    label = _program_name(argv[0])
    if _PYTHON_PROGRAM.fullmatch(label):
        label = label if label in _PUBLIC_RUNNERS else "python"
    index = _python_starts_at(argv)
    while index is not None and index < len(argv) - 1:
        if argv[index] == "-m":
            label = argv[index + 1]
            break
        if argv[index] in ("-W", "-X"):
            index += 2      # the option's value is not a script
            continue
        if argv[index] in ("-c", "-") or not argv[index].startswith("-"):
            break           # code or a script: what follows is its own
        index += 1
    return label if label in _PUBLIC_RUNNERS else "a project command"


def _configured_checks(gate) -> str:
    """The checks the harness executes, one line per command: id, whether it
    is required, its runner label, and whether it is skipped. Never a skip
    reason, operand or path. "" with no gate."""
    if gate is None:
        return ""
    commands = getattr(gate, "commands", None)
    if commands:
        return "\n".join(f"- {c.id} ({'required' if c.required else 'optional'}): runs {_check_runner(c.argv)}"
                         + ("; skipped by configuration" if c.skip_reason else "") for c in commands)
    command = getattr(gate, "command", None)
    return f"- check (required): runs {_check_runner(command)}" if command else ""


def _gate_identity(gate) -> str:
    """A stable digest of what a gate runs and where: each command with its
    settings (argv, cwd, timeout, required, minimum tests, skip reason), the
    gate's own working directory and its test-count minimum. None for none."""
    if gate is None:
        return ""
    commands = getattr(gate, "commands", None)
    body = dict(kind=type(gate).__name__, cwd=str(getattr(gate, "cwd", "") or ""),
                command=list(getattr(gate, "command", []) or []), timeout=getattr(gate, "timeout", None),
                minimum_tests=getattr(gate, "minimum_tests", None),
                commands=[dataclasses.asdict(c) if dataclasses.is_dataclass(c) else repr(c)
                          for c in (commands or ())])
    return hashlib.sha256(json.dumps(body, sort_keys=True, default=str).encode()).hexdigest()[:16]


def _executed_clean(report, tests) -> str:
    """"" when a command's record shows cases executed with none failed or
    skipped; otherwise why it does not."""
    counts = report.get("counts") if isinstance(report, dict) and report.get("state") == "parsed" else None
    if isinstance(counts, dict):
        if counts.get("failed", 0) or counts.get("errors", 0):
            return "it has failures"
        if counts.get("skipped", 0):
            return "it skipped cases"
        return "" if counts.get("passed", 0) > 0 else "it executed no cases"
    if isinstance(tests, int) and tests > 0:
        return "its skips and failures were not parsed"
    return "its counts are unknown"


def _verified_by_check(check, current) -> str:
    """"" when the harness's recorded check passed on ``current`` with every
    command executing cases and none failing or skipping; otherwise why not.
    Only harness records count: a model's account of a run never does."""
    if not check:
        return "no project check has run"
    if not check.get("passed"):
        return "the latest project check failed"
    if not current or check.get("source") != current:
        return "the latest project check ran on an earlier source"
    receipts = check.get("receipts") or []
    if not receipts:
        why = _executed_clean(check.get("report"), None)
        return f"the latest project check: {why}" if why else ""
    for r in receipts:
        if r.get("status") != "passed":
            return f"command {r.get('id')} did not pass ({r.get('status')})"
        why = _executed_clean(r.get("report"), r.get("tests"))
        if why:
            return f"command {r.get('id')}: {why}"
    return ""


def _check_runs(check: dict, root=None) -> List[dict]:
    """Each command of a recorded check as execution evidence: whether it
    passed and was required, whether it ran only part of what it names, how
    many cases executed and how many it skipped (None when unknown), and the
    files it names whole. A check with no receipts is one required command."""
    import shlex
    from collections import Counter
    from types import SimpleNamespace

    from .integration import (
        _test_count,
        case_outcomes,
        deselected_count,
        is_filtered,
        operand_paths,
        skipped_count,
    )
    receipts = check.get("receipts") or ()
    # A single-command gate's selection and cwd are recorded on the check; its
    # receipts (the one synthesized when original tests join, and theirs)
    # keep them too, never weaker (Codex review of 67c9fad, S3 and S4).
    direct = check.get("selection") or ""
    if receipts:
        rows = [dict(id=r.get("id"), passed=r.get("status") == "passed", required=r.get("required", True),
                     command=str(r.get("command") or ""), output=r.get("output") or "", report=r.get("report"),
                     tests=r.get("tests"), cwd=r.get("cwd") or check.get("command_cwd") or ".",
                     selection="; ".join(x for x in (r.get("selection") or "", direct if r.get("id") in (
                         "check", "original-tests:check") else "") if x), cases=r.get("cases"))
                for r in receipts]
    else:
        rows = [dict(id="check", passed=bool(check.get("passed")), required=True,
                     command=str(check.get("command") or ""), output=check.get("output") or "",
                     report=check.get("report"), tests=None, cwd=check.get("command_cwd") or ".",
                     selection=direct, cases=check.get("cases"))]
    out = []
    for row in rows:
        try:
            argv = shlex.split(row["command"])
        except ValueError:
            argv = row["command"].split()
        tests = row["tests"] if row["tests"] is not None else _test_count(row["output"])
        # Selection from configuration or the environment, or cases the runner
        # reports deselected, is a filter as much as one in argv (Codex review
        # of 81adcc7, R2: pytest.ini's addopts = -k unit printed "1 passed, 1
        # deselected" for a named file and looked like a whole run).
        filtered = is_filtered(argv) or bool(row["selection"]) or deselected_count(row["output"]) > 0
        names = _receipt_names(row)
        skipped_names = names[0] if names[0] is not None else case_outcomes(row["output"])[0]
        every_names = names[1]
        qualified_names = set((row["cases"] or {}).get("qualified") or ()) if isinstance(row["cases"], dict) else set()
        executed_names = names[2] if names[2] is not None else Counter()
        out.append(dict(id=row["id"], passed=row["passed"], required=row["required"], filtered=filtered,
                        tests=tests, skipped=skipped_count(SimpleNamespace(output=row["output"],
                                                                           report=row["report"])),
                        files=set(operand_paths(argv, root, row["cwd"])),
                        skipped_names=skipped_names, executed_names=executed_names, every_names=every_names,
                        qualified_names=qualified_names))
    return out


def _receipt_names(receipt):
    """``(skipped, every, executed)`` case-key Counters for one receipt (an
    object or a recorded dict).

    Identity comes only from a complete whole-output ``cases`` record: then
    all three are known. Without one (none recorded, or past its bound) the
    skipped names may still be read from the output tail for the record's
    text, but ``every`` and ``executed`` are None, because a tail that lists
    every skip need not list every case sharing a name (Codex review of
    d157378, T2). Skipped is None too when the names do not account for every
    skip counted."""
    from collections import Counter
    from types import SimpleNamespace

    from .integration import case_outcomes, skipped_count
    get = receipt.get if isinstance(receipt, dict) else (lambda k, d=None: getattr(receipt, k, d))
    count = skipped_count(SimpleNamespace(output=get("output") or "", report=get("report")))
    record = get("cases")
    if isinstance(record, dict):
        skipped = Counter(record.get("skipped") or {})
        executed = Counter(record.get("executed") or {})
        every = skipped + executed + Counter(record.get("other") or {})
    else:
        skipped, every, executed = case_outcomes(get("output") or "")[0], None, None
    if count is None or sum(skipped.values()) != count:
        return None, None, None
    return skipped, every, executed


def _new_skips(skipped, every, start, stable):
    """The skips in ``skipped`` that are not historical against ``start``
    (``(count, skipped, every)`` from the run-start baseline, or None). A
    skip is historical only when its key is one case in both runs, that case
    was skipped at run start, and ``stable(key)`` says the one test file that
    holds it is the same file, unchanged since run start. Without complete
    identity on both sides every skip is new (S1, T2); a name reused in
    another file or revision is new too (Codex review of e530b89, U1)."""
    from collections import Counter
    if start is None or every is None or start[2] is None or start[1] is None:
        return Counter(skipped)
    start_skipped, start_every = start[1], start[2]
    return Counter({n: k for n, k in skipped.items()
                    if not (every[n] == 1 and start_every[n] == 1 and start_skipped[n] == 1 and stable(n))})


_QUOTES = ("'", '"', "`")


def _case_files(key: str, texts: dict, qualified: bool):
    """The test files that hold one case, as a frozenset, or None when that
    cannot be read. A key a pytest verbose line printed (``qualified``) names
    its file as ``path::name``; any other name, whatever punctuation it holds
    (a node name may contain ``::``, Codex review of 5292fc2, V1), is held by
    the test files whose text has it as a quoted string. A hashed long key
    cannot be searched for."""
    if "\u2026sha256:" in key:
        return None
    if qualified:
        path = key.split("::", 1)[0]
        return frozenset([path]) if path in texts else None
    found = frozenset(path for path, text in texts.items() if any(q + key + q in text for q in _QUOTES))
    return found or None


def _case_block(key: str, qualified: bool, text: str):
    """The source that defines one case in its file, or None when it cannot
    be found unambiguously. Comparing this, not the whole file, tells an
    unchanged old case from a file that merely gained another test (Codex
    review of 5292fc2, V2).

    A pytest id is resolved along its whole path (``path::Class::test``,
    parameters dropped): exactly one class or function must match at each
    step, and the block is the test function with its decorators plus each
    enclosing class's decorators and header, so the same method name in two
    classes is two cases (f2f66ff, W3). Any other name is defined by every
    registration call that quotes it, read whole by a bounded bracket scan
    that skips strings and comments, so a changed callback body on later
    lines is a changed case (W2); a call that cannot be scanned is None."""
    if qualified:
        return _pytest_block(key, text)
    blocks = []
    for quote in _QUOTES:
        literal = quote + key + quote
        at = text.find(literal)
        while at != -1:
            call = _enclosing_call(text, at)
            if call is None:
                return None
            blocks.append(call)
            at = text.find(literal, at + 1)
    return tuple(blocks) or None


def _pytest_block(key: str, text: str):
    import ast
    parts = key.split("::")[1:]
    if not parts:
        return None
    parts[-1] = parts[-1].split("[")[0]
    try:
        tree = ast.parse(text)
    except (SyntaxError, ValueError):
        return None
    lines = text.splitlines()

    def header(node, whole):
        first = min([node.lineno] + [d.lineno for d in node.decorator_list])
        last = node.end_lineno if whole else node.lineno
        return tuple(lines[first - 1:last])

    body, block = tree.body, []
    for depth, name in enumerate(parts):
        matches = [n for n in body if isinstance(n, (ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef))
                   and n.name == name]
        if len(matches) != 1:
            return None
        node = matches[0]
        final = depth == len(parts) - 1
        if final and not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            return None
        block.append(header(node, whole=final))
        body = node.body
    return tuple(block)


def _enclosing_call(text: str, at: int):
    """The text of the call whose first argument is the string literal at
    ``at``: from the identifier before its ``(`` to the matching ``)``.
    Strings and comments are skipped while matching bracket types. None
    (unknown) when the literal is not a call's first argument, when the call
    holds anything this lexical scan cannot read with certainty (a ``/`` that
    may start a regex literal, whose brackets and quotes are not structural;
    a template literal with ``${``), when a closer does not match its opener,
    or when the call does not close within 200,000 characters. A prefix is
    never returned as the whole call (Codex review of 02accbd, X1)."""
    open_at = at - 1
    while open_at >= 0 and text[open_at] in " \t\r\n":
        open_at -= 1
    if open_at < 0 or text[open_at] != "(":
        return None
    start = open_at
    while start > 0 and (text[start - 1].isalnum() or text[start - 1] in "_$."):
        start -= 1
    pairs = {")": "(", "]": "[", "}": "{"}
    stack, i, end = [], open_at, min(len(text), open_at + 200_000)
    while i < end:
        ch = text[i]
        last = _skip_lexeme(text, i, end)
        if last is None:
            return None
        if last != i:
            i = last
        elif ch in "([{":
            stack.append(ch)
        elif ch in ")]}":
            if not stack or stack.pop() != pairs[ch]:
                return None
            if not stack:
                return text[start:i + 1]
        i += 1
    return None


def _skip_lexeme(text: str, i: int, end: int):
    """One step of the bounded lexical scan: where the string or comment
    that starts at ``i`` ends (the index of its last character), ``i`` itself
    for any other character, or None when what starts there cannot be read
    with certainty: an unclosed string or block comment, a template literal
    with ``${``, or a ``/`` that may start a regex literal. A line comment
    with no newline runs to ``end``."""
    ch = text[i]
    if ch in "'\"`":
        close = i + 1
        while close < end and text[close] != ch:
            if ch == "`" and text.startswith("${", close):
                return None
            close += 2 if text[close] == "\\" else 1
        return None if close >= end else close
    if text.startswith("//", i):
        newline = text.find("\n", i)
        return end if newline == -1 else newline
    if text.startswith("/*", i):
        close = text.find("*/", i + 2)
        return None if close == -1 else close + 1
    if ch == "/":
        return None
    return i


def _named_outside_comments(text: str, name: str, python: bool) -> bool:
    """Whether ``name`` appears in a test file's text anywhere but inside a
    comment. A comment that mentions a test file does not run it (review
    2026-11-10, second half 21: a comment naming a new test file removed the
    unnamed-file observation and the audit lowering that depends on it).

    A Python file is read by the language's own tokenizer; any other test
    file by the same lexical step the registration scan uses, up to the
    first thing that step cannot read. What cannot be read for certain
    counts as a mention, as before: unknown is not uncovered."""
    if name not in text:
        return False
    if python:
        import io
        import tokenize
        try:
            inside = sum(token.string.count(name)
                         for token in tokenize.generate_tokens(io.StringIO(text).readline)
                         if token.type == tokenize.COMMENT)
        except Exception:  # noqa: BLE001 -- not readable as Python: unknown, so it counts
            return True
        return text.count(name) > inside
    from bisect import bisect_right
    starts, ends, i, certain = [], [], 0, len(text)
    while i < certain:
        last = _skip_lexeme(text, i, certain)
        if last is None:
            certain = i
            break
        if last != i and text[i] == "/":
            starts.append(i)
            ends.append(last)
        i = last + 1
    at = text.find(name)
    while at != -1:
        k = bisect_right(starts, at) - 1
        if at >= certain or k < 0 or at > ends[k]:
            return True
        at = text.find(name, at + 1)
    return False


def _complete_run(run: dict) -> bool:
    """A passing required command that ran whole files: no filter, at least
    one executed case and a known zero skips. Anything less does not show
    that a named file's cases ran (Codex review of b6ba3ba, F3 and F4)."""
    return (run["passed"] and run["required"] and not run["filtered"]
            and (run["tests"] or 0) > 0 and run["skipped"] == 0)


def _item_paths(item: str) -> List[str]:
    """The file paths a NOT RUN item names; none for a runner name or prose."""
    import shlex
    try:
        tokens = shlex.split(item)
    except ValueError:
        tokens = item.split()
    paths = []
    for token in tokens:
        token = token.split("::")[0]
        if "/" in token or re.search(r"\.[A-Za-z]\w*$", token):
            paths.append(token[2:] if token.startswith("./") else token)
    return paths


#: Stands in a check finding's ``failed_checks`` when the check failed with no
#: failed command id: the tree moved under the gate, or only the scope
#: receipt failed. No command id could ever match it, so the finding is
#: settled by a later gate that passes as a whole and reran the same commands.
_WHOLE_GATE = "(whole gate)"


def _check_ids(receipts, *, passed: bool) -> List[str]:
    """Check identities from a gate's receipts: the ids whose status is (or
    is not) passed. A single-command gate carries no receipts and is the one
    check, ``check``."""
    if not receipts:
        return ["check"]
    ids = [r.get("id") or "check" for r in receipts
           if (r.get("status") == "passed") == passed and r.get("id") != "scope"]
    return ids


def _check_for_models(check: dict) -> dict:
    """A recorded check as a seat may see it: outcomes, never commands or their paths."""
    from .integration import redact_command_paths
    ordered = _check_command_paths(check)
    return {
        "passed": check.get("passed"),
        "gates": [{"id": r.get("id"), "status": r.get("status"),
                   "reason": redact_command_paths(str(r.get("reason") or ""), ordered),
                   "tests": r.get("tests")} for r in check.get("receipts") or []],
        "output": redact_command_paths(check.get("output") or "", ordered)[-1500:],
    }


def _check_command_paths(check: dict) -> list:
    """Absolute paths named by a recorded check's commands, and the folders
    holding them, longest first: what redact_command_paths hides."""
    import shlex
    paths = set()
    for text in [check.get("command", "")] + [r.get("command", "") for r in check.get("receipts") or []]:
        try:
            tokens = shlex.split(text or "")
        except ValueError:
            tokens = (text or "").split()
        for token in tokens:
            if token.startswith("/") and len(token) > 1:
                paths.add(token.rstrip("/"))
                parent = token.rstrip("/").rsplit("/", 1)[0]
                if parent.count("/") >= 2:
                    paths.add(parent)
    return sorted(paths, key=len, reverse=True)


def _describe_tree_change(before: dict, after: dict, *, limit: int = 20) -> str:
    """Name the files that moved while the integration check was running.

    Added, removed and modified are reported separately: a test suite leaving
    artefacts behind looks nothing like a concurrent edit to a source file, and
    the old undifferentiated message made the two indistinguishable.
    """
    before = before or {}
    after = after or {}
    added = sorted(set(after) - set(before))
    removed = sorted(set(before) - set(after))
    modified = sorted(
        name for name in set(before) & set(after) if before[name] != after[name]
    )

    def show(names):
        head = ", ".join(names[:limit])
        extra = len(names) - limit
        return head + (f", and {extra} more" if extra > 0 else "")

    parts = []
    if added:
        parts.append(f"{len(added)} added ({show(added)})")
    if modified:
        parts.append(f"{len(modified)} modified ({show(modified)})")
    if removed:
        parts.append(f"{len(removed)} removed ({show(removed)})")
    detail = "; ".join(parts) or "no files differ, but the contents compared unequal"
    return (
        "Project source changed while the integration check ran; the result is "
        f"not valid. Changed paths: {detail}. A check that writes into the "
        "project invalidates its own result -- isolate those writes (a "
        "temporary directory or a fixture) rather than excluding the paths."
    )


def _selection_record(run) -> Optional[tuple]:
    """The file set of a "test selection changed" record
    (``Session._selection_changed``), or None for any other original-test
    record: those carry receipts or a pass, and each is its own run."""
    if (isinstance(run, dict) and run.get("passed") is None and not run.get("receipts")
            and "added" not in run and ": test selection in " in str(run.get("problem") or "")):
        return tuple(run.get("changed") or ())
    return None

"""What a task is allowed to touch, and how far it may go.

The 2026-09-13 saved-filter trial recorded the failure this module exists to
catch: a worker handed a two-line fixture task went on to implement the rest
of the goal's interface and regression tests, hundreds of lines across files
nobody had named. Nothing was broken in a way the harness could see. The gate
passed, the close-out read well, and the actual delivered change bore no
relation to the task that was authorised.

That is not a model failure that better prompting fixes. A task with no stated
bounds has no expansion to detect, because there is nothing for the observed
change to disagree with. So a :class:`TaskScope` states four things up front:

* **permitted paths** -- where edits may land, as glob patterns.
* **intended result** -- one sentence a person can check the diff against.
* **acceptance criteria** -- how the task knows it is finished.
* **change bounds** -- roughly how large the change should be.

and :meth:`TaskScope.assess` compares them to what actually changed.

Two rules govern the response, and both come from the trial:

**Expansion is reported, never reverted.** The trial's over-wide change was
also the only work that existed; discarding it would have destroyed real
output to satisfy a bookkeeping rule. :class:`ScopeReport` is evidence for the
lead and the record, and :attr:`ScopeReport.blocking` marks only the case the
operator actually forbade -- a write outside the permitted paths. Size is
advisory (operator directive 2026-10-20): :attr:`ScopeReport.oversized`
marks a change well past the planner's estimate for the record, the
reviewers and the planner, and the dispatcher stops nothing for it. Until
then a 50 percent tolerance was a hard stop; that stop is retired.

**The estimate is advisory to the model and measured by the harness.**
Telling a model its expected size helps a little; measuring the diff
afterwards and showing the measurement to the reviewers is what makes the
figure mean something. Both happen -- :meth:`render` goes into the prompt,
:meth:`assess` runs against the diff, and the reviewer judges whether the
size was necessary.
"""

from __future__ import annotations

import ast
import fnmatch
import json
import re
from dataclasses import dataclass, field
from pathlib import PurePosixPath
from typing import Dict, List, Optional, Sequence, Tuple

__all__ = ["TaskScope", "ScopeReport", "changed_paths", "count_change_lines",
           "count_change_lines_by_path", "is_test_path", "declared_signatures",
           "lint_declaration"]

#: How far past the planner's estimate (``max_lines``) a change may run
#: before the report says so. A measurement threshold for the record, the
#: reviewer and the planner's next estimate, never a stop: a task sized at
#: 100 lines landing at 130 is normal, and one landing at 400 is either an
#: estimate that missed or a task that grew, which only a reader of the
#: change can tell (operator directive 2026-10-20: size is advisory).
_OVERRUN_TOLERANCE = 1.5


@dataclass(frozen=True)
class ScopeReport:
    """What a task actually did, against what it said it would.

    ``blocking`` is deliberately narrow. Out-of-scope *paths* block: the
    operator named where edits may land, and a write elsewhere is the grant
    being exceeded; the editing dispatcher stops on it and preserves the
    partial work. ``oversized`` is size telemetry: the change ran well past
    the planner's estimate. It stops nothing, buys no call and rejects no
    work; it is recorded, shown to the lead, the reviewers and the planner,
    and the reviewer judges whether the size is what correctness and the
    task's proofs need. Acceptance criteria are still checked by the task's
    reviews and gates.
    """

    within_scope: bool
    changed: List[str] = field(default_factory=list)
    out_of_scope: List[str] = field(default_factory=list)
    changed_lines: int = 0
    max_lines: Optional[int] = None
    notes: List[str] = field(default_factory=list)
    #: ``changed_lines`` split by whether the path is a test file. The
    #: estimate is a single figure and tests count in full against it; the
    #: split is reported so the next estimate can be calibrated against what
    #: actually happened, and so a reviewer sees where the size went.
    code_lines: int = 0
    test_lines: int = 0
    overrun_ratio: float = _OVERRUN_TOLERANCE

    @property
    def blocking(self) -> bool:
        return bool(self.out_of_scope)

    @property
    def oversized(self) -> bool:
        return (
            self.max_lines is not None
            and self.changed_lines > self.max_lines * self.overrun_ratio
        )

    def render(self) -> str:
        if self.within_scope and not self.oversized and not self.notes:
            return (
                f"Scope check passed: {len(self.changed)} file(s), "
                f"{self.changed_lines} changed line(s)."
            )
        lines = ["Scope check:"]
        if self.out_of_scope:
            lines.append(
                "  Outside the permitted paths for this task: "
                + ", ".join(sorted(self.out_of_scope))
            )
        if self.oversized:
            # A measurement, not a diagnosis. The overrun may be an estimate
            # that missed (attempt 2 of the blind acceptance: a validator plus
            # ten named test scenarios sized at 100 lines landed at 223, all
            # inside the slice) or a task growing into the whole feature; the
            # harness cannot tell which, so it reports the split for the
            # reviewer and the planner and stops nothing.
            lines.append(
                f"  {self.changed_lines} changed lines ({self.code_lines} in "
                f"code, {self.test_lines} in tests) against the planner's estimate of "
                f"~{self.max_lines}. Tests count in full. This is a measurement for the "
                f"reviewer and the planner, not a stop: say what the estimate missed, "
                f"or what grew."
            )
        for note in self.notes:
            lines.append(f"  {note}")
        if self.changed:
            lines.append("  Changed: " + ", ".join(sorted(self.changed)))
        return "\n".join(lines)


@dataclass
class TaskScope:
    """The bounds a task declares before any model is invoked.

    Attributes:
        permitted_paths: Globs, matched against project-relative POSIX paths.
            Empty means unrestricted, which is recorded as such rather than
            silently treated as "everything is fine".
        intended_result: One sentence describing the finished state.
        acceptance: How the task knows it is done. Rendered into the prompt
            and carried into the close-out.
        max_lines: The planner's estimate of changed lines (advisory: measured
            and reported, never a stop).
        forbidden_paths: Globs that are never editable regardless of
            ``permitted_paths`` -- the operator's explicit exclusions.
    """

    permitted_paths: Sequence[str] = ()
    intended_result: str = ""
    acceptance: Sequence[str] = ()
    max_lines: Optional[int] = None
    forbidden_paths: Sequence[str] = ()
    overrun_ratio: float = _OVERRUN_TOLERANCE
    #: Declared review-only intent (SCOPE ``"edits": "none"``): the task
    #: reports and does not repair. Never inferred from a small max_lines, since
    #: a one-line fix is still editing work (Codex review of 3d5c3f3), and
    #: enforced by :meth:`assess`: any source change is out of scope.
    review_only: bool = False
    #: The page and interaction the harness captures for this task when the
    #: operator declared a capture profile (SCOPE ``"capture"``): ``{"path",
    #: "steps"}``, validated syntactically at parse; see quadratus.preview.
    capture: Optional[Dict[str, object]] = None
    #: Exact-file binding (direct tier, Codex on #42): ``permitted_paths`` are
    #: file identities, not patterns or prefixes. A path permits only itself,
    #: so a declared file that becomes a directory puts every descendant out
    #: of scope. Set by the harness at dispatch, never declared by a task.
    exact: bool = False
    #: The planner's own split of ``max_lines`` (SCOPE ``"estimate"``):
    #: ``{"implementation", "proof", "setup"}``, exact non-negative integers
    #: summing to ``max_lines``, so a task is sized for its implementation
    #: and the executed proof its COVERS promise together. Rendered to the
    #: lead and kept on the record for calibration; every line counts in
    #: full against the one estimate, and the split proves nothing about
    #: what the proof covers. None when not declared (legacy declarations):
    #: unknown, not zero.
    estimate: Optional[Dict[str, int]] = None
    #: The planner's proof scenarios (SCOPE ``"proofs"``, ``read_proofs``):
    #: each a stable id, the requirement ids it shows, the operation, the
    #: starting state, the expected behaviour or state, and the intended test
    #: location. Each becomes an evidence obligation at dispatch that only a
    #: named executed case the lead binds (``PROVES: P<n> - file::case``) can
    #: discharge. None when not declared (legacy declarations): the mapping is
    #: unknown, never read as complete.
    proofs: Optional[Tuple[Dict[str, object], ...]] = None

    def permits(self, path: str) -> bool:
        """Whether ``path`` may be edited under this scope."""
        path = str(path).removeprefix("./")
        if any(_matches(path, pattern) for pattern in self.forbidden_paths):
            return False
        if self.exact:
            return path in {str(p).removeprefix("./") for p in self.permitted_paths}
        if not self.permitted_paths:
            return True
        return any(_matches(path, pattern) for pattern in self.permitted_paths)

    def assess(self, diff: str) -> ScopeReport:
        """Compare an actual diff against what this task said it would do."""
        changed = changed_paths(diff)
        lines = count_change_lines(diff)
        by_path = count_change_lines_by_path(diff)
        test_lines = sum(n for path, n in by_path.items() if is_test_path(path))
        # A declared review-only task changes no source at all: every changed
        # path is out of scope (Codex review of 9a31aac: edits:none was prompt
        # only). Harness state such as capture fixtures is not source, so it
        # never reaches this diff. An empty diff passes as before.
        out = sorted(changed if self.review_only else (p for p in changed if not self.permits(p)))
        notes: List[str] = []
        if self.review_only and changed:
            notes.append("This task declared no source edits (edits: none).")
        if not self.permitted_paths and not self.forbidden_paths:
            notes.append(
                "No permitted paths were declared for this task, so no path "
                "check was possible."
            )
        report = ScopeReport(
            within_scope=not out,
            changed=sorted(changed),
            out_of_scope=out,
            changed_lines=lines,
            max_lines=self.max_lines,
            notes=notes,
            code_lines=lines - test_lines,
            test_lines=test_lines,
            overrun_ratio=self.overrun_ratio,
        )
        return report

    def render(self, *, include_forbidden: bool = True) -> str:
        """The scope as a prompt block, so the bound is stated before the work.

        Omitting exclusions is for measuring the reference budget only. The
        packet sent to a model still carries every exclusion, and permits()
        always enforces the full list.
        """
        parts = ["## Scope of this task"]
        if self.intended_result:
            parts.append(f"Intended result: {self.intended_result}")
        if self.permitted_paths:
            parts.append(
                "You may change only these paths: "
                + ", ".join(self.permitted_paths)
                + ". A change anywhere else will be rejected as out of scope."
            )
        if self.forbidden_paths and include_forbidden:
            parts.append("Never change: " + ", ".join(self.forbidden_paths) + ".")
        if self.acceptance:
            parts.append(
                "This task is finished when:\n"
                + "\n".join(f"  - {a}" for a in self.acceptance)
            )
        if self.review_only:
            parts.append("Declared review-only: report findings; do not change project source.")
        if self.proofs:
            parts.append("Proof scenarios (a test scenario is shown only by a named test case that runs and passes. "
                         + PROVES_RULE + " "
                         "A scenario marked 'evidence: reading' is a documentation claim the audit reads and cites; "
                         "one marked 'evidence: capture' is a visual state the harness renders and the design "
                         "reviewer judges; neither takes a PROVES line):\n"
                         + "\n".join(f"  - {p['id']} [{', '.join(p['requirements'])}] {p['operation']}; given "
                                      f"{p['state']}; expect {p['expect']}; in {p['location']}"
                                      + (f"; evidence: {p['evidence']}" if p.get("evidence") in ("reading", "capture") else "")
                                      for p in self.proofs))
        if self.estimate:
            parts.append("The planner's split of that size: "
                         + ", ".join(f"{k} {v}" for k, v in self.estimate.items())
                         + " lines. The estimate below is the sum; every line counts the same.")
        if self.max_lines:
            # The lead is told what the harness measures and what it does
            # with it (series rule-2ffa7f6 f3, f4 and f7: first tasks of 85
            # to 100 lines landed at 135 to 236, with 112 to 171 of them in
            # tests the lead wrote against a bound it read as code). The
            # size is advisory (operator directive 2026-10-20): correctness
            # and complete requirements first, the tests that prove them,
            # then conciseness; the measured size reaches the reviewer, who
            # judges whether it was necessary.
            parts.append(
                f"Expected size: about {self.max_lines} changed lines, counting added and removed "
                f"lines in every file, tests in full. This is the planner's estimate, not a limit: "
                f"the harness measures the diff after each editing call and reports the size to "
                f"you, to the reviewers and to the planner, and it stops nothing for size alone. Do "
                f"what this task and its proof scenarios need, completely and correctly, with the "
                f"tests that show it; if that needs substantially more than the estimate, deliver it "
                f"and say what the estimate missed. Keep the change focused on this task: no "
                f"unrelated edits, no duplication, no padding. Never compress formatting, squeeze "
                f"tests onto one line or leave a test out to fit a number; a reviewer judges "
                f"whether the size was necessary, and length alone is not a finding."
            )
        return "\n".join(parts)

    @classmethod
    def unbounded(cls) -> "TaskScope":
        """An explicit 'nobody scoped this', which reads better than None."""
        return cls()

    def to_dict(self) -> Dict[str, object]:
        return {
            "permitted_paths": list(self.permitted_paths),
            "forbidden_paths": list(self.forbidden_paths),
            "intended_result": self.intended_result,
            "acceptance": list(self.acceptance),
            "max_lines": self.max_lines,
            **({"edits": "none"} if self.review_only else {}),
            **({"capture": self.capture} if self.capture else {}),
            **({"exact": True} if self.exact else {}),
            **({"estimate": dict(self.estimate)} if self.estimate else {}),
            **({"proofs": [dict(p) for p in self.proofs]} if self.proofs is not None else {}),
        }


def _matches(path: str, pattern: str) -> bool:
    """Glob match that also treats a bare directory prefix as covering it.

    ``docs`` and ``docs/`` both cover ``docs/BULK_TAGGING.md``; writing
    ``docs/**`` for every directory would be noise, and getting it wrong would
    silently widen or narrow the grant.
    """
    pattern = str(pattern).removeprefix("./")
    if fnmatch.fnmatch(path, pattern):
        return True
    prefix = pattern.rstrip("/")
    if prefix and not any(ch in prefix for ch in "*?["):
        return path == prefix or path.startswith(prefix + "/")
    # Also accept a double-star directory wildcard matching zero levels.
    if "**" in pattern:
        return fnmatch.fnmatch(path, pattern.replace("**/", "*").replace("**", "*"))
    return False


def changed_paths(diff: str) -> List[str]:
    """Project-relative paths named by a unified diff, deduplicated.

    Reads ``+++``/``---`` headers rather than ``diff --git`` lines, because
    :meth:`quadratus.project.Project.diff` emits the former and not always the
    latter. ``/dev/null`` is skipped; an added file is named by its ``+++``
    side and a deleted one by its ``---`` side.
    """
    seen: List[str] = []
    for line in (diff or "").splitlines():
        if line.startswith(("Binary file changed: ", "Empty file added/deleted: ")):
            name = line.partition(": ")[2]
            if name not in seen:
                seen.append(name)
            continue
        if not (line.startswith("--- ") or line.startswith("+++ ")):
            continue
        raw = line[4:].strip().split("\t")[0]
        if raw == "/dev/null" or not raw:
            continue
        if raw.startswith(("a/", "b/")):
            raw = raw[2:]
        if raw not in seen:
            seen.append(raw)
    return seen


def count_change_lines(diff: str) -> int:
    """Added plus removed lines, excluding diff headers.

    A moved line counts twice, which overstates a pure reshuffle. Accepted:
    the number is a size signal, and a reshuffle large enough to matter is
    itself worth a look.
    """
    total = 0
    for line in (diff or "").splitlines():
        if line.startswith(("+++", "---", "@@", "diff --git", "index ")):
            continue
        if line.startswith(("+", "-")):
            total += 1
    return total


def count_change_lines_by_path(diff: str) -> Dict[str, int]:
    """:func:`count_change_lines`, attributed to the file each hunk belongs to.

    Only ``+++``/``---`` headers name files (see :func:`changed_paths`), so a
    hunk is attributed to the most recent header. Lines before any header
    are counted under ``""``; a diff that lacks headers still sums correctly.
    """
    counts: Dict[str, int] = {}
    current = ""
    for line in (diff or "").splitlines():
        if line.startswith(("--- ", "+++ ")):
            raw = line[4:].strip().split("\t")[0]
            if raw != "/dev/null" and raw:
                current = raw[2:] if raw.startswith(("a/", "b/")) else raw
            continue
        if line.startswith(("@@", "diff --git", "index ")):
            continue
        if line.startswith(("+", "-")):
            counts[current] = counts.get(current, 0) + 1
    return counts


_TEST_DIRS = frozenset({"test", "tests", "spec", "specs", "__tests__"})
_TEST_NAMES = re.compile(
    r"^(test_.*|.*_test|.*\.test|.*\.spec|.*_spec|conftest)\.[A-Za-z0-9]+$"
)


#: How a proof scenario is bound to its executed cases, stated once for
#: every seat that writes or judges a delivery (the lead's packet, the
#: revision prompt, the reviewer's proof rows). The settings-migration run
#: (2026-10-21): the lead's P11 first named a test class, then a
#: comma-separated case list; the harness rightly rejected both, the
#: reviewer asked for source revisions, and the second revision was spent
#: on a format the prompt had never spelled out. The grammar is unchanged
#: and strict; the rule now says it, and the measured reason comes back.
PROVES_RULE = (
    "Bind each proof scenario with one line per named test case, exactly "
    "'PROVES: P<n> - path/test_x.py::Class::test_case' (the Class segment only when the case "
    "is in a class; a parametrized id in brackets is part of the case), one case per line and "
    "nothing else on the line. A scenario shown by several cases takes several PROVES lines, "
    "repeated with the same P<n>. A line that names a test class, a test file, or a "
    "comma-separated list binds nothing and invalidates that scenario's declaration; the "
    "measured reason is returned to you, and the correction is the corrected line(s)."
)

#: What a delivered test must do, stated for the author and the judge
#: alike. The same run's independent source finding: a generated
#: standard-library-only test forbade every module import and held a loop
#: that asserted nothing, which the next task's json import could not
#: satisfy. Generic by design: the requirement decides what is asserted.
TEST_SUBSTANCE_RULE = (
    "A test proves the requirement its scenario names: it performs the stated operation from "
    "the stated prior state and asserts the stated result or resulting state, so that it fails "
    "when that behaviour is absent. A test with no assertion, a loop whose body asserts nothing, "
    "or an assertion that cannot fail proves nothing. Assert only what the requirement states and "
    "nothing it does not: a 'standard library only' requirement permits every standard-library "
    "import (json, sqlite3, http.server, threading and the rest) and forbids third-party packages, "
    "so a test never forbids imports, files or constructs that the goal's later work may validly "
    "use. A test that would fail valid future work on this goal is a defect in the test. An assertion "
    "that accepts more than one outcome where the goal names one proves neither; where the goal is "
    "ambiguous, the reading is decided first and the test asserts that one."
)


def is_test_path(path: str) -> bool:
    """Whether a project-relative path is a test file by common convention.

    A directory named ``tests``, ``test``, ``spec`` or ``__tests__`` anywhere
    in the path, or a basename such as ``test_x.py``, ``x_test.go`` or
    ``x.spec.ts``. Conventions only: a project with tests elsewhere gets them
    counted as code, which errs toward the stricter reading.
    """
    parts = PurePosixPath(str(path).lstrip("./")).parts
    if not parts:
        return False
    if any(part in _TEST_DIRS for part in parts[:-1]):
        return True
    return bool(_TEST_NAMES.match(parts[-1]))


_DEFINED = re.compile(r"\bdef\s+([A-Za-z_][\w.]*)\s*\(")
_DEFINED_WITH_ARGS = re.compile(r"\bdef\s+([A-Za-z_][\w.]*)\s*\(([^()]*)\)")
_ELIDED = frozenset({"...", "\u2026"})
_QUOTED = re.compile(r"`\s*(def\s+)?([A-Za-z_][\w.]*)\s*\(([^()`]*)\)\s*:?\s*`")
#: The anchored assertion grammar (cycle19): a backticked ``name(args)``
#: without ``def`` states a signature only when, on its own line, one of these
#: phrases ends directly before its opening backtick (an optional colon
#: between): "signature" or "signature is", "define", "defines", "defined as",
#: "declare", "declares", "declared as", "make it". These are the forms the
#: historical regressions assert with (attempt 2: "give the function
#: signature `...`"; acceptance: "app.py defines `...`"; f6: "actually make it
#: `...`"). Every other backticked ``name(args)`` is an invocation or an
#: example and never a signature, however the sentence around it reads.
_ASSERTED = re.compile(r"(?:\bsignature(?:\s+is)?|\bdefines?|\bdefined\s+as|\bdeclares?|\bdeclared\s+as"
                       r"|\bmake\s+it)\s*:?\s*$", re.IGNORECASE)
#: How far before a quoted form the assertion grammar looks, on its line.
_ASSERTED_WINDOW = 40
#: A parameter list longer than this is not read as one (bounded parsing).
_MAX_PARAMETERS_CHARS = 400


def _parameter_list(args: str) -> bool:
    """Whether ``args`` reads as a Python parameter list, by static parsing
    only (``ast``; nothing is executed or imported). A placeholder such as
    ``<argv list>`` or an elision is not one and states no signature."""
    if len(args) > _MAX_PARAMETERS_CHARS:
        return False
    try:
        ast.parse(f"def _f({args}):\n    pass\n")
    except (SyntaxError, ValueError):
        return False
    return True


def _asserted(text: str, start: int) -> bool:
    """Whether the assertion grammar ends directly before ``start`` on its
    line (``_ASSERTED``)."""
    line = text[:start].rsplit("\n", 1)[-1]
    return bool(_ASSERTED.search(line[-_ASSERTED_WINDOW:]))


def declared_signatures(description: str, *fields: Sequence[str]) -> Dict[str, List[str]]:
    """Every signature the declaration gives each function it defines.

    A function is *declared* where the description, the intended result or
    an acceptance condition writes ``def name(``. For those names a signature
    is (cycle19, PR73 6745520095):

    - a definition: every ``def name(args)``, in a code block or in
      backticks, in the description or the fields; or
    - an asserted signature: a backticked ``name(args)`` that the anchored
      assertion grammar (``_ASSERTED``) directly precedes on its line.

    Any other backticked ``name(args)`` is an invocation or an example (a
    test calling ``main(argv)``, an expected ``TypeError`` from ``f(1)``) and
    is never collected: cycle18 stalled because ``main(argv)`` in test
    guidance read as a second signature beside ``def main(argv=None)``.
    Signatures are compared as written, whitespace normalised, so a rename or
    a changed default in a real assertion is a difference even where the two
    would bind the same call. A form whose parentheses do not read as a
    parameter list (an elision such as ``(...)``, a placeholder) states no
    signature and is skipped (f6-download-guard on 2ffa7f6). Incidental calls
    (``float()``, ``len(row)``) and backticked names that are not functions
    (attempt 3's column headings ``Start (s)``) are never declared, since
    only a ``def`` declares a name.

    Limits: names are matched as written, with no class or module
    resolution, so two methods of one name in different classes are one
    name, as before; a qualified invocation (``store.add(x)``) is not the
    declared name and is never collected; and a form with nested
    parentheses (a default of ``()``) is not read, as before.

    Returns ``{name: [distinct signatures, in order of appearance]}``. More
    than one entry for a name is the contradiction the decomposition prompt
    forbids: attempt 2 of the blind acceptance wrote
    ``def _validate_clip_row(project, cols, row)`` in a code block, revised it
    mid-description to ``(project, cols, width, row)``, and emitted acceptance
    from the first draft. The lead implemented one and was measured against
    the other.
    """
    texts = [description, *[x for group in fields for x in group]]
    declared = {name for text in texts for name in _DEFINED.findall(text)}
    if not declared:
        return {}
    found: Dict[str, List[str]] = {}

    def note(name: str, args: str) -> None:
        if name not in declared:
            return
        parts = [a.strip() for a in args.split(",") if a.strip()]
        if parts and all(a in _ELIDED for a in parts):
            # ``_serve_guarded(...)`` is a reference to the function, not a
            # signature: f6-download-guard on 2ffa7f6 stalled because the
            # description wrote ``def _serve_guarded(directory, name)`` and
            # later said each route "calls `_serve_guarded(...)`", and this
            # check reported the ellipsis as a second signature. The
            # correction then named ``(...)`` as the contradiction, which the
            # orchestrator could not act on. An elision says nothing about
            # the parameters, so it is not collected.
            return
        if not _parameter_list(args):
            return
        normalised = ", ".join(parts)
        forms = found.setdefault(name, [])
        if normalised not in forms:
            forms.append(normalised)

    for text in texts:
        for name, args in _DEFINED_WITH_ARGS.findall(text):
            note(name, args)
        for match in _QUOTED.finditer(text):
            if match.group(1) or _asserted(text, match.start()):
                note(match.group(2), match.group(3))
    return found



def lint_declaration(description: str, scope: "TaskScope") -> None:
    """Deterministic checks a decomposition must pass before dispatch.

    Raises :class:`ValueError` with a message the correction round can quote.
    The prompt asks for a final description with one signature per function,
    quoted verbatim in acceptance; this is the backstop for when the
    orchestrator thinks aloud inside the description instead. Only
    definitions and asserted signatures are compared (``declared_signatures``);
    the message says so, so the one correction is not spent rewriting call
    examples (cycle19).
    """
    forms = declared_signatures(description, [scope.intended_result], scope.acceptance)
    for name, variants in forms.items():
        if len(variants) > 1:
            listed = "; ".join(f"{name}({v})" for v in variants)
            raise ValueError(
                f"the declaration gives {name} more than one signature ({listed}); "
                f"write the description as final text with exactly one signature "
                f"per function and quote it verbatim in acceptance (a signature is a "
                f"`def {name}(<parameters>)` form or a form written directly after 'signature', "
                f"'defines' or 'make it'; a quoted call such as a test's invocation is "
                f"an example, never a signature)"
            )


PROOF_KEYS = ("id", "requirements", "operation", "state", "expect", "location")
#: Supported evidence modalities a scenario may declare: ``test`` (the
#: default: a named test case that runs and passes), ``reading`` (a
#: documentation file the audit reads and cites; refused at dispatch for a
#: behavioural location or an error or refusal requirement) or ``capture``
#: (a visual state the harness renders and the design reviewer judges;
#: admitted only for design work with design evidence in force).
PROOF_EVIDENCE = ("test", "reading", "capture")
_PROOF_ID = re.compile(r"^P[1-9]\d*$")
_REQUIREMENT_ID = re.compile(r"^R[1-9]\d*$")


def read_proofs(data) -> Tuple[Dict[str, object], ...]:
    """The optional SCOPE ``proofs`` list, strictly: a non-empty list of
    objects with exactly id, requirements, operation, state, expect and
    location; ids ``P<n>`` and unique; requirements a non-empty list of
    ``R<n>`` ids; the texts non-empty strings; the location a narrow
    project-relative path. Anything else is refused rather than read
    partially. Omitted is unknown and allowed."""
    if not isinstance(data, list) or not data:
        raise ValueError("SCOPE proofs must be a non-empty list of scenario objects")
    seen = set()
    proofs = []
    for item in data:
        if not isinstance(item, dict) or not set(PROOF_KEYS) <= set(item) or not set(item) <= set(PROOF_KEYS) | {"evidence"}:
            raise ValueError("SCOPE proofs items must be objects with exactly id, requirements, operation, "
                             "state, expect and location (and optionally evidence)")
        evidence = item.get("evidence", "test")
        if evidence not in PROOF_EVIDENCE:
            raise ValueError(f"SCOPE proof {item.get('id')} evidence must be one of {', '.join(PROOF_EVIDENCE)}")
        pid = item["id"]
        if not isinstance(pid, str) or not _PROOF_ID.match(pid) or pid in seen:
            raise ValueError("SCOPE proofs ids must be unique P<n> ids")
        seen.add(pid)
        rids = item["requirements"]
        if (not isinstance(rids, list) or not rids
                or any(not isinstance(r, str) or not _REQUIREMENT_ID.match(r) for r in rids)
                or len(set(rids)) != len(rids)):
            raise ValueError(f"SCOPE proof {pid} must name a non-empty list of distinct R<n> requirement ids")
        for key in ("operation", "state", "expect", "location"):
            if not isinstance(item[key], str) or not item[key].strip():
                raise ValueError(f"SCOPE proof {pid} needs a non-empty {key}")
        location = item["location"]
        if (location != location.strip() or PurePosixPath(location).is_absolute()
                or ".." in PurePosixPath(location).parts or "\\" in location
                or location.startswith(("~", ".quadratus")) or location.strip("./*?[]") == ""):
            raise ValueError(f"SCOPE proof {pid} location must be a narrow project-relative path")
        proofs.append(dict(id=pid, requirements=list(rids), operation=item["operation"].strip(),
                           state=item["state"].strip(), expect=item["expect"].strip(), location=location,
                           evidence=evidence))
    return tuple(proofs)


ESTIMATE_KEYS = ("implementation", "proof", "setup")


def read_estimate(data, max_lines: int) -> Dict[str, int]:
    """The optional SCOPE ``estimate`` object, strictly: exactly the keys
    implementation, proof and setup, each an exact non-negative int (a bool
    is not one), summing to ``max_lines``. Anything else is refused rather
    than read as a lower figure. Omitted is unknown and allowed."""
    if not isinstance(data, dict) or set(data) != set(ESTIMATE_KEYS):
        raise ValueError("SCOPE estimate must be an object with exactly implementation, proof and setup")
    for key in ESTIMATE_KEYS:
        value = data[key]
        if type(value) is not int or value < 0:
            raise ValueError(f"SCOPE estimate {key} must be a non-negative integer")
    if sum(data[key] for key in ESTIMATE_KEYS) != max_lines:
        raise ValueError("SCOPE estimate implementation + proof + setup must equal max_lines")
    return {key: int(data[key]) for key in ESTIMATE_KEYS}


def read_scope(description: str):
    """Require a structured declaration before normal project dispatch.
    ``max_lines`` is the planner's size estimate, any positive integer: a
    plan is never refused for its size (operator directive 2026-10-20)."""
    matches = list(re.finditer(r"^SCOPE:\s*(.+)$", description, re.MULTILINE))
    if len(matches) != 1:
        raise ValueError("Declare exactly one SCOPE JSON line")
    try:
        data = json.loads(matches[0].group(1))
    except ValueError as exc:
        raise ValueError("SCOPE must contain valid JSON") from exc
    if not isinstance(data, dict):
        raise ValueError("SCOPE must be an object")
    paths = data.get("permitted_paths")
    acceptance = data.get("acceptance")
    result = data.get("intended_result")
    bound = data.get("max_lines")
    if not isinstance(paths, list) or not paths:
        raise ValueError("SCOPE needs permitted_paths")
    for name in paths:
        if (not isinstance(name, str) or not name.strip() or name != name.strip()
                or PurePosixPath(name).is_absolute() or ".." in PurePosixPath(name).parts
                or "\\" in name or name.startswith(("~", ".quadratus"))
                or name.strip("./*?[]") == ""):
            raise ValueError("SCOPE paths must be narrow project-relative paths")
    if not isinstance(result, str) or not result.strip():
        raise ValueError("SCOPE needs an intended_result")
    if (not isinstance(acceptance, list) or not acceptance
            or any(not isinstance(x, str) or not x.strip() for x in acceptance)):
        raise ValueError("SCOPE needs verifiable acceptance conditions")
    if type(bound) is not int or bound <= 0:
        raise ValueError("SCOPE max_lines must be a positive integer (the planner's size estimate)")
    edits = data.get("edits", "allowed")
    if edits not in ("allowed", "none"):
        raise ValueError('SCOPE edits must be "none" (review only) or omitted')
    capture = None
    if "capture" in data:
        from .preview import validate_capture
        capture = validate_capture(data["capture"])
    estimate = read_estimate(data["estimate"], bound) if "estimate" in data else None
    proofs = read_proofs(data["proofs"]) if "proofs" in data else None
    body = (description[:matches[0].start()] + description[matches[0].end():]).strip()
    scope = TaskScope(paths, result, acceptance, bound, review_only=edits == "none", capture=capture,
                      estimate=estimate, proofs=proofs)
    lint_declaration(body, scope)
    return scope, body

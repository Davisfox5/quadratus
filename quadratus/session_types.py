"""What the parts of the session engine share: the stops a run raises, the
task record, and the questions asked of one task.

Moved out of session.py unchanged on 2026-11-11. quadratus.session imports
every name here again, so ``from quadratus.session import TaskSpec`` still
works and names the same object. SessionConfig stays in session.py.
The move was redone on 2026-11-12, after the development line was merged
into this file, and took that line's code of the same kind with it.
"""
from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from functools import wraps
from pathlib import PurePosixPath
from typing import List, Optional

from .delegation import invocation
from .providers import PermissionBlocked, ProviderError, ProviderRefusal
from .routing import WorkClass
from .scope import TaskScope
from .task_kinds import KNOWN_NEEDS, TaskKind, needs_from_text, normalise_needs


class TaskListInvalid(RuntimeError):
    """An operator-listed task (``Session.run(tasks=...)``) failed a rule the
    orchestrator's reply would have been sent back for: no label, an invalid
    scope, a bad COVERS or RESOLVES, a missing capture, or a control reply
    in place of a task. There is nobody to send it back to, so the run stops
    before any lead call on that task, and the operator corrects the list.
    """


class OperatorInputNeeded(RuntimeError):
    """The orchestrator asked a question only the operator can answer.

    Raised when no ``ask_operator`` channel is configured. Deliberately not
    swallowed: a system that guesses the answer to a question it explicitly
    flagged as operator-only has defeated the point of asking. The question
    is in ``args[0]``; answer it, add it to the session's rulings, and resume.
    """


class PartialWorkStopped(RuntimeError):
    """An editing call stopped and the tree was inspected before giving up.

    Carries :attr:`partial` -- what was already written, as a JSON-shaped dict
    -- so the run report can hand an honest, resumable picture to whoever picks
    this up: which files moved, how much, and whether inspection was even
    possible. Nothing has been rolled back.
    """

    def __init__(self, message: str, *, partial: Optional[dict] = None) -> None:
        super().__init__(message)
        self.partial = partial or {}

    def render(self) -> str:
        changed = self.partial.get("changed") or []
        lines = [str(self)]
        if changed:
            lines.append(
                f"Already written when the call stopped ({len(changed)} file(s), "
                f"{self.partial.get('changed_lines', 0)} line(s)), and preserved: "
                + ", ".join(changed[:20])
            )
        note = self.partial.get("note")
        if note:
            lines.append(note)
        return "\n".join(lines)


#: What a task can fail on by itself (``TaskFailed.cause``).
TASK_FAILURE_CAUSES = ("scope", "reply", "channel", "transport", "environment")


class TaskFailed(PartialWorkStopped):
    """One task broke its own rules; the task fails, the run goes on.

    Raised where a lead's work or reply broke the task it was doing (the
    measured edits exceed the declared scope, a reply that is neither a
    request nor a delivery, a request channel that did not converge, a
    transport stop after writes) and caught by the task runner, which keeps
    the edits, records the cause on the task and hands the remaining work
    back to the orchestrator exactly as a capped task is. Two live runs
    (2026-09-28) ended on the first such failure, so every later task's
    faults stayed unseen; a task-level failure lets one run show several.

    Never raised for what the harness cannot inspect, for a dependency or
    evidence integrity stop, a refusal, a denied capability, a spent budget,
    an operator question or the orchestrator's own stalls: those still stop
    the run, because no re-plan repairs them.
    """

    def __init__(self, cause: str, message: str, *, partial: Optional[dict] = None) -> None:
        assert cause in TASK_FAILURE_CAUSES, cause
        super().__init__(message, partial=partial)
        self.cause = cause


class PreviewUnavailable(RuntimeError):
    """The operator's preview failed where the harness can prove the cause is
    the environment, not the project (map E1; Codex, 5862699144): it was
    never launched because the port was taken, or its launch itself failed.
    An operator handoff: no repair call, nothing recaptured. A preview that
    ran and then exited, or was never ready, is not this."""


class CapabilityUnavailable(RuntimeError):
    """The seat a task needs cannot perform a capability the harness requires.

    Codex, Run 18: a UI lead was told to start a preview and capture, which
    its transport would never let it run; both leads capped on denials.
    Raised before the lead is invoked when that is known from the transport,
    or after the invocation when it was denied a command the harness itself
    declared. Nothing is granted, replayed or rerouted: the operator declares
    a capture profile or a check. Distinct from a model's refusal.
    """


#: A shell or tool's own refusal line ("zsh:1: command not found: pytest",
#: "cat: app.py: Permission denied"), never a test's report of an error.
_ENVIRONMENT_TEXT = re.compile(r"^(?:[\w./-]+:\s*)+(?:command not found|permission denied|operation not permitted)",
                               re.IGNORECASE | re.MULTILINE)


def _environment_signature(failures) -> tuple:
    """The environmental part of a call's tool failures, order-free:
    (command, exit) for each command the environment could not run."""
    found = set()
    for failure in failures or []:
        if not isinstance(failure, dict):
            continue
        code = failure.get("exit_code")
        text = str(failure.get("output_tail") or failure.get("message") or "")
        if (code in (126, 127) or failure.get("status") == "permission_denied"
                or _ENVIRONMENT_TEXT.search(text)):
            found.add((str(failure.get("command") or failure.get("type") or "tool"), code))
    return tuple(sorted(found, key=repr))


class InfrastructureUnavailable(CapabilityUnavailable):
    """A call's tools could not run in this execution environment (a vendor
    sandbox that cannot start inside the operator's, for one). The cause is
    the environment, so the run stops at once with it quoted; no review,
    revision or retry is spent on code that was never the problem (step 1,
    2026-10-13: six calls followed a lead whose every command failed
    "sandbox_apply", status 71, before any file was read)."""


class PlanningUnaffordable(RuntimeError):
    """A planner re-ask inside one planning decision (FETCH, evidence, an
    operator answer, a correction) that would leave no payable continuation
    (``Session._reask_refusal``, cycle03). Raised before the re-ask, so no
    call is bought to discover the refusal; the planning loop records it as
    the existing route forfeit and the cap's decision applies."""


class RunStalled(RuntimeError):
    """The orchestrator named the same task twice in a row.

    Two identical consecutive decompositions mean the ledger is not moving the
    orchestrator's view forward -- the loop has stopped converging and every
    further round would spend the subscription window re-running a known
    outcome. Raised rather than silently continued or silently stopped: the
    operator should see that the run stalled and why, because the fix (a
    better close-out, a narrower goal, a fetched artifact) is theirs to pick.
    """


class Complexity:
    """How hard a task is. Drives two decisions at once.

    First, who leads: difficulty maps onto the brain trust as a ladder (see
    :data:`quadratus.task_kinds.DIFFICULTY_LADDER`), so the hardest work gets
    the strongest model and the bulk of the work lands on the subscriptions
    with capacity to spare. Second, how many collaborators the task draws:
    each one is another full invocation and another voice in the lead's
    working memory, so easy work is not made expensive by ceremony.
    """

    ROTE = "rote"          # mechanical; lead alone
    SIMPLE = "simple"      # the bulk of well-sized tasks; lead alone
    STANDARD = "standard"  # lead + one collaborator
    COMPLEX = "complex"    # many logical steps; lead + the whole brain trust

    _COLLABORATORS = {ROTE: 0, SIMPLE: 0, STANDARD: 1, COMPLEX: None}

    @classmethod
    def collaborator_count(cls, complexity: str, available: int) -> int:
        if complexity not in cls._COLLABORATORS:
            complexity = cls.STANDARD
        wanted = cls._COLLABORATORS[complexity]
        return available if wanted is None else min(wanted, available)


@dataclass
class TaskSpec:
    """One unit of work, as named by the orchestrator."""

    task_id: str
    description: str
    complexity: str = Complexity.STANDARD
    work_class: str = WorkClass.GENERAL
    #: What kind of work this is, which may pin the lead. See
    #: :mod:`quadratus.task_kinds`; most kinds express no preference, and the
    #: task then rides the difficulty ladder (``DIFFICULTY_LADDER``).
    kind: str = TaskKind.GENERAL
    #: Force a particular lead. Normally left to the kind pin, then the
    #: difficulty ladder; the rotation is only the last resort.
    lead: Optional[str] = None
    #: How the kind/difficulty above were arrived at: ``"labelled"`` when the
    #: orchestrator said so, ``"defaulted"`` when nothing was found. Carried
    #: so a defaulted route is visible in the record instead of being
    #: indistinguishable from a stated one -- the shape of the silent misroute
    #: this field was added to expose.
    metadata_confidence: str = "labelled"
    #: Provenance for the above, rendered into diagnostics.
    metadata_notes: List[str] = field(default_factory=list)
    #: The execution tier the orchestrator asked for ("normal" or "direct");
    #: what it gets is fixed on the contract at dispatch.
    tier: str = "normal"
    #: What this task is allowed to touch and how far it may go. ``None``
    #: means unbounded, which is the honest description of a task nobody
    #: scoped. See :mod:`quadratus.scope`.
    scope: Optional["TaskScope"] = None
    #: Required operations, independent of difficulty or the write grant.
    needs: frozenset[str] = field(default_factory=frozenset)

    def __post_init__(self) -> None:
        declared, self.description = _read_task_needs(self.description)
        if isinstance(self.needs, str):
            raise ValueError("Task needs must be a collection, not a string.")
        self.needs = normalise_needs(self.needs) | declared | frozenset(needs_from_text(
            self.description, self.scope.acceptance if self.scope else (),
        ))
        if self.needs - KNOWN_NEEDS:
            raise ValueError(f"Unknown task needs: {sorted(self.needs - KNOWN_NEEDS)}")
        # ``kind`` and ``work_class`` grew up in different modules and both can
        # say "security". Keeping them synchronised here means no caller can
        # construct a task that one security mechanism sees and the other
        # misses -- which is exactly the bug this guards against: the excursion
        # machinery watching work_class while the orchestrator's label only
        # ever set kind.
        if self.kind == TaskKind.SECURITY:
            self.work_class = WorkClass.SECURITY
        elif self.work_class == WorkClass.SECURITY and self.kind == TaskKind.GENERAL:
            self.kind = TaskKind.SECURITY


@dataclass(frozen=True)
class SurveyConfig:
    """A survey run: one run that continues through many failures and records
    each, judged apart from acceptance (Davis, 2026-09-30; Codex and Claude
    answers on #39).

    The orchestrator never asks for more: the operator sets the allowance in
    advance, the harness spends it. ``recovery_tasks`` is how many
    continuation or repair tasks (CONTINUES or RESOLVES named after a
    failure) may run; the next failure past it ends the run. The ordinary
    two-unfinished breaker is replaced by a same-cause repeat stop: a
    continuation that fails the same way as its predecessor with no new
    evidence (the same cause and nothing newly changed) stops the run.
    Every re-plan after a failure carries a ``HYPOTHESIS:`` line, recorded
    beside the harness's own record and never acted on.
    """
    recovery_tasks: int = 6

    def __post_init__(self):
        if type(self.recovery_tasks) is not int or self.recovery_tasks < 0:
            raise ValueError('recovery_tasks must be a non-negative whole number')


#: The file types that render, written once: the suffix half of ``_UI_PATH``,
#: the after-the-fact rule (``_rendered_file``) and the sentence that states
#: it (``_UNDECLARED_RENDER_RULE``) are all built from this.
_RENDERED_SUFFIXES = ("html", "htm", "css", "scss", "sass", "less", "jsx", "tsx", "vue", "svelte")
_UI_FILE = re.compile(r"\.(" + "|".join(_RENDERED_SUFFIXES) + r")$", re.I)
_UI_PATH = re.compile(r"(^|/)(templates|static|components|pages|views|styles?)/|" + _UI_FILE.pattern, re.I)


def _renders(path) -> bool:
    """Whether a permitted path is a rendered file or a UI directory.

    A bare UI directory ("static", "templates") is a rendered scope too: the
    path rule wants its trailing slash (the direct-tier admission tests
    declare permitted_paths=["static"])."""
    text = str(path)
    return bool(_UI_PATH.search(text) or _UI_PATH.search(text.rstrip("/") + "/"))


def _rendered_file(path) -> bool:
    """Whether a file a task changed is itself rendered: its own type is one
    of ``_RENDERED_SUFFIXES`` and it is not a test or test-support file.

    Narrower than ``_renders`` on purpose. That one reads a declared scope,
    where a UI folder is a statement of intent. This one reads what changed
    under a scope that named nothing rendered, and there a folder name says
    nothing about the file: app/views/user.py, pages/api/x.ts and
    tests/pages/login.py each ended a finished task as unrendered design work
    (review 1110, engine 1). A test page or an HTML fixture is not shown to a
    user, so it is not design work either."""
    from .integration import is_test_support
    text = str(path)
    return bool(_UI_FILE.search(text)) and not is_test_support(text)


def is_design_task(spec) -> bool:
    """Work that changes what a user sees: the frontend kind, or a declared
    scope that reaches UI files.

    A scope naming a bare UI directory counts. It used to count only for the
    frontend send-back, so a backend task scoped to "static" or "templates"
    shipped CSS and HTML with no render and no design review. A parent
    directory ("app") still names nothing rendered: only the paths a task
    actually changed can settle that, after its draft."""
    if getattr(spec, "kind", None) == TaskKind.FRONTEND:
        return True
    paths = getattr(getattr(spec, "scope", None), "permitted_paths", ()) or ()
    return any(_renders(p) for p in paths)


def _frontend_without_rendered_file(spec) -> bool:
    """A task labelled frontend whose declared scope names files but none
    that renders (no template, page, component, stylesheet or markup). The
    label alone makes it design work that must be captured; the recovery
    diagnostic lane on 2e57e94 stopped on a CommonJS state model labelled
    frontend with no page anywhere in its acceptance. A frontend task with no
    declared paths is left as it is: there is nothing to compare."""
    if getattr(spec, "kind", None) != TaskKind.FRONTEND:
        return False
    paths = getattr(getattr(spec, "scope", None), "permitted_paths", ()) or ()
    return bool(paths) and not any(_renders(p) for p in paths)


def is_review_only(spec) -> bool:
    """A task whose SCOPE declares ``"edits": "none"``: an audit. Only the
    declaration counts; a one-line fix is still editing work (Codex review of
    3d5c3f3), and the scope checks still measure whatever the call changes."""
    return bool(getattr(getattr(spec, "scope", None), "review_only", False))


def _scope_path(path) -> str:
    """A project-relative path in the one form paths are compared in: a
    leading "./" dropped and separators collapsed, nothing else.

    ``str.strip("./")`` strips characters, not a prefix, so ".env" and "env"
    compared equal: a parallel task scoped to "env" that changed ".env"
    passed the merge's own scope test."""
    return PurePosixPath(str(path)).as_posix()


def _repeat_key(spec) -> tuple:
    """What "the same task" means to the repeat check: the task's words
    together with the paths its scope permits.

    The words alone are not the task. SCOPE is read off the text before it
    is compared, so one instruction given for a.py and then for b.py read as
    the same task named twice and stalled the run (review 1110, engine 3)."""
    paths = getattr(getattr(spec, "scope", None), "permitted_paths", None) or ()
    return (spec.description, tuple(sorted({_scope_path(p) for p in paths})))


def _held_lead(chosen, spec) -> Optional[str]:
    """The lead a check before dispatch picked for ``spec``, or None.

    ``chosen`` is ``(task id, lead)`` or ``(task id, lead, what was named)``.
    Naming spends no id, so a task sent back and a different task named
    next share one: a hold that says what was named (kind, difficulty and
    permitted paths) stands only for that, and the other task is routed on
    its own labels. Both checks that hold a lead (capture, packet) say what
    was named; a hold of two parts, from older code, stands on the id."""
    if not chosen or chosen[0] != spec.task_id:
        return None
    if len(chosen) > 2 and chosen[2] != _lead_hold(spec):
        return None
    return chosen[1]


def _lead_hold(spec) -> tuple:
    """What a held lead was picked for: what routing reads off the task."""
    return (spec.kind, spec.complexity, _repeat_key(spec)[1])


def _literal_paths(scope) -> Optional[set]:
    paths = list(getattr(scope, "permitted_paths", ()) or ())
    if not paths or any(any(ch in p for ch in "*?[]") for p in paths):
        return None
    return {_scope_path(p) for p in paths}


@dataclass(frozen=True)
class _Intake:
    """What one named task's directive lines said (Session._read_directives).

    ``spec`` is the task without those lines, its requested tier set.
    ``refused`` is "" when the task may be dispatched, else which check
    refused it: ``covers``, ``resolves``, ``handoff`` (the harness has already
    verified what the task would verify), ``capture``, or ``batch`` (a line a
    parallel batch cannot honour). ``problem`` is that check's own text."""
    spec: "TaskSpec"
    continues: Optional[str] = None
    covers: List[str] = field(default_factory=list)
    #: None when the task carries no RESOLVES line; the raw tokens otherwise.
    resolves: Optional[List[str]] = None
    refused: str = ""
    problem: str = ""
    #: The HYPOTHESIS line's text, or None when the task states none. Handed
    #: back so the serial loop never reads the line a second time.
    hypothesis: Optional[str] = None


#: The word at the left margin, then the rest of the line.
_NEEDS_LINE = re.compile(r"NEEDS[ \t]*:(.*)$", re.IGNORECASE)
#: The word in capitals behind indentation: written as the declaration, but
#: not where one is read.
_NEEDS_INDENTED = re.compile(r"[ \t]+NEEDS[ \t]*:")


def _needs_lines(description: str):
    """``(declarations, other lines, problem)`` for a task description.

    A line is a NEEDS declaration when it starts at the left margin and the
    rest of it parses as a JSON list, in any case. "Needs: a migration for
    the users table" is prose and stays in the description: it used to be
    read as a declaration that failed, and ended the run. In capitals the
    word is the declaration the prompt asks for, so a tail that is not a
    list is a problem to send back, never prose. So is the word in capitals
    behind indentation: it was read as a declaration before, and dropping
    it in silence would run the task without what it declared."""
    lines, declarations, problem = [], [], ""
    for line in (description or "").splitlines():
        match = _NEEDS_LINE.match(line)
        if match is None:
            if _NEEDS_INDENTED.match(line):
                problem = problem or (f"the line {line.strip()[:80]!r} is indented: a NEEDS declaration "
                                      "starts at the left margin")
            lines.append(line)
            continue
        try:
            values = json.loads(match.group(1))
        except ValueError:
            values = None
        if not isinstance(values, list):
            if line.startswith("NEEDS"):
                problem = problem or (f"the line {line.strip()[:80]!r} is not a NEEDS declaration: NEEDS must "
                                      "be a JSON list of operation names, for example NEEDS: [\"execute\"]")
            lines.append(line)
            continue
        if any(not isinstance(v, str) for v in values) or set(values) - KNOWN_NEEDS:
            problem = problem or "NEEDS allows only execute, patch, and direct-write"
            continue
        declarations.append(normalise_needs(values))
    return declarations, lines, problem


def _read_task_needs(description: str):
    declarations, lines, problem = _needs_lines(description)
    if problem:
        raise ValueError(problem + ".")
    if len(set(declarations)) > 1:
        raise ValueError("Conflicting NEEDS declarations.")
    return (declarations[0] if declarations else frozenset()), "\n".join(lines).strip()


def _invocation_role(role):
    def decorate(method):
        @wraps(method)
        def wrapped(self, *args, **kwargs):
            task_id = getattr(getattr(self, "_active_spec", None), "task_id", "run")
            with invocation(task_id, role):
                return method(self, *args, **kwargs)
        return wrapped
    return decorate


#: The task's own lead working on its source: each carries the lead's packet
#: and, after the draft, the original goal (Session._invoke_model_call).
_EDITING_ROLES = ('lead', 'revision', 'gate-fix', 'security-fix', 'design-fix', 'capture-redeclare',
                  'fixture-supply')


def _seat_takes_scratch(key: str) -> bool:
    """Whether ``key``'s CLI runs a review in a writable disposable copy."""
    from .cli_providers import CLAUDE_SPEC, CODEX_SPEC, GROK_SPEC
    vendor = (key or "").partition(":")[0]
    spec = next((s for s in (CLAUDE_SPEC, CODEX_SPEC, GROK_SPEC) if s.vendor == vendor), None)
    return bool(spec is not None and spec.copy_args)


class JobStoreUnavailable(RuntimeError):
    """The run's job store could not publish an operation record: before an
    effect, the effect does not start; after one, its outcome is recorded as
    uncertain and the run stops here."""


def _stops_the_run(exc: BaseException) -> bool:
    """Whether a stop met in a private copy (a parallel child's, a contest
    entrant's) or by a contest's judge is the run's stop, raised once the
    work it would lose is kept. One rule for all three.

    A task's own result is not: preserved partial work and a failed task
    (PartialWorkStopped), a cap, a refusal or a transport error
    (ProviderError), a stall (RunStalled). In a batch that child is not
    merged; in a contest that draft loses. Everything else is: a spent
    budget, an interrupt, a denied capability, dependency integrity, and any
    failure nothing names, which is handed to the operator as it was raised
    (map J27) and never read as one draft losing. A contest's routed lead is
    held to one rule more (``_never_replaced``)."""
    return not isinstance(exc, (PartialWorkStopped, ProviderError, RunStalled))


def _never_replaced(exc) -> bool:
    """Whether a contest's routed lead ended on a stop a single-lead run
    never replaces the lead for: a policy refusal, a denied capability or
    a permission block.

    All three are among the stops no re-plan repairs, and none is ever
    handed to another model. Before this a refused routed lead lost on
    completion and the challenger's draft was merged, so turning contests
    on was a quiet way around a refusal the run would otherwise have
    stopped on; a permission block took the same road until 2026-11-12.
    Only the routed lead: a challenger's stop is that draft losing."""
    return isinstance(exc, (ProviderRefusal, CapabilityUnavailable, PermissionBlocked))

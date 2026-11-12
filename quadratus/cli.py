"""Command-line interface for the multi-LLM workflow."""

from __future__ import annotations

import argparse
import logging
import sys
from contextlib import contextmanager
from pathlib import Path
from typing import List, Optional

from .config import Settings
from .orchestrator import CollaborationResult, Orchestrator
from .providers import ProviderError
from .registry import MODE_ROSTERS, mode_audit_gap
from .run_budget import MIN_LEAD_TURNS, RunLimits

try:
    import colorama
    from colorama import Fore, Style

    colorama.init()
    _COLOR = True
except Exception:  # pragma: no cover - optional dependency
    _COLOR = False

    class _Dummy:
        def __getattr__(self, _):
            return ""

    Fore = Style = _Dummy()  # type: ignore


def _c(text: str, color: str = "", bold: bool = False) -> str:
    if not _COLOR or not color:
        return text
    prefix = (Style.BRIGHT if bold else "") + color
    return f"{prefix}{text}{Style.RESET_ALL}"


def _build_settings(args: argparse.Namespace) -> Settings:
    settings = Settings.from_env()
    if getattr(args, "neutral_preferences", False):
        settings.neutral_preferences = True
    if args.rounds is not None:
        settings.rounds = max(1, args.rounds)
    if getattr(args, "lead_max_turns", None) is not None:
        # The operator's own cap: it outranks one derived from a per-call
        # threshold (run_budget.effective_lead_turns).
        settings.lead_max_turns = args.lead_max_turns
    for vendor in ("claude", "openai", "grok"):
        model = getattr(args, f"{vendor}_model", None)
        if model:
            setattr(settings, f"{vendor}_model", model)
            settings.cli_models.setdefault(vendor, {})['high'] = model
    return settings


def _run_probe(settings, *, everything: bool) -> int:
    """Ask the installed CLIs what they accept, and cache the answers.

    Separate from ``--status``, which is free and says what is configured.
    This one spends real subscription budget to find out what is true, which
    is why it is opt-in and why the default set is only the seats a run cannot
    start without.
    """
    from .probe import (
        alias_overrides_in_effect,
        default_probe_set,
        probe_binaries,
        probe_models,
        render_report,
        roster_keys,
    )

    keys = roster_keys() if everything else default_probe_set()
    print(_c(f"Probing {len(keys)} models against the installed CLIs…", Fore.CYAN, True))
    for line in alias_overrides_in_effect():
        print(_c(f"  override in effect — {line}", Fore.YELLOW))

    binaries = probe_binaries(settings)
    results = probe_models(
        keys,
        settings=settings,
        on_progress=lambda what: print(_c(f"  … {what}", Fore.YELLOW)),
    )
    print()
    print(render_report(binaries, results))
    return 0 if all(r.ok for r in results) else 1


def _run_session(goal: str, args: argparse.Namespace, settings: Settings) -> int:
    """Drive the orchestrated session engine against the subscription CLIs.

    The pipeline and the session engine are two different products sharing a
    process: the pipeline runs four fixed phases across every provider, the
    session engine decomposes a goal into sized tasks and routes each one.
    This is the second, and it is opt-in rather than the default because
    switching what ``quadratus "..."`` does is the operator's call, not a
    side effect of it becoming possible.

    Everything the run accumulates lands under one state directory so a
    session is inspectable afterwards and resumable in principle: artifacts
    keep the raw output that summaries only point at, the codebase map is
    cross-session memory about the repository, and the usage log is the
    API-price counterfactual.
    """
    options = _session_options(args)
    withheld = _withheld_line()
    if withheld:
        print(f">> {withheld}", flush=True)
    if getattr(args, "baseline", None):
        return _run_baseline(goal, args, settings)
    if args.project:
        from .project import Project
        from .project_run import run_project
        try:
            # main() has already refused bad limit flags; read again here so
            # this function stands alone, and before anything is cloned.
            limits, survey = _run_limits(args)
            project = Project.open(args.project, clone_url=args.clone or '', branch=args.branch or '')
            result = run_project(
                goal, project, settings, allow_writes=args.allow_writes,
                check=args.check or '', state_dir=args.state_dir,
                extra_checks=getattr(args, "extra_check", None) or (),
                capture_profile=getattr(args, "capture_profile", None),
                readiness=getattr(args, "readiness", None),
                decider=getattr(args, "decider", None),
                decider_labels=getattr(args, "decider_labels", "unstated"),
                forbid=args.forbid, declared_paths=args.declared_paths,
                **({"repair_tests": tuple(args.repair_tests)} if getattr(args, "repair_tests", None) else {}),
                **({"test_changes": args.test_changes} if getattr(args, "test_changes", None) else {}),
                max_tasks=args.max_tasks, mode=args.mode,
                **({"resume_from": args.resume} if getattr(args, "resume", None) else {}),
                security_verdict_json=getattr(args, "security_verdict_json", False),
                survey=survey,
                # Passed only when chosen, so a run with no limit flag and no
                # session setting is exactly the call made before they existed.
                **({"run_limits": limits} if limits is not None else {}),
                **({"session_options": options} if options else {}),
                **_trust(args),
                # Only when contests are on: "off" is the call made before.
                **({"compete": args.compete} if getattr(args, "compete", None) not in (None, "off") else {}),
                direct_tier=bool(getattr(args, "direct_tier", False)),
                tasks=_task_list(getattr(args, "tasks", None)),
                progress=lambda message: print(f">> {message}", flush=True),
                ask_operator=_operator(args),
                plan_gate=(lambda plan: print(plan) is None and
                           input("Run this plan? [y/N] ").lower() in ('y', 'yes')) if args.plan_gate else None,
            )
        except (ValueError, ProviderError, OSError) as exc:
            print(f"Error: {exc}")
            return 1
        except KeyboardInterrupt as exc:
            return _interrupted(exc)
        print(result.report)
        print(f"\nSource diff: {result.run_dir / 'changes.diff'}")
        if args.output:
            Path(args.output).write_text(result.report, encoding='utf-8')
        return 0 if result.completed else 1

    state = Path(args.state_dir or ".quadratus").expanduser()
    # Owner-only and ignored by git from the moment it exists, as a project
    # run's state folder is: it holds artifacts, the map and the usage log.
    from .safe_io import prepare_state_dir
    try:
        prepare_state_dir(state)
    except OSError as exc:
        print(_c(f"\nError: the state directory {state} cannot be used: {exc}", Fore.RED, True))
        return 1
    try:
        with _state_lock(state):
            return _run_projectless(goal, args, settings, state, options)
    except _StateBusy as exc:
        print(_c(f"\nError: {exc}", Fore.RED, True))
        return 1


#: The exit status of a run the operator interrupted, as a shell reports it.
INTERRUPTED = 130


def _interrupted(exc) -> int:
    """Say where an interrupted run's record is, and end with status 130.

    ``run_project`` and ``run_baseline`` write the whole record and then pass
    an interrupt on when it landed while the call transcripts were copied
    (``project_run.RunInterrupted``, which carries the result). Uncaught, it
    ended the command in a traceback that never named the run folder. An
    interrupt with no result landed before any record existed."""
    result = getattr(exc, "result", None)
    run_dir = getattr(result, "run_dir", None)
    if run_dir is None:
        print("\nInterrupted before a run record was written.")
        return INTERRUPTED
    print(f"\nInterrupted. The run record was written first: {run_dir}")
    print(f"Report: {Path(run_dir) / 'report.md'}")
    print(f"Source diff: {Path(run_dir) / 'changes.diff'}")
    return INTERRUPTED


def _baseline_status(result) -> tuple:
    """``(exit status, closing line)`` for a finished baseline run.

    Never the project run's "0 means complete": a baseline is not judged.
    1 when the run stopped, 2 when its last check failed or no check ran,
    and 0 only when it ran to its end and its last check passed, which says
    that much and nothing about the goal. Read from the run's own record."""
    import json
    if result.error:
        return 1, f"The baseline stopped: {result.error.splitlines()[0][:300]}"
    try:
        record = json.loads((Path(result.run_dir) / "result.json").read_text(encoding="utf-8"))
        gate = record["baseline"]["gate"]
        passed, configured = gate.get("passed"), gate.get("configured")
    except (OSError, ValueError, KeyError, TypeError, AttributeError):
        return 2, "The baseline's record could not be read, so its last check is unknown."
    if passed is True:
        return 0, "The baseline ran to its end and its last check passed. The goal was not judged."
    if passed is False:
        return 2, "The baseline's last check failed. The goal was not judged."
    return 2, ("No check ran, so nothing was test-verified. The goal was not judged."
               if not configured else "No check result was recorded. The goal was not judged.")


def _run_baseline(goal: str, args: argparse.Namespace, settings: Settings) -> int:
    """One model alone on the project (``quadratus.baseline``): the
    measurement arm beside a project run, under the same checks, limits and
    trust choices. It never ends as "complete"; see :func:`_baseline_status`."""
    from .baseline import run_baseline
    from .project import Project
    try:
        limits, _ = _run_limits(args)
        project = Project.open(args.project, clone_url=args.clone or '', branch=args.branch or '')
        result = run_baseline(
            goal, project, settings, model=args.baseline, check=args.check or '',
            extra_checks=getattr(args, "extra_check", None) or (),
            readiness=getattr(args, "readiness", None), state_dir=args.state_dir,
            forbid=args.forbid, declared_paths=args.declared_paths,
            fix_rounds=args.fix_rounds,
            **({"run_limits": limits} if limits is not None else {}),
            **_trust(args),
            progress=lambda message: print(f">> {message}", flush=True),
        )
    except (ValueError, ProviderError, OSError) as exc:
        print(f"Error: {exc}")
        return 1
    except KeyboardInterrupt as exc:
        return _interrupted(exc)
    print(result.report)
    print(f"\nSource diff: {result.run_dir / 'changes.diff'}")
    if args.output:
        Path(args.output).write_text(result.report, encoding='utf-8')
    status, line = _baseline_status(result)
    print(f"\n{line}")
    return status


class _StateBusy(ProviderError):
    """Another run holds this state directory's lock."""


@contextmanager
def _state_lock(state: Path):
    """Hold a state directory's run lock for the length of a projectless session.

    Two sessions started in one folder would otherwise share one artifact
    store, codebase map and usage log. Same file name, same flock and same
    refusal as ``project_run._project_lock``, so with the default
    ``.quadratus`` a projectless session and a ``--project .`` run in one
    folder also keep each other out.
    """
    import fcntl

    with (state / "run.lock").open("a") as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            raise _StateBusy(f"Another Quadratus run is using {state}.") from exc
        try:
            yield
        finally:
            fcntl.flock(lock, fcntl.LOCK_UN)


def _run_projectless(goal: str, args: argparse.Namespace, settings: Settings, state: Path, options: dict) -> int:
    """A session with no project, run under its state directory's lock.

    ``--check`` and ``--allow-writes`` need ``--project`` (a check has to run
    somewhere and a write has to land somewhere), so this path has no gate and
    no write grant to offer. An earlier version built both from flags the
    parser had already refused.
    """
    from .artifacts import ArtifactStore
    from .codebase_map import CodebaseMap
    from .routing import OrchestratorUnavailable
    from .runtime import Fleet, new_session
    from .session import OperatorInputNeeded, RunStalled, SessionConfig
    from .session_knobs import session_options
    from .usage import UsageMeter

    meter = UsageMeter(state / "usage.jsonl")
    store = ArtifactStore(state / "artifacts")

    def progress(message: str) -> None:
        print(_c(f">> {message}", Fore.YELLOW), flush=True)

    def ask_operator(question: str) -> str:
        """The ASK channel. A question the orchestrator says only you can
        answer is not one to guess at, so it is asked on the terminal and the
        answer becomes a standing ruling for the rest of the session."""
        print(_c(f"\n?? The orchestrator needs an answer:\n   {question}", Fore.CYAN, True))
        return input("   > ").strip()

    def plan_gate(plan: str) -> bool:
        """Reviewing the plan is reviewing the work at a fraction of the cost."""
        print(_c("\n=== Expected task list ===", Fore.MAGENTA, True))
        print(plan)
        return input("\nRun this plan? [y/N] ").strip().lower() in ("y", "yes")

    code_map = CodebaseMap(state / "codebase-map.jsonl")
    for notice in list(getattr(code_map, "notices", ()) or ()):
        # What a damaged or over-long map cost, said once as a project run says it.
        progress(f"Codebase map: {notice}")
    decider = _decider_for(args, meter)
    if decider is not None:
        # Said before any task text is sent, as a project run says it: the
        # --decider help promises the run names the destination.
        from .decisions import disclosure
        progress(disclosure(decider))
    config = SessionConfig(
        mode=args.mode,
        codebase_map=code_map,
        plan_gate=plan_gate if args.plan_gate else None,
        ask_operator=ask_operator,
        # So the lead prompt states the cap the Fleet applies (--lead-max-turns).
        lead_max_turns=getattr(settings, "lead_max_turns", None),
        security_verdict_json=getattr(args, "security_verdict_json", False),
        progress=progress,
        decider=decider,
        decider_labels=getattr(args, "decider_labels", "unstated"),
        **session_options(options),
    )

    fleet = Fleet(settings, usage_meter=meter)
    print(_c("Fleet:", Fore.CYAN, True))
    for line in fleet.status_lines():
        print(f"  - {line}")

    try:
        session = new_session(goal, store, fleet=fleet, config=config)
        summaries = session.run(max_tasks=args.max_tasks)
    except OrchestratorUnavailable as exc:
        print(_c(f"\nThe run cannot be seated: {exc}", Fore.RED, True))
        return 1
    except RunStalled as exc:
        print(_c(f"\nThe run stalled: {exc}", Fore.RED, True))
        return 1
    except OperatorInputNeeded as exc:  # pragma: no cover - ask_operator is wired
        print(_c(f"\nThe orchestrator needs an answer: {exc}", Fore.RED, True))
        return 1
    except ProviderError as exc:
        print(_c(f"\nError: {exc}", Fore.RED, True))
        return 1
    finally:
        fleet.close()

    # The session's own verdict decides how this ends, as it does for a
    # project run. A stop it recorded (the task cap, a failed task, findings
    # or requirements left open) is an incomplete run: it used to be printed
    # as "N task(s) complete" with exit 0. A plan the operator declined is
    # their own decision and stays the clean "Nothing ran."
    stopped = str(getattr(session, "stop_reason", "") or "")
    incomplete = not stopped.startswith("PlanDeclined") and (
        bool(stopped) or getattr(session, "completed", True) is False)
    why = stopped or "the session did not report the goal met"

    if not summaries:
        if incomplete:
            print(_c(f"\nThe run stopped incomplete before any task closed: {why}", Fore.RED, True))
            return 1
        print(_c("\nNothing ran.", Fore.YELLOW))
        return 0

    if incomplete:
        print(_c(f"\n=== {len(summaries)} task(s) closed; the run is incomplete ===", Fore.YELLOW, True))
    else:
        print(_c(f"\n=== {len(summaries)} task(s) complete ===", Fore.GREEN, True))
    for summary in summaries:
        print(_c(f"\n--- {summary.task_id} — {summary.author} ---", Fore.GREEN))
        print(summary.summary)
        if summary.reasoning:
            print(f"\nWhy: {summary.reasoning}")
        for ref in summary.refs:
            # The pointer, not the content: raw output stays in the store and
            # a summary is an index into it.
            print(f"  [{ref.kind}] {ref.id} — {ref.lines} lines")

    if args.output:
        Path(args.output).write_text(
            session.memory.render(current=""), encoding="utf-8"
        )
        print(_c(f"\nLedger written to {args.output}", Fore.GREEN))

    print(_c("\n" + meter.render_report(), Fore.CYAN))
    print(_c(f"\nArtifacts, map and usage log: {state}", Fore.CYAN))
    if incomplete:
        print(_c(f"\nThe run stopped incomplete: {why}", Fore.RED, True))
        return 1
    return 0


def _run_monitor(args: argparse.Namespace) -> int:
    """Print what the engine is doing, read from the run directory, and exit.

    Observational only: ``quadratus.monitor`` reads the series lock, the
    latest run directory and its bounded tails, and writes nothing. Meant
    for a terminal over ssh, so it is one screen of plain text and never a
    model call. Exit 0 whether or not a run is live; 1 only when there is
    nothing to read because no project or series was named.

    With ``--serve PORT`` it instead keeps running and serves the same
    status as a self-reloading web page on 127.0.0.1 (quadratus.monitor_server),
    for a phone via Tailscale Serve.
    """
    from .monitor import read_status, render_text, run_history

    if not args.project and not args.series:
        print("Error: --monitor needs --project DIR or --series DIR")
        return 1
    if args.serve is not None:
        from .monitor_server import serve
        # Passed only when given: a line without them is the call made before
        # the flags existed, and the two environment settings still apply.
        admitted = {}
        if args.allow_host:
            admitted["allow_hosts"] = tuple(args.allow_host)
        if args.allow_project:
            admitted["allow_projects"] = tuple(args.allow_project)
        return serve(args.serve, project=args.project, series=args.series,
                     state_dir=args.state_dir, history=args.history, **admitted)
    status = read_status(args.project, args.series, state_dir=args.state_dir)
    project = status.get("project")
    history = run_history(project, state_dir=args.state_dir, limit=args.history) \
        if project and project != "unknown" else []
    print(render_text(status, history, calls=bool(getattr(args, "calls", False))))
    return 0


def _trust(args) -> dict:
    """The operator's trust choices as ``run_project`` arguments.

    Only the ones given, so a line without them is the call made before the
    flags existed and the two environment settings still decide.
    """
    chosen = {}
    if getattr(args, "trust_project_policy", None):
        chosen["trust_project_policy"] = args.trust_project_policy
    if getattr(args, "allow_project_agent_config", False):
        chosen["allow_project_agent_config"] = True
    if getattr(args, "no_declared_checks", False):
        chosen["declared_checks"] = False
    if getattr(args, "accept_watched_paths", False):
        chosen["accept_watched_paths"] = True
    if getattr(args, "accept_unrecorded_parent", False):
        chosen["accept_unrecorded_parent"] = True
    return chosen


def _policy_hash(text):
    """An argparse type: a policy file's sha256, read by the rule the policy
    loader uses (policy.approval_digest). A mistyped hash is refused here: a
    project with no policy file would otherwise pass over it in silence."""
    from .policy import PolicyError, approval_digest
    try:
        digest = approval_digest(text, "the value")
    except PolicyError:
        digest = ""
    if not digest:
        raise argparse.ArgumentTypeError(
            f"expected the policy file's sha256 (64 hexadecimal characters), not {str(text)[:80]!r}")
    return digest


def _withheld_line() -> str:
    """One line naming the environment variables a child process is not
    given, or "" when there are none. Without it a check that fails for want
    of an inherited variable shows only the project's own error."""
    from .child_env import withheld_notice
    return withheld_notice()


def _task_list(path):
    """The operator's explicit task list: a JSON array of non-empty strings."""
    if not path:
        return None
    import json
    try:
        listed = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise ValueError(f"--tasks {path}: {exc}") from exc
    if not isinstance(listed, list) or not listed or any(not isinstance(t, str) or not t.strip() for t in listed):
        raise ValueError(f"--tasks {path}: expected a JSON array of non-empty task texts")
    return listed


def _decider_for(args, meter=None):
    """The decider named on the command line, for a projectless session (the
    project runner resolves its own, with the run budget)."""
    from .decisions import decider_from_name
    return decider_from_name(getattr(args, "decider", None), meter=meter)


def _operator(args):
    """Interactive answers, preceded by any rulings the operator gave in advance."""
    def interactive(question):
        return input(f"\n{question}\n> ").strip()
    path = getattr(args, "rulings", None)
    if not path:
        return interactive
    from .project_run import standing_rulings
    return standing_rulings(path, fallback=interactive)


#: What a flag needs before it does anything, and how the refusal reads. A
#: flag given where its need is not met would be accepted and then ignored
#: (``--mode`` on a bare pipeline run, ``--rounds`` on a project run), so the
#: command line is refused instead, naming the flag and what it needs.
_NEEDS = {
    "project": "{names} require --project",
    "engine": "{names} require --session or --project: the collaboration pipeline does not use {them}",
    "pipeline": ("{names} cannot be combined with --session or --project: only the collaboration "
                 "pipeline uses {them}"),
    "monitor": "{names} can only be used with --monitor",
}

#: The flags a ``--baseline`` run reads, by parser dest. One model does the
#: whole goal in one call, so everything about planning, routing, review and
#: capture has nothing to act on, and a flag not listed here is refused with
#: ``--baseline`` by name. A new flag is refused there until it is listed.
_BASELINE_READS = frozenset({
    "output", "project", "clone", "branch", "state_dir", "check", "extra_check", "readiness",
    "declared_paths", "forbid", "allow_writes", "neutral_preferences", "policy_preview",
    "trust_project_policy", "allow_project_agent_config", "no_declared_checks", "accept_watched_paths",
    "max_calls", "max_reported_tokens", "wall_seconds", "max_concurrent_workers",
    "reserve_tokens_per_call", "max_tokens_per_call", "lead_max_turns",
    "baseline", "fix_rounds", "status", "probe", "probe_all", "verbose",
})

#: ``--compete`` choices (contest.COMPETE_MODES; a test holds the two equal).
_COMPETE_MODES = ("off", "complex")

#: How much of a task description an external decider is sent, for the
#: --decider help (decisions.TASK_TEXT_LIMIT; a test holds the two equal).
_DECIDER_TEXT_LIMIT = 8_000

#: The two standing trust settings, named in the flags' help. Spelled here so
#: the parser builds without importing the policy and watch modules; a test
#: holds them equal to policy.TRUST_ENV and project_watch.ALLOW_AGENT_CONFIG_ENV.
_TRUST_ENV = "QUADRATUS_TRUST_PROJECT_POLICY"
_AGENT_CONFIG_ENV = "QUADRATUS_ALLOW_PROJECT_AGENT_CONFIG"
#: The same for the watched-paths acceptance (project_watch.ACCEPT_WATCHED_PATHS_ENV).
_WATCHED_ENV = "QUADRATUS_ACCEPT_WATCHED_PATHS"
#: The same for a resume from a run that left no record of its own
#: (project_run.ACCEPT_UNRECORDED_PARENT_ENV).
_UNRECORDED_ENV = "QUADRATUS_ACCEPT_UNRECORDED_PARENT"

#: Monitor flags only the served page reads: the one-screen status checks no
#: host name and takes no folder from a request.
_SERVE_ONLY = ("allow_host", "allow_project")
#: Monitor flags only the one-screen status reads: the served page lists
#: every call whether or not it is asked to.
_PRINT_ONLY = ("calls",)

#: Parse-time defaults are None so a flag that was typed can be told from one
#: that was not; the real defaults are filled in once the line is accepted.
_DEFAULTS = {"max_tasks": 20, "mode": "adversarial", "decider": "rule",
             "decider_labels": "unstated", "history": 10, "fix_rounds": 1}

#: The run-limit flags by the field each fills (run_budget.LIMIT_FIELDS). The
#: same names label a refused value, so the error says which flag to fix.
_LIMIT_FLAGS = {
    "max_calls": "--max-calls",
    "max_reported_tokens": "--max-reported-tokens",
    "wall_seconds": "--wall-seconds",
    "max_concurrent_workers": "--max-concurrent-workers",
    "reserve_tokens_per_call": "--reserve-tokens-per-call",
    "max_tokens_per_call": "--max-tokens-per-call",
    "recovery_tasks": "--survey-recovery",
}

#: A bounded run is bounded on all three: RunLimits has no "unlimited" value
#: for any of them, and filling one in silently (its defaults are 24 calls,
#: 500k tokens and 15 minutes) would stop a run the operator never capped.
_CORE_LIMITS = ("max_calls", "max_reported_tokens", "wall_seconds")

#: Session settings that take a number, by SessionConfig field.
_COUNT_KNOBS = ("recent_entries", "max_fix_cycles", "max_consults", "max_parallel_tasks")


def _whole(minimum: int):
    """An argparse type: a whole number of at least ``minimum``, read by the
    rule the run limits use (run_budget.whole_number)."""
    def parse(text):
        from .run_budget import whole_number
        try:
            return whole_number(text, "value", minimum)
        except ValueError:
            raise argparse.ArgumentTypeError(
                f"expected a whole number of at least {minimum}, not {text!r}") from None
    return parse


def _run_limits(args):
    """The RunLimits and SurveyConfig the limit flags describe.

    Built by ``run_budget.limits_from_fields``, the rule the GUI's Run limits
    form follows, so a run is bounded the same way from either. No limit flag
    gives ``(None, survey)``: the run made before the flags existed. Raises
    ValueError naming the flag otherwise.
    """
    from .run_budget import limits_from_fields
    fields = {name: getattr(args, name, None) for name in _LIMIT_FLAGS if name != "recovery_tasks"}
    fields["recovery_tasks"] = getattr(args, "survey_recovery", None)
    fields = {name: value for name, value in fields.items() if value is not None}
    enabled = any(name != "recovery_tasks" for name in fields)
    if enabled:
        missing = [_LIMIT_FLAGS[name] for name in _CORE_LIMITS if name not in fields]
        if missing:
            raise ValueError(
                "run limits need --max-calls, --max-reported-tokens and --wall-seconds together "
                f"(missing: {', '.join(missing)}): a bounded run is bounded on all three and none "
                "of them has an unlimited value. For example: --max-calls 90 "
                "--max-reported-tokens 5000000 --wall-seconds 3600")
        fields.setdefault("max_concurrent_workers", RunLimits.max_concurrent_workers)
    return limits_from_fields(enabled, fields, labels=_LIMIT_FLAGS)


def _session_options(args) -> dict:
    """The session settings the flags chose, by SessionConfig field name
    (quadratus.session_knobs). Empty when none was given, which leaves every
    setting at SessionConfig's own default."""
    chosen = {name: getattr(args, name) for name in _COUNT_KNOBS if getattr(args, name, None) is not None}
    if getattr(args, "no_spread_leads", False):
        chosen["spread_leads"] = False
    if getattr(args, "no_design_cross_check", False):
        chosen["design_cross_check"] = False
    return chosen


def _flag_name(action) -> str:
    return next((name for name in action.option_strings if name.startswith("--")), action.option_strings[0])


def _given(args, action) -> bool:
    """Whether the operator typed this flag. Every flag parses to None, False
    or an empty list when it is absent, so anything else was given."""
    value = getattr(args, action.dest, None)
    return value is not None and value is not False and value != []


def _misplaced(parser, args) -> str:
    """Why this command line would ignore something it was given, or "".

    Each flag says what it needs when it is declared (``flag(..., needs=)``
    in :func:`build_parser`). ``--monitor`` is stricter: it starts nothing,
    so it takes only the flags marked for it and no prompt. The print-and-exit
    actions (``--status``, ``--probe``, ``--policy-preview``) are held to the
    same needs and nothing more, so a full run line with ``--policy-preview``
    added still previews that run.
    """
    given = [action for action in parser.flags if _given(args, action)]
    if args.monitor:
        strays = [_flag_name(a) for a in given if a.needs != "monitor" and not a.monitor]
        if args.prompt or strays:
            return ("--monitor reads a run and starts nothing: it takes no prompt"
                    + (f" and does not use {', '.join(strays)}" if strays else ""))
        unserved = [_flag_name(a) for a in given if a.dest in _SERVE_ONLY]
        if unserved and args.serve is None:
            return (f"{', '.join(unserved)} require --serve: only the served page checks a host "
                    "name or takes a folder from a request")
        printed = [_flag_name(a) for a in given if a.dest in _PRINT_ONLY]
        if printed and args.serve is not None:
            return (f"{', '.join(printed)} cannot be combined with --serve: the served page lists "
                    "every call already")
        return ""
    met = {
        "project": bool(args.project),
        "engine": bool(args.session or args.project),
        "pipeline": not (args.session or args.project),
        "monitor": False,
    }
    problems = []
    if args.baseline:
        # Pipeline-only flags are named by their own rule below.
        unread = [_flag_name(a) for a in given if a.dest not in _BASELINE_READS and a.needs != "pipeline"]
        if unread:
            problems.append(
                "--baseline runs one model alone on the whole goal, with no orchestrator, reviewer or "
                f"capture: it does not use {', '.join(unread)}")
    elif any(a.dest == "fix_rounds" for a in given):
        problems.append("--fix-rounds requires --baseline: it is how many fix calls a failing check "
                        "may buy the one model")
    if any(a.dest == "accept_unrecorded_parent" for a in given) and not getattr(args, "resume", None):
        problems.append("--accept-unrecorded-parent requires --resume: only a resume has a parent run "
                        "to accept")
    for need, wording in _NEEDS.items():
        unmet = [a for a in given if a.needs == need and not met[need]]
        if not unmet:
            continue
        hints = sorted({a.hint for a in unmet if a.hint})
        problems.append(
            wording.format(names=", ".join(_flag_name(a) for a in unmet),
                           them="it" if len(unmet) == 1 else "them")
            + (f" ({'; '.join(hints)})" if hints else ""))
    return "; ".join(problems)


def _launch_problem(args) -> str:
    """Why this session or project run is refused before anything is opened,
    or "". Each case is a flag the run would otherwise accept and not honour,
    or a run that could not finish as asked."""
    if args.decider_labels == "all" and args.decider == "rule":
        return ("--decider-labels all requires --decider jev: with the rule the orchestrator "
                "labels every task itself, and 'all' would change nothing")
    if args.plan_gate and args.tasks:
        return ("--plan-gate cannot be combined with --tasks: the listed tasks are the plan, "
                "and no plan is asked for")
    if args.max_parallel_tasks is not None and not args.allow_writes:
        return ("--max-parallel-tasks requires --allow-writes: tasks run in parallel only when "
                "edits are enabled")
    if getattr(args, "compete", None) == "complex" and not args.allow_writes:
        return ("--compete complex requires --allow-writes: each draft is made in a private copy of "
                "a project that may be edited, and without edits no contest could form")
    gap = mode_audit_gap(args.mode)
    if gap:
        from .session import SessionConfig
        if SessionConfig(mode=args.mode).requirements_ledger:
            return f"--mode {args.mode} is refused: {gap}"
    try:
        _run_limits(args)
    except ValueError as exc:
        return str(exc)
    return ""


def build_parser() -> argparse.ArgumentParser:
    """The command line. Every option is declared through ``flag`` with what
    it needs, and ``parser.flags`` lists them for :func:`_misplaced`."""
    parser = argparse.ArgumentParser(
        description=(
            "Quadratus: Claude, ChatGPT and Grok collaborate on one coding task "
            "(plan, consensus, build-and-debate, synthesis), on your own "
            "subscriptions via the vendor coding-agent CLIs, or on billed APIs."
        )
    )
    parser.flags = []

    def flag(group, *names, needs=None, monitor=False, hint="", **kwargs):
        """Add one option. ``needs`` is "project", "engine" (--session or
        --project), "pipeline" (neither) or "monitor"; None runs anywhere.
        ``monitor`` marks the few flags --monitor itself reads."""
        action = group.add_argument(*names, **kwargs)
        action.needs, action.monitor, action.hint = needs, monitor, hint
        parser.flags.append(action)
        return action

    overrides = "session model overrides use QUADRATUS_ALIAS_<VENDOR>_<SEAT>"
    parser.add_argument("prompt", nargs="?", help="The coding task to solve.")
    flag(parser, "-o", "--output", help="Write the final solution to this file.")
    flag(parser, "--rounds", needs="pipeline", type=int, default=None,
         help="Number of review/refinement rounds (pipeline only).")
    flag(parser, "--claude-model", needs="pipeline", hint=overrides,
         help="Override the Claude model ID (pipeline only).")
    flag(parser, "--openai-model", needs="pipeline", hint=overrides,
         help="Override the OpenAI model ID (pipeline only).")
    flag(parser, "--grok-model", needs="pipeline", hint=overrides,
         help="Override the Grok model ID (pipeline only).")
    flag(parser, "--show-stages", needs="pipeline", action="store_true",
         help="Print every intermediate stage, not just the final answer (pipeline only).")
    flag(parser, "--status", action="store_true",
         help="Print provider availability and exit. Costs nothing.")
    flag(
        parser, "--probe", action="store_true",
        help=(
            "Ask each installed CLI which model aliases it actually accepts, "
            "cache the answers, and exit. Spends a small amount of "
            "subscription budget: one short call per model."
        ),
    )
    flag(parser, "--probe-all", action="store_true",
         help="Probe every model in the roster, not just the seats a run needs.")

    engine = parser.add_argument_group(
        "session engine",
        "The orchestrated loop: a persistent orchestrator decomposes the goal "
        "into sized tasks and routes each one, instead of running four fixed "
        "phases across every provider.",
    )
    flag(engine, "--project", "--repo", monitor=True,
         help="Project folder to open or create. Enables the session engine.")
    flag(engine, "--clone", needs="project", metavar="GITHUB_URL",
         help="Clone a GitHub repository into --project before running.")
    flag(engine, "--branch", needs="project",
         help="Create and switch to a new branch in --project before running.")
    flag(engine, "--session", action="store_true",
         help="Run the session engine instead of the collaboration pipeline.")
    flag(
        engine, "--resume", needs="project", metavar="RUN",
        help=("Resume an interrupted run of the same job (a folder under the state "
              "directory's runs) as its next episode: its record, lineage and spent "
              "allowance carry over. Refused before any call when the run is sealed, "
              "already resumed, ended with a call in flight or unknown usage, or when "
              "the source, goal, policy, limits, settings, gates or dependency trees differ."),
    )
    flag(
        engine, "--max-tasks", needs="engine", type=int, default=None,
        help=(
            "Runaway backstop, not a quality gate (default 20). A loop that "
            "has not converged by then has a problem the cap will not fix."
        ),
    )
    flag(
        engine, "--state-dir", needs="engine", monitor=True, default=None,
        help=(
            "Where artifacts, the cross-session codebase map and the usage log "
            "live (default .quadratus)."
        ),
    )
    flag(
        engine, "--mode", needs="engine", default=None, choices=sorted(MODE_ROSTERS),
        help=(
            "Which models participate (default adversarial: the brain trust, one "
            "seat per vendor). collaborative seats two vendors. solo seats one "
            "peer and is refused while the requirements ledger is on, because "
            "the final audit needs a peer from another vendor."
        ),
    )
    flag(
        engine, "--plan-gate", needs="engine", action="store_true",
        help=(
            "Show the full expected task list and wait for approval before "
            "any window is spent. Reviewing the plan is reviewing the work at "
            "a fraction of the cost."
        ),
    )
    flag(engine, "--security-verdict-json", needs="engine", action="store_true",
         help="Require the strict v1 JSON security verdict (opt-in).")
    flag(
        engine, "--check", needs="project", metavar="CMD",
        help=(
            "The project's own check command, run after each task's work is "
            "final (e.g. --check 'pytest -q'). The deterministic half of "
            "'do the pieces fit together'."
        ),
    )
    flag(
        engine, "--readiness", needs="project", metavar="JSON",
        help=(
            "The operator's capability readiness probes, run once before any model "
            "call under this environment: [{\"id\": \"browser\", \"argv\": [...], "
            "\"timeout\": 30}]. A failing probe stops the run as an operator handoff; "
            "passing proves readiness only, never acceptance."
        ),
    )
    flag(
        engine, "--capture-profile", needs="project", metavar="JSON",
        help=(
            "The operator's preview profile, so the harness starts the app and "
            "captures UI tasks' renders itself: {\"preview\": [argv], \"origin\": "
            "\"http://127.0.0.1:PORT\", \"ready_path\": \"/\", \"ready_timeout\": 30, "
            "\"capture_timeout\": 120}. Validated before any model call."
        ),
    )
    flag(
        engine, "--decider", needs="engine", choices=("rule", "jev"), default=None,
        help=(
            "Who answers a routing decision the orchestrator left unlabelled (task kind and "
            "difficulty): 'rule' is the deterministic default; 'jev' asks TypeSafe AI's Jev "
            "(pip install 'quadratus[decisions]', TYPESAFE_API_KEY). With 'jev', each task "
            f"description it routes, up to {_DECIDER_TEXT_LIMIT:,} characters, is sent to "
            "api.typesafe.ai (ai-gateway.vercel.sh for a vck_ key, or the host in "
            "TYPESAFE_BASE_URL) as a billed API call, metered and recorded on the task. Task "
            "text can carry file paths, requirement text and code; the run names the "
            "destination in its first progress lines. A stated label is never overridden."
        ),
    )
    flag(
        engine, "--decider-labels", needs="engine", choices=("unstated", "all"), default=None,
        help=(
            "With --decider jev: 'unstated' (default) fills in only the labels the orchestrator "
            "left out; 'all' asks the orchestrator not to write a KIND line, so the decider "
            "routes every planned task. A label the orchestrator states anyway is kept."
        ),
    )
    flag(
        engine, "--extra-check", needs="project", metavar="CMD", action="append",
        help=(
            "A further required check run beside --check, split without a "
            "shell (repeatable; e.g. --extra-check 'node --test tests/ui/a.test.js'). "
            "Patterns are refused: name the files."
        ),
    )
    flag(
        engine, "--survey-recovery", needs="project", type=int, default=None, metavar="N",
        help=(
            "Run as a survey: continue through failures, spending at most N continuation or "
            "repair tasks, require a HYPOTHESIS line on each re-plan, stop only on a same-cause "
            "repeat, and report a survey section apart from acceptance."
        ),
    )
    flag(
        engine, "--tasks", needs="project", metavar="FILE", default=None,
        help=(
            "Run this JSON list of task texts in order instead of asking the orchestrator: "
            "each text is read as an orchestrator reply would be (KIND, SCOPE, TIER, COVERS "
            "lines and the description). With the requirements ledger on (the default), the "
            "first text starts with a 'REQUIREMENTS:' block (R1: ...) and every text carries "
            "a 'COVERS: R1' line; the whole list is checked before any call and every problem "
            "is reported at once. No planner or acknowledgment call is made and no other task "
            "runs; a text the loop would send back stops the run. The goal is still required "
            "for the record and the prompts, and it is not judged."
        ),
    )
    flag(
        engine, "--direct-tier", needs="project", action="store_true",
        help=(
            "Enable the direct execution tier: the orchestrator may label a small, local task "
            "TIER: direct; a deterministic admission decides, and an admitted task runs with no "
            "collaborator review. The harness writes the close-out on every tier. Checks, "
            "capture, design review and every stop are unchanged."
        ),
    )
    flag(
        engine, "--allow-writes", needs="project", action="store_true",
        help=(
            "Let the agents write files. Off by default: several models in one "
            "tree is write-thrash, and a reviewer asked to critique will edit."
        ),
    )
    flag(
        engine, "--rulings", needs="project", metavar="FILE",
        help=("JSON list of {\"about\": regex, \"answer\": text}: answers you give in advance to "
              "questions the planner may ask. Anything else is asked interactively."),
    )
    flag(
        engine, "--neutral-preferences", needs="engine", action="store_true",
        help=("Run without the operator's personal CLI configuration (plugins, hooks, rules) "
              "where each CLI allows it. Account-side rules that cannot be removed are recorded."),
    )
    flag(engine, "--policy-preview", needs="project", action="store_true",
         help="Show the resolved policy without running models or checks.")
    flag(
        engine, "--trust-project-policy", needs="project", type=_policy_hash, default=None, metavar="SHA256",
        help=(
            "Approve the project's own .quadratus/policy.json by the sha256 of that exact file. "
            "Needed only when the file grants something (a command check, external effects, a "
            "withheld instruction file): the run is then refused before any model call and the "
            "refusal prints the hash and what the file asks for. A policy that only narrows needs "
            f"no approval. --policy-preview reads it too. Same as {_TRUST_ENV}."
        ),
    )
    flag(
        engine, "--allow-project-agent-config", needs="project", action="store_true",
        help=(
            "Let the run start in a project that carries vendor agent configuration "
            "(.claude/settings.json, .mcp.json, .codex/, .grok/). A headless vendor CLI started "
            "there can run its hooks and MCP servers as you without asking, so such a project is "
            "otherwise refused before any model call. Allows exactly the files present at the "
            f"start. Same as {_AGENT_CONFIG_ENV}=1."
        ),
    )
    flag(
        engine, "--no-declared-checks", needs="project", action="store_true",
        help=(
            "Run only your own checks (--check, --extra-check): nothing the project declares is "
            "added beside them or run in their place. --policy-preview reads it too."
        ),
    )
    flag(
        engine, "--accept-watched-paths", needs="project", action="store_true",
        help=(
            "Start although the last run on this project recorded a change to a path outside the "
            "measured source that no task may change (a git hook, an env file, a link, vendor agent "
            "configuration) and that change is still in place. Such a project is otherwise refused "
            "before any model call, with each path named: inspect them first. An acceptance is "
            "written to this run's record, and later runs do not ask about the same paths again. "
            f"Same as {_WATCHED_ENV}=1."
        ),
    )
    flag(
        engine, "--accept-unrecorded-parent", needs="project", action="store_true",
        help=(
            "With --resume: resume a run that left no record of its own to hold its checkpoint to "
            "(no readable result.json, as after a killed process or a restart, or a result.json "
            "from before records named their committed state). Such a run is otherwise refused "
            "before any model call, because nothing in its folder proves the checkpoint is the one "
            "the harness wrote: inspect the run folder first. Every other check of the resume still "
            "runs. The acceptance is written to this run's record and to every later episode of the "
            "job, and tools/truth_check.py reports them unverified. A run whose record names a "
            f"different committed state stays refused. Same as {_UNRECORDED_ENV}=1."
        ),
    )
    flag(
        engine, "--compete", needs="project", choices=_COMPETE_MODES, default=None,
        help=(
            "Contests (default off). With 'complex', each task routed at complex difficulty is "
            "drafted twice, by two models from different vendors that cannot see each other's work, "
            "each in its own private copy of the project; measured facts pick the draft that is "
            "merged (finished, inside its scope, your checks). When they tie, a model from a third "
            "vendor is asked once if the brain trust has one; today it has two vendors, so the tie "
            "goes to the smaller diff, then the routed lead. Costs about one extra draft per contested task, and needs --allow-writes. "
            "The routed lead's own refusal still stops the run."
        ),
    )
    flag(engine, "--path", needs="project", dest="declared_paths", action="append", default=[],
         help="Declare a narrow project-relative path; whole-project dot is refused. Repeat as needed.")
    flag(engine, "--forbid", needs="project", action="append", default=[],
         help="Forbid writes to a project-relative path or glob. Repeat as needed.")
    flag(engine, "--repair-test", needs="project", dest="repair_tests", action="append", default=[],
         help="An existing test file this run may repair (exact path). Its original cases must "
              "still run unskipped; every other original test is restored. Repeat as needed.")
    flag(engine, "--test-changes", needs="project", metavar="FILE",
         help="Operator JSON approval for exact existing test cases intentionally replaced or retired "
              "under this goal. Planned changes still require approval of the resulting diff "
              "unless its exact current_sha256 is preapproved.")

    solo = parser.add_argument_group(
        "baseline",
        "Measure the pipeline against one model working alone: the same project, goal, checks, "
        "limits and trust choices, one write-granted call, and nothing judged.",
    )
    flag(
        solo, "--baseline", needs="project", metavar="MODEL", default=None,
        help=(
            "Run MODEL alone on the goal in --project instead of the session engine (a roster key "
            "of an unrestricted seat, for example claude:opus). It implies write access to the "
            "project: the model edits your files, with or without --allow-writes. It gets one "
            "call, then your checks; no orchestrator, reviewer, worker or close-out call is made. "
            "The goal is not judged and the run never reports complete: exit status 0 means only "
            "that it ran to its end and its last check passed, 1 that it stopped, 2 that its last "
            "check failed or no check ran. Reads --check, --extra-check, --readiness, --path, "
            "--forbid, --state-dir, the run limits, --lead-max-turns and the trust flags; any "
            "other engine flag is refused. Compare runs with tools/compare_runs.py."
        ),
    )
    flag(
        solo, "--fix-rounds", needs="project", type=_whole(0), default=None, metavar="N",
        help=(
            "With --baseline: how many more calls a failing check may buy, each with the check's "
            "output (default 1, the one fix round a lead gets; 0 for none). Only a failure the "
            "application can repair buys one."
        ),
    )

    limits = parser.add_argument_group(
        "run limits",
        "Bound a --project run the way the GUI's Run limits form does. Give none and the run "
        "has no shared allowance, as before. Give any and --max-calls, --max-reported-tokens "
        "and --wall-seconds are all required: a bounded run is bounded on all three. Token "
        "counts are what each CLI reports after a call returns, so the token limits are stop "
        "thresholds, not hard ceilings and not an invoice. The allowance is printed before the "
        "first call and saved as run-limits.json.",
    )
    flag(limits, "--max-calls", needs="project", metavar="N",
         help="At most N model calls in total; every retry is a call.")
    flag(limits, "--max-reported-tokens", needs="project", metavar="N",
         help="No call starts once the CLIs have reported N tokens in total. A call already "
              "running can pass it.")
    flag(limits, "--wall-seconds", needs="project", metavar="N",
         help="No call starts after N seconds and each call gets only the time left. Checks and "
              "captures between calls are not cut off.")
    flag(limits, "--max-concurrent-workers", needs="project", metavar="N",
         help=f"How many worker errands may run at once (default {RunLimits.max_concurrent_workers}).")
    flag(limits, "--reserve-tokens-per-call", needs="project", metavar="N",
         help="A call starts only while at least N tokens of the total remain, after what the "
              "calls still running were started on (default 0: no pre-call check).")
    flag(limits, "--max-tokens-per-call", needs="project", metavar="N",
         help="Stop the run after a single call reports more than N tokens; it cannot cut that "
              "call short (default 0: none). Without --lead-max-turns it also derives a lead "
              f"turn cap, never below {MIN_LEAD_TURNS} rounds.")
    flag(limits, "--lead-max-turns", needs="engine", type=_whole(1), default=None, metavar="N",
         help="Round limit for each lead or reviewer call, on the CLIs that have a turn flag "
              "(Claude, Grok; Codex has none). Outranks the cap derived from "
              "--max-tokens-per-call. A capped lead keeps its edits and its task is re-planned; "
              "a capped review counts as no verdict. Also works with --session.")

    knobs = parser.add_argument_group(
        "session settings",
        "Engine settings that had no flag. Each default is the value every run has used.",
    )
    flag(knobs, "--recent-entries", needs="engine", type=_whole(1), default=None, metavar="N",
         help="Show the orchestrator and each lead only the last N ledger entries (default: "
              "all), so every prompt re-sends less. The prompt says how many earlier entries it "
              "left out. Nothing is deleted, but the summaries and artifact ids of those entries "
              "are no longer in front of the model.")
    flag(knobs, "--max-fix-cycles", needs="engine", type=_whole(1), default=None, metavar="N",
         help="How many lead revisions a task may spend on blocking review findings, the first "
              "revision included (default 2). Findings still open after the last are recorded "
              "as open, never dropped.")
    flag(knobs, "--max-consults", needs="engine", type=_whole(1), default=None, metavar="N",
         help="How many CONSULT questions a lead may put to brain-trust members per task "
              "(default 2).")
    flag(knobs, "--max-parallel-tasks", needs="project", type=_whole(1), default=None, metavar="N",
         help="How many independent tasks may run at once (default 3; 1 turns parallel batches "
              "off). Needs --allow-writes: tasks run in parallel only when edits are enabled.")
    flag(knobs, "--no-spread-leads", needs="engine", action="store_true",
         help="Keep each simple or standard task on the lead the difficulty ladder names, "
              "instead of moving it to the vendor that has led the fewest tasks this run.")
    flag(knobs, "--no-design-cross-check", needs="engine", action="store_true",
         help="Drop the reviewer from another vendor that design and UI tasks otherwise get. "
              "The rendered evidence and its checks are still required; only the independent "
              "design review goes.")

    watch = parser.add_argument_group(
        "monitor",
        "Read what the engine is doing from the run directory and exit. "
        "Nothing is started, stopped or written.",
    )
    flag(
        watch, "--monitor", monitor=True, action="store_true",
        help=(
            "Print the latest run's status for --project (or the running cell of "
            "--series): live or not, run id, task, stage, seat, tokens against "
            "the cap, last event, and a finished run's stop reason and report."
        ),
    )
    flag(
        watch, "--series", needs="monitor", metavar="DIR",
        help=(
            "A Stage B series directory (manifest.json and run.lock). Supplies the "
            "running cell's project and the packet's token limits to --monitor."
        ),
    )
    flag(
        watch, "--calls", needs="monitor", action="store_true",
        help=(
            "With --monitor: also print every call of the run (task, role, seat, input, output, "
            "cached input, model turns, tool steps, input per turn, seconds) under the by-role summary."
        ),
    )
    flag(watch, "--history", needs="monitor", type=int, default=None, metavar="N",
         help="How many recent runs --monitor lists (default 10).")
    flag(
        watch, "--serve", needs="monitor", type=int, nargs="?", const=7861, default=None, metavar="PORT",
        help=(
            "With --monitor: keep running and serve the status as a read-only web "
            "page on 127.0.0.1 (default port 7861) that reloads itself. Loopback "
            "only, and it answers only to 127.0.0.1 and localhost: to open it on a "
            "phone use `tailscale serve --bg PORT` (or a tunnel) and admit that "
            "address's host name with --allow-host."
        ),
    )
    flag(
        watch, "--allow-host", needs="monitor", action="append", default=[], metavar="NAME",
        help=(
            "With --monitor --serve: a host name the page also answers to, such as the "
            "Tailscale or tunnel name a phone uses (repeatable). The name alone admits any "
            "port; name:port admits that port only. Give no http:// and no path. Adds to "
            "QUADRATUS_MONITOR_ALLOW_HOST."
        ),
    )
    flag(
        watch, "--allow-project", needs="monitor", action="append", default=[], metavar="DIR",
        help=(
            "With --monitor --serve: a project folder or series directory the page may also "
            "be pointed at with ?project= or ?series= (repeatable). Any other path gets one "
            "fixed refusal. Adds to QUADRATUS_MONITOR_ALLOW_PROJECT."
        ),
    )
    flag(parser, "-v", "--verbose", monitor=True, action="store_true", help="Enable debug logging.")
    return parser


def main(argv: Optional[List[str]] = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.WARNING,
        format="%(levelname)s %(name)s: %(message)s",
    )

    # Nothing is opened, configured or called until the line is one the
    # chosen run would honour in full.
    problem = _misplaced(parser, args)
    if problem:
        parser.error(problem)
    for name, value in _DEFAULTS.items():
        if getattr(args, name) is None:
            setattr(args, name, value)

    if args.monitor:
        return _run_monitor(args)

    if args.policy_preview:
        if args.clone or args.branch or args.probe or args.probe_all or args.status:
            parser.error("Policy preview cannot be combined with clone, branch, probe or status")
        import json

        from .policy import preview_policy
        try:
            # Each only when given. --no-declared-checks is read too: the
            # preview is of the run this line would start, and that run would
            # not have the project's own check.
            # A baseline always edits, so its preview is of a writing run.
            plan = preview_policy(args.project, args.declared_paths, forbid=args.forbid,
                                  writing=args.allow_writes or bool(args.baseline),
                                  **({"trust_project_policy": args.trust_project_policy}
                                     if args.trust_project_policy else {}),
                                  **({"declared_checks": False} if args.no_declared_checks else {}))
        except (ValueError, OSError) as exc:
            print(f"Error: {exc}")
            return 1
        print(json.dumps(plan, indent=2))
        return 1 if plan['blocked'] else 0

    running = not (args.probe or args.probe_all or args.status)
    if running:
        if not args.prompt:
            parser.error("a prompt is required (or use --status / --probe)")
        if args.max_tasks < 1:
            parser.error("--max-tasks must be at least 1")
        if args.session or args.project:
            problem = _launch_problem(args)
            if problem:
                parser.error(problem)

    from .config import SettingsError
    try:
        settings = _build_settings(args)
    except SettingsError as exc:
        print(f"Error: invalid setting: {exc}", file=sys.stderr)
        return 2
    if args.probe or args.probe_all:
        return _run_probe(settings, everything=args.probe_all)

    if args.status:
        orchestrator = Orchestrator(settings)
        print(_c("Provider status:", Fore.CYAN, True))
        for line in orchestrator.status_lines():
            print(f"  - {line}")
        print(_c("\nModel aliases:", Fore.CYAN, True))
        for line in _alias_lines():
            print(f"  - {line}")
        withheld = _withheld_line()
        if withheld:
            print(_c("\nChild environment:", Fore.CYAN, True))
            print(f"  - {withheld}")
        return 0

    if args.session or args.project:
        return _run_session(args.prompt, args, settings)

    orchestrator = Orchestrator(settings)

    if not orchestrator.available:
        print(_c("Error: no LLM providers available.", Fore.RED, True))
        for line in orchestrator.status_lines():
            print(f"  - {line}")
        print(
            "On the default subscription transport, that usually means the "
            "vendor CLI is not installed or not signed in: run `quadratus "
            "--probe` to see which. To use billed API keys instead, set "
            "LLM_BACKEND=api and the matching key."
        )
        return 1

    print(_c("Active collaborators:", Fore.CYAN, True))
    for p in orchestrator.available:
        print(f"  - {p.status}")

    def progress(msg: str) -> None:
        print(_c(f"\n>> {msg}", Fore.YELLOW))

    try:
        result = orchestrator.run(args.prompt, progress=progress)
    except ProviderError as exc:
        print(_c(f"\nError: {exc}", Fore.RED, True))
        return 1

    if args.show_stages:
        for stage in result.stages[:-1]:
            print(_c(f"\n=== {stage.label} — {stage.role} ===", Fore.MAGENTA, True))
            print(stage.content)

    print(_c("\n=== Final Solution ===", Fore.GREEN, True))
    print(_c(f"(synthesised by {result.final_provider})\n", Fore.GREEN))
    print(result.final)

    if args.output:
        _save(result, args.output)
        print(_c(f"\nFinal solution written to {args.output}", Fore.GREEN))

    return 0


def _alias_lines():
    """What every roster model resolves to today, and on whose authority."""
    from .latest import resolution_source
    from .registry import ROSTER

    lines = []
    for spec in ROSTER:
        alias, source = resolution_source(spec.key)
        lines.append(f"{spec.key} -> {alias} ({source})")
    return lines


def _save(result: CollaborationResult, filename: str) -> None:
    with open(filename, "w", encoding="utf-8") as f:
        f.write(f"# Task\n\n{result.task}\n\n")
        f.write(f"# Final Solution (synthesised by {result.final_provider})\n\n")
        f.write(result.final)
        f.write("\n\n# Stages\n\n")
        for stage in result.stages:
            f.write(f"## {stage.label} — {stage.role}\n\n{stage.content}\n\n")


if __name__ == "__main__":
    sys.exit(main())

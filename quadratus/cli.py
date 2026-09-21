"""Command-line interface for the multi-LLM workflow."""

from __future__ import annotations

import argparse
import logging
import sys
from pathlib import Path
from typing import List, Optional

from .config import Settings
from .orchestrator import CollaborationResult, Orchestrator
from .providers import ProviderError
from .registry import MODE_ROSTERS

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
    if args.rounds is not None:
        settings.rounds = max(1, args.rounds)
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
    if args.project:
        from .project import Project
        from .project_run import run_project
        try:
            project = Project.open(args.project, clone_url=args.clone or '', branch=args.branch or '')
            result = run_project(
                goal, project, settings, allow_writes=args.allow_writes,
                check=args.check or '', state_dir=args.state_dir,
                forbid=args.forbid, declared_paths=args.declared_paths,
                max_tasks=args.max_tasks, mode=args.mode,
                progress=lambda message: print(f">> {message}", flush=True),
                ask_operator=lambda question: input(f"\n{question}\n> ").strip(),
                plan_gate=(lambda plan: print(plan) is None and
                           input("Run this plan? [y/N] ").lower() in ('y', 'yes')) if args.plan_gate else None,
            )
        except (ValueError, ProviderError, OSError) as exc:
            print(f"Error: {exc}")
            return 1
        print(result.report)
        print(f"\nSource diff: {result.run_dir / 'changes.diff'}")
        if args.output:
            Path(args.output).write_text(result.report, encoding='utf-8')
        return 0 if result.completed else 1

    import shlex

    from .artifacts import ArtifactStore
    from .codebase_map import CodebaseMap
    from .integration import IntegrationGate
    from .routing import OrchestratorUnavailable
    from .runtime import Fleet, new_session
    from .session import OperatorInputNeeded, RunStalled, SessionConfig
    from .usage import UsageMeter

    state = Path(args.state_dir or ".quadratus")
    state.mkdir(parents=True, exist_ok=True)

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

    gate = None
    if args.check:
        gate = IntegrationGate(shlex.split(args.check), cwd=Path.cwd())

    config = SessionConfig(
        mode=args.mode,
        codebase_map=CodebaseMap(state / "codebase-map.jsonl"),
        plan_gate=plan_gate if args.plan_gate else None,
        ask_operator=ask_operator,
        integration_gate=gate,
        progress=progress,
    )

    fleet = Fleet(settings, usage_meter=meter, allow_writes=args.allow_writes)
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

    if not summaries:
        print(_c("\nNothing ran.", Fore.YELLOW))
        return 0

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
    return 0


def main(argv: Optional[List[str]] = None) -> int:
    parser = argparse.ArgumentParser(
        description=(
            "Quadratus: Claude, ChatGPT and Grok collaborate on one coding task "
            "(plan, consensus, build-and-debate, synthesis), on your own "
            "subscriptions via the vendor coding-agent CLIs, or on billed APIs."
        )
    )
    parser.add_argument("prompt", nargs="?", help="The coding task to solve.")
    parser.add_argument("-o", "--output", help="Write the final solution to this file.")
    parser.add_argument(
        "--rounds", type=int, default=None, help="Number of review/refinement rounds."
    )
    parser.add_argument("--claude-model", help="Override the Claude model ID.")
    parser.add_argument("--openai-model", help="Override the OpenAI model ID.")
    parser.add_argument("--grok-model", help="Override the Grok model ID.")
    parser.add_argument(
        "--show-stages",
        action="store_true",
        help="Print every intermediate stage, not just the final answer.",
    )
    parser.add_argument(
        "--status",
        action="store_true",
        help="Print provider availability and exit. Costs nothing.",
    )
    parser.add_argument(
        "--probe",
        action="store_true",
        help=(
            "Ask each installed CLI which model aliases it actually accepts, "
            "cache the answers, and exit. Spends a small amount of "
            "subscription budget: one short call per model."
        ),
    )
    parser.add_argument(
        "--probe-all",
        action="store_true",
        help="Probe every model in the roster, not just the seats a run needs.",
    )

    engine = parser.add_argument_group(
        "session engine",
        "The orchestrated loop: a persistent orchestrator decomposes the goal "
        "into sized tasks and routes each one, instead of running four fixed "
        "phases across every provider.",
    )
    engine.add_argument("--project", "--repo", help="Project folder to open or create. Enables the session engine.")
    engine.add_argument("--clone", metavar="GITHUB_URL", help="Clone a GitHub repository into --project before running.")
    engine.add_argument("--branch", help="Create and switch to a new branch in --project before running.")
    engine.add_argument(
        "--session",
        action="store_true",
        help="Run the session engine instead of the collaboration pipeline.",
    )
    engine.add_argument(
        "--max-tasks",
        type=int,
        default=20,
        help=(
            "Runaway backstop, not a quality gate (default 20). A loop that "
            "has not converged by then has a problem the cap will not fix."
        ),
    )
    engine.add_argument(
        "--state-dir",
        default=None,
        help=(
            "Where artifacts, the cross-session codebase map and the usage log "
            "live (default .quadratus)."
        ),
    )
    engine.add_argument(
        "--mode",
        default="adversarial",
        choices=sorted(MODE_ROSTERS),
        help="Which models participate (default adversarial: the brain trust).",
    )
    engine.add_argument(
        "--plan-gate",
        action="store_true",
        help=(
            "Show the full expected task list and wait for approval before "
            "any window is spent. Reviewing the plan is reviewing the work at "
            "a fraction of the cost."
        ),
    )
    engine.add_argument(
        "--check",
        metavar="CMD",
        help=(
            "The project's own check command, run after each task's work is "
            "final (e.g. --check 'pytest -q'). The deterministic half of "
            "'do the pieces fit together'."
        ),
    )
    engine.add_argument(
        "--allow-writes",
        action="store_true",
        help=(
            "Let the agents write files. Off by default: several models in one "
            "tree is write-thrash, and a reviewer asked to critique will edit."
        ),
    )
    engine.add_argument("--policy-preview", action="store_true",
                        help="Show the resolved policy without running models or checks.")
    engine.add_argument("--path", dest="declared_paths", action="append", default=[],
                        help="Declare a project-relative task path. Repeat for multiple paths.")
    engine.add_argument("--forbid", action="append", default=[],
                        help="Forbid writes to a project-relative path or glob. Repeat as needed.")
    parser.add_argument("-v", "--verbose", action="store_true", help="Enable debug logging.")
    args = parser.parse_args(argv)

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.WARNING,
        format="%(levelname)s %(name)s: %(message)s",
    )

    if (args.policy_preview or args.forbid or args.declared_paths) and not args.project:
        parser.error("--policy-preview, --path and --forbid require --project")
    if args.policy_preview:
        if args.clone or args.branch or args.probe or args.probe_all or args.status:
            parser.error("Policy preview cannot be combined with clone, branch, probe or status")
        import json

        from .policy import preview_policy
        try:
            plan = preview_policy(args.project, args.declared_paths, forbid=args.forbid,
                                  writing=args.allow_writes)
        except (ValueError, OSError) as exc:
            print(f"Error: {exc}")
            return 1
        print(json.dumps(plan, indent=2))
        return 1 if plan['blocked'] else 0

    settings = _build_settings(args)
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
        return 0

    if not args.prompt:
        parser.error("a prompt is required (or use --status / --probe)")

    if args.clone or args.branch or args.allow_writes or args.check:
        if not args.project:
            parser.error("--clone, --branch, --allow-writes and --check require --project")
    if args.max_tasks < 1:
        parser.error("--max-tasks must be at least 1")
    if (args.session or args.project) and any((args.claude_model, args.openai_model, args.grok_model)):
        parser.error("Session model overrides use QUADRATUS_ALIAS_<VENDOR>_<SEAT>; vendor flags only select pipeline models.")
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

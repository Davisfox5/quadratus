"""The command line for the session architecture: ``multi-llm``.

This is the glue that turns the tested machine into a thing you run: it
builds real CLI-backed providers, wires them into a
:class:`~multi_llm.session.Session` with every subsystem attached -- artifact
store, codebase map, session log, usage meter, integration gate, plan gate,
operator channel, completion judge -- and drives a run from your terminal.

Two starting points, one loop:

* **Greenfield** -- ``multi-llm "build me X"`` interviews you into a goal
  and starts from nothing.
* **Existing codebase** -- run it inside a repository and the deterministic
  scan (:mod:`multi_llm.repo_scan`) seeds the codebase map, shortens the
  interview, and auto-configures the integration gate from the project's own
  check command. Same loop; the difference is entirely in seeding.

All session state lives under ``<repo>/.multi_llm/`` -- artifacts, map,
ledger log, usage log. A run interrupted mid-way resumes from the ledger log
by default; ``--fresh`` archives the old log and starts over.

Subcommands: ``run`` (the default), ``plan`` (decompose without executing),
``probe`` (verify the installed CLIs answer), ``scoreboard`` (reviewer
quality from the artifact record).
"""

from __future__ import annotations

import argparse
import logging
import shlex
import sys
import threading
from pathlib import Path
from typing import Dict, List, Optional

from .artifacts import ArtifactStore
from .cli_providers import cli_provider_classes
from .codebase_map import CodebaseMap
from .integration import IntegrationGate
from .interview import InterviewAborted, conduct_interview
from .persistence import SessionLog
from .probes import (
    binary_available,
    probe_models,
    render_probe_report,
)
from .product_map import ProductMap, build_units, run_survey, survey_estimate
from .registry import CONTROL_PLANE
from .repo_scan import scan_repo, seed_map
from .scoreboard import build_scoreboard, render_scoreboard
from .session import OperatorInputNeeded, RunStalled, Session, SessionConfig
from .usage import UsageMeter

log = logging.getLogger(__name__)

__all__ = ["main"]

_SUBCOMMANDS = ("run", "plan", "probe", "scoreboard")

#: Standing rules every terminal-driven run carries. Small on purpose: rules
#: are re-emitted on every render, so each one taxes every prompt forever.
_BASE_INVARIANTS = [
    "This system runs on the operator's personal subscriptions; work stays "
    "on this machine and nothing is sent anywhere the operator did not name.",
]

_EXISTING_CODEBASE_INVARIANT = (
    "The work concerns an existing codebase. The product map is the "
    "standing reference: ground every task in it, fetch the relevant "
    "section before changing an area, and never rely on a section marked "
    "STALE -- issue a comprehend task to resurvey it first. Where the map "
    "is silent, put a comprehension task in an earlier wave so the change "
    "is made with the code understood rather than guessed at."
)


def _build_invoke(repo: Path, *, meter: UsageMeter, timeout: float):
    """The real ``(model_key, prompt) -> reply`` callable, CLI-backed.

    Providers are cached per (vendor, alias, writes) and run *in the
    repository* -- a lead that cannot see the code cannot improve it. Writes
    stay opt-in per call, which is how a NEED TOOL regrant arrives.

    Metering happens here rather than via ``UsageMeter.wrap`` so that
    measured token counts (the Claude CLI reports real ones) win over
    character estimates whenever the CLI provides them.
    """
    classes = cli_provider_classes()
    cache: Dict[tuple, object] = {}
    cache_lock = threading.Lock()

    def invoke(model_key: str, prompt: str, *, system: Optional[str] = None,
               allow_writes: bool = False) -> str:
        vendor, _, alias = model_key.partition(":")
        if vendor not in classes:
            raise RuntimeError(f"no CLI backend for vendor {vendor!r}")
        key = (vendor, alias, bool(allow_writes))
        with cache_lock:
            provider = cache.get(key)
            if provider is None:
                provider = classes[vendor](
                    alias,
                    workdir=str(repo),
                    allow_writes=allow_writes,
                    timeout=timeout,
                )
                cache[key] = provider
        kwargs = {"system": system} if system else {}
        reply = provider.generate(prompt, **kwargs)
        try:
            usage = getattr(provider, "last_usage", None)
            if usage:
                meter.record(
                    model=model_key,
                    input_tokens=usage.get("input_tokens"),
                    output_tokens=usage.get("output_tokens"),
                )
            else:
                meter.record(model=model_key, prompt=prompt, reply=reply)
        except Exception:  # noqa: BLE001 -- metering must never fail a call
            pass
        return reply

    return invoke


def _terminal_ask(question: str) -> str:
    print(f"\n[the run needs your answer]\n{question}")
    return input("> ").strip()


def _terminal_plan_gate(plan: str) -> bool:
    print("\n=== Proposed plan (nothing has run yet) ===\n")
    print(plan)
    answer = input("\nProceed with this plan? [y/N] ").strip().lower()
    return answer in ("y", "yes")


def _prepare(args) -> dict:
    """Everything ``run`` and ``plan`` share: scan, wiring, goal, session."""
    repo = Path(args.repo).resolve()
    if not repo.is_dir():
        raise SystemExit(f"not a directory: {repo}")
    if not binary_available("claude"):
        raise SystemExit(
            "the 'claude' CLI is not on PATH. The orchestrator is a hard "
            "dependency: install the CLI, sign in with your subscription, "
            "and re-run. (`multi-llm probe` checks the whole fleet.)"
        )

    state = repo / ".multi_llm"
    scan = scan_repo(repo)
    codebase_map = CodebaseMap(state / "map.jsonl")
    if scan.has_code:
        seeded = seed_map(scan, codebase_map)
        if seeded:
            print(f"Scanned existing codebase; seeded {seeded} map note(s).")

    meter = UsageMeter(state / "usage.jsonl")
    invoke = _build_invoke(repo, meter=meter, timeout=args.timeout)
    session_log = SessionLog(state / "ledger.jsonl")
    interactive = sys.stdin.isatty()

    # The goal: flag beats stored beats interview. A resumed session reuses
    # the goal it was recorded under -- the ledger only makes sense against
    # the goal its entries were written toward.
    goal = (args.goal or "").strip()
    resuming = session_log.exists() and not args.fresh
    if not goal and resuming:
        goal = session_log.stored_goal() or ""
        if goal:
            print("Resuming under the stored goal.")
    if not goal:
        wish = (args.wish or "").strip()
        if not wish:
            raise SystemExit(
                "nothing to do: give a wish (multi-llm \"improve the error "
                "handling\") or --goal for a pre-written goal paragraph."
            )
        if interactive:
            print("A few questions before anything is spent.")
            try:
                goal = conduct_interview(
                    wish, invoke=invoke, ask=_terminal_ask,
                    context=scan.summary(),
                )
            except InterviewAborted as exc:
                raise SystemExit(f"interview ended without a goal: {exc}") from exc
            print(f"\nGoal:\n{goal}\n")
        else:
            # No terminal to interview through: the wish plus the scan is the
            # goal, stated honestly rather than a hung prompt.
            goal = wish if not scan.summary() else f"{wish}\n\n{scan.summary()}"

    if args.fresh and session_log.exists():
        rotated = session_log.rotate()
        print(f"Archived previous session log to {rotated.name}.")

    gate = None
    check_cmd = shlex.split(args.check) if args.check else scan.check_command
    if check_cmd:
        gate = IntegrationGate(check_cmd, cwd=repo)
        print(f"Integration gate: {' '.join(check_cmd)}")

    store = ArtifactStore(state / "artifacts")
    available = lambda key: binary_available(key.split(":", 1)[0])  # noqa: E731

    # The product map: no project work starts on an existing codebase until
    # it has been surveyed into a verified, operator-reviewable reference.
    # Incremental by fingerprint -- a repeat run on an unchanged repo skips
    # straight through.
    product_map = None
    if scan.has_code and not args.no_survey:
        product_map = ProductMap(
            state / "product_map.json", root=repo,
            md_path=state / "product_map.md",
        )
        stale = product_map.stale_units(build_units(repo))
        if stale:
            calls = survey_estimate(len(stale))
            print(
                f"\nProduct map: {len(stale)} area(s) need surveying "
                f"(~{calls} model calls, parallel across your four "
                f"subscriptions)."
            )
            if interactive:
                go = input("Run the survey now? [Y/n] ").strip().lower()
                if go in ("n", "no"):
                    raise SystemExit(
                        "stopped: the survey is required before project "
                        "work. Re-run when ready, or pass --no-survey to "
                        "run without a product map."
                    )
            written = run_survey(
                repo, invoke=invoke, store=store, product_map=product_map,
                available=available, progress=lambda m: print(f"  {m}"),
            )
            print(
                f"Product map: {written} section(s) written and "
                f"cross-vendor verified -> {product_map.md_path}"
            )
            if interactive and not args.no_map_gate:
                print(
                    "\nReview the product map before any project work: "
                    f"{product_map.md_path}"
                )
                approve = input(
                    "Approve the product map? [y/N] "
                ).strip().lower()
                if approve not in ("y", "yes"):
                    raise SystemExit(
                        "product map not approved; nothing has been built. "
                        "Re-run after reviewing (the survey will not be "
                        "re-paid for unchanged code)."
                    )

    invariants = list(_BASE_INVARIANTS)
    if scan.has_code:
        invariants.append(_EXISTING_CODEBASE_INVARIANT)

    config = SessionConfig(
        codebase_map=codebase_map,
        integration_gate=gate,
        ask_operator=_terminal_ask if interactive else None,
        plan_gate=(
            _terminal_plan_gate
            if interactive and not args.no_plan_gate else None
        ),
        done_judge=CONTROL_PLANE["convergence"],
        session_log=session_log,
        product_map=product_map,
    )
    session = Session(
        goal,
        store,
        invoke,
        config=config,
        invariants=invariants,
        available=available,
    )
    if resuming:
        restored = session.restore(session_log)
        if restored:
            print(f"Resumed: {restored} completed task(s) restored from the log.")
    else:
        session_log.record_goal(goal)
    return {"session": session, "meter": meter, "session_log": session_log}


def _cmd_run(args) -> int:
    parts = _prepare(args)
    session, meter = parts["session"], parts["meter"]
    try:
        summaries = session.run(max_tasks=args.max_tasks)
    except OperatorInputNeeded as exc:
        print(
            "\nThe run is waiting on questions only you can answer:\n"
            f"{exc.args[0]}\n\nAnswer by re-running -- the session resumes "
            "from its log and will re-ask."
        )
        return 2
    except RunStalled as exc:
        print(f"\nThe run stalled rather than burn the window:\n{exc}")
        return 3
    finally:
        print("\n" + meter.render_report())

    print(f"\nRun complete: {len(summaries)} task(s) this session.")
    open_questions = [
        q for s in summaries for q in s.open_questions
    ]
    if open_questions:
        print("Open questions carried in the record:")
        for q in open_questions:
            print(f"  - {q}")
    return 0


def _cmd_plan(args) -> int:
    parts = _prepare(args)
    print("\n=== Plan (nothing executed) ===\n")
    print(parts["session"].plan())
    return 0


def _cmd_probe(_args) -> int:
    print("Probing every model the system routes to (one cheap call each)...")
    results = probe_models()
    print(render_probe_report(results))
    return 0 if all(r.ok for r in results) else 1


def _cmd_scoreboard(args) -> int:
    store_root = Path(args.repo).resolve() / ".multi_llm" / "artifacts"
    print(render_scoreboard(build_scoreboard(store_root)))
    return 0


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="multi-llm",
        description="Multi-model coding system on consumer subscriptions.",
    )
    sub = parser.add_subparsers(dest="command")

    def _common(p):
        p.add_argument("--repo", default=".",
                       help="Repository to work in (default: current directory).")
        p.add_argument("--goal", default="",
                       help="Pre-written goal paragraph; skips the interview.")
        p.add_argument("--check", default="",
                       help="Integration check command (default: auto-detected).")
        p.add_argument("--timeout", type=float, default=600.0,
                       help="Per-invocation CLI timeout in seconds.")
        p.add_argument("--fresh", action="store_true",
                       help="Archive the previous session log and start over.")
        p.add_argument("--no-plan-gate", action="store_true",
                       help="Skip the plan approval prompt.")
        p.add_argument("--no-map-gate", action="store_true",
                       help="Skip the product map approval prompt.")
        p.add_argument("--no-survey", action="store_true",
                       help="Run without building the product map (not "
                            "recommended on an existing codebase).")
        p.add_argument("wish", nargs="?", default="",
                       help="What you want, in a sentence; the interview "
                            "makes it concrete.")

    run_p = sub.add_parser("run", help="Interview, plan, and run (the default).")
    _common(run_p)
    run_p.add_argument("--max-tasks", type=int, default=20,
                       help="Runaway backstop on tasks per session.")
    run_p.set_defaults(func=_cmd_run)

    plan_p = sub.add_parser("plan", help="Show the decomposition; execute nothing.")
    _common(plan_p)
    plan_p.set_defaults(func=_cmd_plan)

    probe_p = sub.add_parser("probe", help="Verify the installed CLIs answer.")
    probe_p.set_defaults(func=_cmd_probe)

    board_p = sub.add_parser("scoreboard", help="Reviewer quality from the record.")
    board_p.add_argument("--repo", default=".")
    board_p.set_defaults(func=_cmd_scoreboard)
    return parser


def main(argv: Optional[List[str]] = None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    # `multi-llm "improve the tests"` means run -- the wish is not a
    # subcommand, and making people type `run` first would be ceremony.
    if argv and argv[0] not in _SUBCOMMANDS and not argv[0].startswith("-"):
        argv = ["run", *argv]
    elif not argv:
        argv = ["run"]
    args = _parser().parse_args(argv)
    if not hasattr(args, "func"):
        args = _parser().parse_args(["run"])
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    try:
        return args.func(args)
    except KeyboardInterrupt:
        print("\nInterrupted. State is on disk; re-run to resume from the log.")
        return 130


if __name__ == "__main__":
    raise SystemExit(main())

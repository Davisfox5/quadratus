"""Command-line interface for the multi-LLM workflow."""

from __future__ import annotations

import argparse
import logging
import sys
from typing import List, Optional

from .config import Settings
from .orchestrator import CollaborationResult, Orchestrator
from .providers import ProviderError

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
    if args.claude_model:
        settings.claude_model = args.claude_model
    if args.openai_model:
        settings.openai_model = args.openai_model
    if args.gemini_model:
        settings.gemini_model = args.gemini_model
    if args.grok_model:
        settings.grok_model = args.grok_model
    return settings


def main(argv: Optional[List[str]] = None) -> int:
    parser = argparse.ArgumentParser(
        description=(
            "Quadratus: Claude, ChatGPT, Gemini and Grok collaborate on one coding task "
            "(plan, consensus, build-and-debate, synthesis), on billed APIs or your own "
            "subscriptions via the vendor coding-agent CLIs."
        )
    )
    parser.add_argument("prompt", nargs="?", help="The coding task to solve.")
    parser.add_argument("-o", "--output", help="Write the final solution to this file.")
    parser.add_argument(
        "--rounds", type=int, default=None, help="Number of review/refinement rounds."
    )
    parser.add_argument("--claude-model", help="Override the Claude model ID.")
    parser.add_argument("--openai-model", help="Override the OpenAI model ID.")
    parser.add_argument("--gemini-model", help="Override the Gemini model ID.")
    parser.add_argument("--grok-model", help="Override the Grok model ID.")
    parser.add_argument(
        "--show-stages",
        action="store_true",
        help="Print every intermediate stage, not just the final answer.",
    )
    parser.add_argument(
        "--status",
        action="store_true",
        help="Print provider availability and exit.",
    )
    parser.add_argument("-v", "--verbose", action="store_true", help="Enable debug logging.")
    args = parser.parse_args(argv)

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.WARNING,
        format="%(levelname)s %(name)s: %(message)s",
    )

    settings = _build_settings(args)
    orchestrator = Orchestrator(settings)

    if args.status:
        print(_c("Provider status:", Fore.CYAN, True))
        for line in orchestrator.status_lines():
            print(f"  - {line}")
        return 0

    if not args.prompt:
        parser.error("a prompt is required (or use --status)")

    if not orchestrator.available:
        print(_c("Error: no LLM providers available.", Fore.RED, True))
        for line in orchestrator.status_lines():
            print(f"  - {line}")
        print("Set at least one API key in your environment or .env file.")
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

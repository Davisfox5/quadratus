"""A model driving a real browser, one observed action at a time.

:mod:`multi_llm.browser` produces passive evidence -- load a page, report what
happened. This module is the active half: a model (the *pilot*) is given a
goal and a live page, and drives it through a strict action protocol --
navigate, click, fill, read -- with the harness executing every action in
Playwright and feeding the observation back. The model never touches the
browser; it emits JSON, the harness acts, and everything both of them did is
recorded.

Who pilots
----------
Two grades, chosen by the caller, because the evidence on small-model agency
is consistent: sub-frontier models execute *scripted, predictable* flows fine
and fall apart on flows that require judgement (recovering from an unexpected
dialog, deciding which of three similar buttons is the right one). So:

* **Complex flows** -- exercising a new UI, exploratory testing, anything
  where the next step depends on understanding the page -- get the frontend
  policy's pinned lead, a brain-trust-grade model.
* **Routine automation** -- a known click-path being re-run as a check --
  gets a cheap model. Recognising that a task is an automation rather than a
  judgement call, and downshifting, is one of the cheapest ways to save the
  expensive models' windows.

The protocol is the harness doing the discipline so the pilot does not have
to be trusted with it: one action per turn, structured JSON only (parsed by
the same forgiving ladder the rest of the system uses), a hard step budget,
and the same repeated-action rule as everywhere else -- a pilot that emits
the identical action twice in a row is looping on a known outcome and is
stopped, not indulged.
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional

from .registry import Capability, models_for
from .structured import StructuredError, extract_json
from .task_kinds import DIFFICULTY_LADDER

log = logging.getLogger(__name__)

__all__ = ["PilotStep", "PilotRun", "pilot_model", "drive"]

#: Visible-text excerpt cap per observation. Enough to act on, not enough to
#: flood the pilot's context with page boilerplate.
READ_CHARS = 2000


def pilot_model(*, routine: bool = False) -> str:
    """Which model should hold the controls.

    Complex flows take the top rung of the difficulty ladder -- the same
    judgement the router applies to any hard task, reused rather than
    duplicated. Routine automation takes the first cheap coder in the roster;
    if the roster someday carries no cheap coder, the complex pilot flies
    everything rather than nobody flying.
    """
    complex_pilot = DIFFICULTY_LADDER["complex"]
    if not routine:
        return complex_pilot
    cheap = [m for m in models_for(Capability.CHEAP) if Capability.CODE in m.caps]
    return cheap[0].key if cheap else complex_pilot


@dataclass(frozen=True)
class PilotStep:
    """One action the pilot took and what the page did in response."""

    action: Dict[str, Any]
    observation: str


@dataclass
class PilotRun:
    """The full record of a piloted browser session."""

    goal: str
    pilot: str
    steps: List[PilotStep] = field(default_factory=list)
    console_errors: List[str] = field(default_factory=list)
    final_url: str = ""
    screenshot_path: str = ""
    #: True only when the pilot itself declared the goal met.
    success: bool = False
    #: Why the run ended: done | aborted | step-budget | repeated-action.
    ended: str = ""

    def render(self) -> str:
        """The run as evidence for a reviewer or a close-out."""
        lines = [
            f"Browser run: {self.goal}",
            f"- pilot: {self.pilot}",
            f"- outcome: {'success' if self.success else 'incomplete'} ({self.ended})",
            f"- final url: {self.final_url}",
            f"- steps: {len(self.steps)}",
        ]
        if self.screenshot_path:
            lines.append(f"- final screenshot: {self.screenshot_path}")
        if self.console_errors:
            lines.append("- console errors:")
            lines += [f"    {e}" for e in self.console_errors]
        for i, step in enumerate(self.steps):
            lines.append(f"  {i + 1}. {json.dumps(step.action)}")
        return "\n".join(lines)


_PROTOCOL = """You are driving a real browser toward a goal, one action per reply.
Reply with a single JSON object and nothing else. Actions:
  {"action": "goto", "url": "..."}
  {"action": "click", "selector": "<css selector>"}
  {"action": "fill", "selector": "<css selector>", "text": "..."}
  {"action": "press", "selector": "<css selector>", "key": "Enter"}
  {"action": "read"}                       -- visible text of the page
  {"action": "screenshot"}                 -- capture the current state
  {"action": "wait", "ms": 500}
  {"action": "done", "reason": "..."}      -- the goal is met; say how you know
  {"action": "abort", "reason": "..."}     -- the goal cannot be met; say why
Rules: verify, don't assume -- read the page before declaring done. If an
action fails, change the action; never repeat one that failed verbatim."""


def _observe(page, note: str) -> str:
    return f"{note}\nurl: {page.url}\ntitle: {page.title()!r}"


def _execute(page, action: Dict[str, Any]) -> str:
    """Run one action. Failures come back as observations, not exceptions:
    what the page did wrong is exactly what the pilot needs to see next."""
    kind = action.get("action")
    try:
        if kind == "goto":
            page.goto(action["url"])
            return _observe(page, "navigated")
        if kind == "click":
            page.click(action["selector"], timeout=5000)
            return _observe(page, f"clicked {action['selector']}")
        if kind == "fill":
            page.fill(action["selector"], action.get("text", ""), timeout=5000)
            return _observe(page, f"filled {action['selector']}")
        if kind == "press":
            page.press(action["selector"], action.get("key", "Enter"), timeout=5000)
            return _observe(page, f"pressed {action.get('key', 'Enter')}")
        if kind == "read":
            text = page.inner_text("body")[:READ_CHARS]
            return f"visible text (truncated):\n{text}"
        if kind == "wait":
            page.wait_for_timeout(min(int(action.get("ms", 500)), 5000))
            return _observe(page, "waited")
        return f"unknown action {kind!r}; use one of the listed actions"
    except KeyError as exc:
        return f"action {kind!r} is missing required field {exc}"
    except Exception as exc:  # playwright errors are the pilot's feedback
        return f"action failed: {str(exc)[:300]}"


def drive(
    invoke: Callable[[str, str], str],
    *,
    goal: str,
    start_url: str,
    out_dir,
    routine: bool = False,
    model: Optional[str] = None,
    max_steps: int = 12,
    executable_path: Optional[str] = None,
) -> PilotRun:
    """Let a model drive a page toward ``goal``. Returns the full record.

    Args:
        invoke: ``(model_key, prompt) -> reply``, same shape as everywhere.
        goal: What the pilot is trying to accomplish or verify.
        start_url: URL or local file path to open first.
        out_dir: Where screenshots land.
        routine: True for known click-paths; picks the cheap pilot.
        model: Override the pilot choice entirely.
        max_steps: Hard budget. A flow that needs more is either complex
            enough to deserve a bigger explicit budget, or looping.
        executable_path: Optional pinned Chromium (else MULTI_LLM_CHROMIUM).
    """
    import os

    try:
        from playwright.sync_api import sync_playwright
    except ImportError as exc:
        from .browser import PlaywrightMissing
        raise PlaywrightMissing(
            "browser piloting needs playwright: pip install playwright"
        ) from exc

    pilot = model or pilot_model(routine=routine)
    out = Path(out_dir)
    out.mkdir(parents=True, exist_ok=True)
    run = PilotRun(goal=goal, pilot=pilot)

    path = Path(start_url)
    url = start_url if "://" in start_url else path.resolve().as_uri()
    executable_path = executable_path or os.environ.get("MULTI_LLM_CHROMIUM") or None

    with sync_playwright() as pw:
        launch_kwargs = {"executable_path": executable_path} if executable_path else {}
        browser = pw.chromium.launch(**launch_kwargs)
        try:
            page = browser.new_page()
            page.on(
                "console",
                lambda msg: run.console_errors.append(msg.text)
                if msg.type == "error" else None,
            )
            page.on("pageerror", lambda err: run.console_errors.append(str(err)))
            page.goto(url)

            observation = _observe(page, "opened start page")
            previous_action: Optional[str] = None
            shots = 0

            for _ in range(max_steps):
                history = "\n".join(
                    f"- {json.dumps(s.action)} -> {s.observation.splitlines()[0]}"
                    for s in run.steps[-6:]
                )
                reply = invoke(
                    pilot,
                    f"{_PROTOCOL}\n\nGoal: {goal}\n\n"
                    f"Recent actions:\n{history or '- none yet'}\n\n"
                    f"Latest observation:\n{observation}\n\n"
                    f"Goal, restated: {goal}\nNext action:",
                )
                try:
                    action = extract_json(reply, required_keys=["action"])
                except StructuredError:
                    observation = (
                        "your reply was not a single JSON action object; "
                        "reply with exactly one action from the list"
                    )
                    run.steps.append(PilotStep(
                        {"action": "invalid-reply"}, observation
                    ))
                    continue

                kind = action.get("action")
                if kind in ("done", "abort"):
                    run.success = kind == "done"
                    run.ended = kind
                    run.steps.append(PilotStep(action, action.get("reason", "")))
                    break

                fingerprint = json.dumps(action, sort_keys=True)
                if fingerprint == previous_action:
                    # The same rule as workers and the run loop: repeating an
                    # identical action is looping on a known outcome.
                    run.ended = "repeated-action"
                    run.steps.append(PilotStep(
                        action, "stopped: identical action twice in a row"
                    ))
                    break
                previous_action = fingerprint

                if kind == "screenshot":
                    shot = out / f"step-{len(run.steps)}.png"
                    page.screenshot(path=str(shot))
                    shots += 1
                    observation = f"screenshot saved: {shot}"
                else:
                    observation = _execute(page, action)
                run.steps.append(PilotStep(action, observation))
            else:
                run.ended = "step-budget"

            run.final_url = page.url
            final = out / "final.png"
            page.screenshot(path=str(final), full_page=True)
            run.screenshot_path = str(final)
        finally:
            browser.close()

    return run

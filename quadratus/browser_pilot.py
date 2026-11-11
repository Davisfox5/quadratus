"""A model driving a real browser, one observed action at a time.

:mod:`quadratus.browser` produces passive evidence -- load a page, report what
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
from typing import Any, Callable, Dict, List, Optional, Sequence
from urllib.parse import urlparse

from .config import env_with_legacy
from .registry import Capability, models_for
from .structured import StructuredError, extract_json
from .task_kinds import DIFFICULTY_LADDER

log = logging.getLogger(__name__)

__all__ = ["PilotStep", "PilotRun", "pilot_model", "drive", "held_to"]

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
    #: Why the run ended: done | aborted | step-budget | repeated-action |
    #: outside-pinning (something ran that request pinning does not cover).
    ended: str = ""
    #: Navigations and requests the harness refused because they left what
    #: the run is held to, bounded like the console errors.
    blocked: List[str] = field(default_factory=list)

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
        if self.blocked:
            lines.append("- refused outside the run's origin:")
            lines += [f"    {b}" for b in self.blocked]
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
action fails, change the action; never repeat one that failed verbatim.
The browser is held to where this run started (stated below): a goto to any
other address, a file: URL included, is refused, and a page that leaves it is
closed."""

#: Bounds on one action and one navigation, and on what an observation
#: carries of a page's own strings.
ACTION_TIMEOUT_MS = 5_000
NAVIGATION_TIMEOUT_MS = 15_000
_URL_CHARS, _TITLE_CHARS = 500, 200


def _origin(url: str):
    """``(scheme, host, port)``; a URL that cannot be read has no origin
    and so matches none."""
    try:
        parsed = urlparse(url)
        return parsed.scheme, parsed.hostname, parsed.port
    except ValueError:
        return None, None, None


def held_to(start_url: str, root=None, allow_origins: Sequence[str] = ()):
    """What a run may open, as ``(predicate, label)``, or ValueError.

    The capture's own origin rule (``design_evidence._navigation_rule``): a
    local preview is held to its origin, a local file to the files under
    ``root`` (the start page's own folder when no root is given). Any other
    start is refused unless the caller names its origin in
    ``allow_origins``, which also admits those origins during the run. A
    model chooses none of this (review 2026-11-10, checks 12: the pilot
    opened whatever URL its reply named, ``file:///home/.../id_rsa``
    included, and read the page back into the next prompt)."""
    from .design_evidence import _LOCAL_HOSTS, _file_url_path, _navigation_rule
    extra = set()
    for origin in allow_origins:
        parsed = urlparse(str(origin))
        if parsed.scheme not in ("http", "https") or not parsed.hostname:
            raise ValueError(f"allow_origins takes http or https origins, not {str(origin)[:80]!r}")
        extra.add((parsed.scheme, parsed.hostname, parsed.port))
    if "://" not in start_url or start_url.startswith("file://"):
        page = _file_url_path(start_url) if start_url.startswith("file://") else Path(start_url)
        if page is None:
            raise ValueError(f"the pilot cannot start on {start_url[:120]!r}")
        base = Path(root) if root is not None else page.resolve().parent
        try:
            rule = _navigation_rule(start_url, base)
        except ValueError:
            raise ValueError(f"the pilot's start page must be a file under {base}") from None
        label = f"files under {base.resolve()}"
    else:
        start = _origin(start_url)
        if start[0] not in ("http", "https") or (start[1] not in _LOCAL_HOSTS and start not in extra):
            raise ValueError("the pilot is held to a local preview (http://localhost or 127.0.0.1) or a file "
                             "in the project; a caller that means another origin names it in allow_origins")

        def rule(url: str) -> bool:
            return _origin(url) == start
        label = f"{start[0]}://{start[1]}" + (f":{start[2]}" if start[2] else "")

    def allowed(url: str) -> bool:
        return rule(url) or _origin(url) in extra
    if extra:
        label += " and " + ", ".join(sorted(f"{s}://{h}" + (f":{p}" if p else "") for s, h, p in extra))
    return allowed, label


def _observe(page, note: str) -> str:
    return f"{note}\nurl: {page.url[:_URL_CHARS]}\ntitle: {page.title()[:_TITLE_CHARS]!r}"


def _off_origin(page, allowed) -> bool:
    return page.url not in ("", "about:blank") and not allowed(page.url)


def _execute(page, action: Dict[str, Any], allowed=None, label: str = "") -> str:
    """Run one action. Failures come back as observations, not exceptions:
    what the page did wrong is exactly what the pilot needs to see next."""
    kind = action.get("action")
    try:
        if kind == "goto":
            url = str(action["url"])
            if allowed is not None and not allowed(url):
                return (f"navigation refused: {url[:200]} is outside what this run is held to ({label}); "
                        "no other address, and no file: URL outside it, is opened")
            page.goto(url, timeout=NAVIGATION_TIMEOUT_MS)
            return _observe(page, "navigated")
        if kind == "click":
            page.click(action["selector"], timeout=ACTION_TIMEOUT_MS)
            return _observe(page, f"clicked {action['selector']}")
        if kind == "fill":
            page.fill(action["selector"], action.get("text", ""), timeout=ACTION_TIMEOUT_MS)
            return _observe(page, f"filled {action['selector']}")
        if kind == "press":
            page.press(action["selector"], action.get("key", "Enter"), timeout=ACTION_TIMEOUT_MS)
            return _observe(page, f"pressed {action.get('key', 'Enter')}")
        if kind == "read":
            outside = "the page is outside what this run is held to; its text is not read"
            if allowed is not None and _off_origin(page, allowed):
                return outside
            text = page.inner_text("body", timeout=ACTION_TIMEOUT_MS)[:READ_CHARS]
            # Checked again once the text is in hand: a navigation that
            # landed while it was being read must not hand over the new page.
            if allowed is not None and _off_origin(page, allowed):
                return outside
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
    root=None,
    allow_origins: Sequence[str] = (),
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
        executable_path: Optional pinned Chromium (else QUADRATUS_CHROMIUM).
        root: The project folder a file start page must be under; the
            start page's own folder when not given.
        allow_origins: Origins beyond the start page's that the caller
            means the run to reach, as ``http(s)://host[:port]``.

    The browser is held to the start page's origin the way a harness
    capture is (:func:`held_to`, ``browser.hold_to_origin``): every request
    off it is failed before it is sent, a new window is refused, and a page
    that lands outside it is closed before anything is read from it.

    Raises:
        ValueError: when ``start_url`` is neither a local preview, a file
            under ``root``, nor an origin named in ``allow_origins``.
    """

    try:
        from playwright.sync_api import sync_playwright
    except ImportError as exc:
        from .browser import PlaywrightMissing
        raise PlaywrightMissing(
            "browser piloting needs playwright: pip install playwright"
        ) from exc
    from .browser import (
        _Events,
        _seen,
        _watch_targets,
        hold_to_origin,
        pinned_launch_args,
        unpinned_note,
    )
    from .integration import write_bound

    allowed, label = held_to(start_url, root, allow_origins)
    pilot = model or pilot_model(routine=routine)
    out = Path(out_dir)
    out.mkdir(parents=True, exist_ok=True)
    run = PilotRun(goal=goal, pilot=pilot)

    path = Path(start_url)
    url = start_url if "://" in start_url else path.resolve().as_uri()
    executable_path = executable_path or env_with_legacy("QUADRATUS_CHROMIUM", "MULTI_LLM_CHROMIUM") or None
    errors, blocked, failed, strangers = _Events(), _Events(), _Events(), []

    def shoot(name: str, **options) -> str:
        # As bytes, written without following a link at the name.
        write_bound(out, (name,), page.screenshot(**options))
        return str(out / name)

    with sync_playwright() as pw:
        launch_kwargs = {"executable_path": executable_path} if executable_path else {}
        browser = pw.chromium.launch(args=pinned_launch_args(), **launch_kwargs)
        try:
            watch = _watch_targets(browser, strangers)
            context = browser.new_context(service_workers="block", accept_downloads=False)
            page = context.new_page()
            page.on(
                "console",
                lambda msg: errors.append(msg.text)
                if msg.type == "error" else None,
            )
            page.on("pageerror", lambda err: errors.append(str(err)))
            hold_to_origin(context, page, allowed, pin_requests=True, blocked=blocked, failed_requests=failed)
            page.set_default_timeout(ACTION_TIMEOUT_MS)
            page.set_default_navigation_timeout(NAVIGATION_TIMEOUT_MS)
            page.goto(url, timeout=NAVIGATION_TIMEOUT_MS)

            observation = _observe(page, "opened start page")
            previous_action: Optional[str] = None

            for _ in range(max_steps):
                history = "\n".join(
                    f"- {json.dumps(s.action)} -> {s.observation.splitlines()[0]}"
                    for s in run.steps[-6:]
                )
                reply = invoke(
                    pilot,
                    f"{_PROTOCOL}\n\nThis run is held to: {label}\n\nGoal: {goal}\n\n"
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

                before = _seen(blocked)
                if kind == "screenshot":
                    observation = f"screenshot saved: {shoot(f'step-{len(run.steps)}.png')}"
                else:
                    observation = _execute(page, action, allowed, label)
                if _off_origin(page, allowed):
                    # Where a click or a redirect took the page, not only
                    # where a goto asked to go: nothing is read from it.
                    left = page.url[:200]
                    try:
                        page.goto("about:blank", timeout=NAVIGATION_TIMEOUT_MS)
                    except Exception:  # noqa: BLE001 -- the next action reports the page's state
                        pass
                    observation = (f"the page left what this run is held to ({left}) and was closed; "
                                   "use goto to return")
                elif _seen(blocked) > before and blocked:
                    observation += "\nblocked outside this run's origin: " + blocked[-1][:200]
                run.steps.append(PilotStep(action, observation))
            else:
                run.ended = "step-budget"

            run.final_url = page.url[:_URL_CHARS]
            run.screenshot_path = shoot("final.png", full_page=True)
            # Nothing may have run whose requests the page's session does
            # not pause: such a run is no evidence, whatever the pilot said.
            watch()
            if strangers:
                run.success = False
                run.ended = "outside-pinning"
        finally:
            browser.close()

    run.console_errors = list(errors)
    run.blocked = list(blocked) + list(failed) + [unpinned_note(kind, where) for kind, where in strangers]
    return run

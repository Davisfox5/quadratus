"""Deterministic browser evidence for frontend work.

The frontend policy in :mod:`quadratus.task_kinds` already states the rule --
a frontend change that compiles is not a frontend change that works -- and
the research behind it found the harness (a render-and-look loop) moves
frontend outcomes more than model choice does. This module is that harness
piece: load the page in a real browser, capture what actually happened, and
hand back evidence a model or an operator can judge.

Design constraints, in order:

* **Deterministic and dumb.** No model in the loop. This produces evidence
  (screenshot, console errors, failed requests, title); judging the evidence
  is the reviewers' job. A checker with opinions would be a third reviewer
  with the worst eyesight.
* **Optional dependency.** Playwright is imported lazily and its absence is a
  clear error at the call site, not an import-time crash for every user who
  never runs frontend tasks.
* **Evidence is artifacts.** Screenshots land on disk and the report carries
  paths, matching the store's rule that a summary points at the original.
"""

from __future__ import annotations

import json
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Callable, List, Optional

from .config import env_with_legacy

__all__ = ["PageEvidence", "render_page", "PlaywrightMissing"]


class PlaywrightMissing(RuntimeError):
    """Playwright is not installed, so no browser evidence can be produced."""


@dataclass(frozen=True)
class PageEvidence:
    """What actually happened when the page loaded. Facts, not judgement."""

    url: str
    title: str
    screenshot_path: str
    #: Uncaught exceptions and console.error output, verbatim.
    console_errors: List[str] = field(default_factory=list)
    #: Requests that failed or returned >=400, as "STATUS url".
    failed_requests: List[str] = field(default_factory=list)
    #: True when the page produced no errors and no failed requests. A
    #: convenience for gates; the lists are the actual evidence.
    clean: bool = True
    #: Interaction steps run before the screenshot, each with its outcome:
    #: ``{"n", "action", "selector", "ok", "error"?, "file"?}``. Empty when
    #: the page was captured at load.
    steps: List[dict] = field(default_factory=list)

    def render(self) -> str:
        """One block for a prompt or a close-out."""
        lines = [
            f"Browser evidence for {self.url}",
            f"- title: {self.title!r}",
            f"- screenshot: {self.screenshot_path}",
        ]
        if self.console_errors:
            lines.append("- console errors:")
            lines += [f"    {e}" for e in self.console_errors]
        if self.failed_requests:
            lines.append("- failed requests:")
            lines += [f"    {r}" for r in self.failed_requests]
        if self.clean:
            lines.append("- no console errors, no failed requests")
        return "\n".join(lines)


def render_page(
    target: str,
    *,
    out_dir,
    wait_ms: int = 1500,
    viewport: Optional[dict] = None,
    executable_path: Optional[str] = None,
    steps: Optional[List[dict]] = None,
    allow_navigation: Optional[Callable[[str], bool]] = None,
    step_timeout_ms: int = 5000,
    deadline: Optional[float] = None,
) -> PageEvidence:
    """Load ``target`` in headless Chromium and capture the evidence.

    Args:
        target: A URL, or a path to a local HTML file (converted to file://).
        out_dir: Where the screenshot and evidence JSON are written.
        wait_ms: Settling time after load for late console errors -- frontend
            frameworks routinely throw after ``load`` fires, which is exactly
            the failure a compile step cannot see.
        viewport: Optional ``{"width": ..., "height": ...}``.
        executable_path: Optional Chromium binary path, for environments that
            pin their own build.
        steps: Optional interaction before the screenshot, run in order:
            ``{"action": "click"|"wait", "selector": ...}`` or
            ``{"action": "file", "selector": ..., "path": <absolute path>}``.
            The first failure stops the rest; every outcome is recorded. No
            script evaluation, typing or navigation step exists.
        allow_navigation: With ``steps``, a predicate on each navigation URL
            in any frame or window; a refused navigation is aborted and fails
            the step that caused it. With ``steps``, a new window or tab is
            always refused and closed.
        deadline: With ``steps``, launch, load, waits, steps and screenshot
            are all clamped to the time left; none can outlast it.
        step_timeout_ms: Per-step bound.

    Raises:
        PlaywrightMissing: when the optional dependency is not installed.
    """
    try:
        from playwright.sync_api import sync_playwright
    except ImportError as exc:
        raise PlaywrightMissing(
            "browser evidence needs playwright: pip install playwright "
            "(and ensure a Chromium is available to it)"
        ) from exc

    if executable_path is None:
        # Environments that pin their own Chromium (CI images, sandboxes) name
        # it here rather than re-downloading Playwright's copy.
        executable_path = env_with_legacy("QUADRATUS_CHROMIUM", "MULTI_LLM_CHROMIUM") or None

    out = Path(out_dir)
    out.mkdir(parents=True, exist_ok=True)

    path = Path(target)
    url = target if "://" in target else path.resolve().as_uri()

    console_errors: List[str] = []
    failed_requests: List[str] = []

    interactive = bool(steps)
    budget = _Budget(deadline if interactive else None)
    budget.require("before the browser started")

    with sync_playwright() as pw:
        launch_kwargs = {}
        if executable_path:
            launch_kwargs["executable_path"] = executable_path
        if budget.active:
            launch_kwargs["timeout"] = budget.ms(30_000)
        browser = pw.chromium.launch(**launch_kwargs)
        try:
            context = browser.new_context(viewport=viewport)
            page = context.new_page()
            page.on(
                "console",
                lambda msg: console_errors.append(msg.text)
                if msg.type == "error" else None,
            )
            page.on("pageerror", lambda err: console_errors.append(str(err)))
            page.on(
                "requestfailed",
                lambda req: failed_requests.append(
                    f"FAILED {req.url} ({req.failure})"
                ),
            )
            page.on(
                "response",
                lambda res: failed_requests.append(f"{res.status} {res.url}")
                if res.status >= 400 else None,
            )
            blocked: List[str] = []
            if interactive:
                # Guarded at the context, so a popup's own navigation is
                # caught too; and any new window is refused outright (Codex
                # review of c222d62: a target=_blank link reached another
                # loopback port and the capture still passed).
                def guard(route):
                    request = route.request
                    if (request.is_navigation_request() and allow_navigation is not None
                            and not allow_navigation(request.url)):
                        blocked.append(request.url)
                        route.abort()
                    else:
                        route.continue_()
                context.route("**/*", guard)

                def refuse_popup(new_page):
                    blocked.append("a new window or tab: " + (new_page.url or "about:blank"))
                    try:
                        new_page.close()
                    except Exception:  # noqa: BLE001 -- it may already be gone
                        pass
                context.on("page", refuse_popup)
                page.set_default_timeout(budget.ms(30_000))
                page.set_default_navigation_timeout(budget.ms(30_000))
            page.goto(url, **({"timeout": budget.ms(30_000)} if budget.active else {}))
            page.wait_for_timeout(budget.ms(wait_ms) if budget.active else wait_ms)
            records = _run_steps(page, steps or [], blocked, step_timeout_ms, deadline)
            if records:
                seen = len(blocked)
                page.wait_for_timeout(budget.ms(min(wait_ms, 500)))
                if len(blocked) > seen and records[-1].get("ok"):
                    # A navigation or window that fired after the last step
                    # still leaves the evidence invalid.
                    records[-1]["ok"] = False
                    records[-1]["error"] = ("navigation outside the preview was blocked after this step: "
                                            + blocked[-1][:200])
            title = page.title()
            shot = out / "page.png"
            page.screenshot(path=str(shot), full_page=True,
                            **({"timeout": budget.ms(30_000)} if budget.active else {}))
        finally:
            browser.close()

    evidence = PageEvidence(
        url=url,
        title=title,
        screenshot_path=str(shot),
        console_errors=console_errors,
        failed_requests=failed_requests,
        clean=not console_errors and not failed_requests,
        steps=records,
    )
    (out / "evidence.json").write_text(
        json.dumps(asdict(evidence), indent=2), encoding="utf-8"
    )
    return evidence


class _Budget:
    """What remains of an interactive capture's time, for every blocking call.

    ``ms(cap)`` is ``cap`` clamped to the remaining time; once nothing
    remains it raises TimeoutError instead of returning 0, which Playwright
    would read as "no timeout". Inactive (no deadline) it returns ``cap``.
    """

    def __init__(self, deadline: Optional[float]):
        self.deadline = deadline
        self.active = deadline is not None

    def remaining_ms(self) -> Optional[int]:
        if not self.active:
            return None
        return int((self.deadline - time.monotonic()) * 1000)

    def require(self, when: str) -> None:
        left = self.remaining_ms()
        if left is not None and left <= 0:
            raise TimeoutError(f"capture time limit reached {when}")

    def ms(self, cap: int) -> int:
        left = self.remaining_ms()
        if left is None:
            return cap
        if left <= 0:
            raise TimeoutError("capture time limit reached")
        return max(1, min(cap, left))


def _run_steps(page, steps, blocked, timeout_ms, deadline) -> List[dict]:
    """Run interaction steps in order; stop at the first failure.

    Every step is recorded with its outcome, so a failure names the step and
    selector that did not happen instead of surfacing as a generic timeout.
    """
    records = []
    for n, step in enumerate(steps, 1):
        action, selector = step["action"], step["selector"]
        record = {"n": n, "action": action, "selector": selector, "ok": False}
        if action == "file":
            record["file"] = step.get("label") or Path(step["path"]).name
        records.append(record)
        budget = _Budget(deadline)
        left = budget.remaining_ms()
        if left is not None and left <= 0:
            record["error"] = "capture time limit reached before this step"
            break
        limit = budget.ms(timeout_ms)
        before = len(blocked)
        try:
            if action == "click":
                page.click(selector, timeout=limit)
                page.wait_for_timeout(min(200, budget.ms(200)))  # let a started navigation reach the guard
            elif action == "wait":
                page.wait_for_selector(selector, state="visible", timeout=limit)
            elif action == "file":
                page.set_input_files(selector, step["path"], timeout=limit)
            else:
                raise ValueError(f"unknown step action {action!r}")
        except Exception as exc:  # noqa: BLE001 -- a failed step is evidence, not a crash
            record["error"] = str(exc).strip().splitlines()[0][:300] if str(exc).strip() else type(exc).__name__
            break
        if len(blocked) > before:
            record["error"] = "navigation outside the preview was blocked: " + blocked[-1][:200]
            break
        record["ok"] = True
    return records

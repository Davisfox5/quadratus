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
        allow_navigation: With ``steps``, a predicate on each main-frame
            navigation URL; a refused navigation is aborted and fails the
            step that caused it.
        step_timeout_ms: Per-step bound.
        deadline: ``time.monotonic()`` value past which no further step runs.

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

    with sync_playwright() as pw:
        launch_kwargs = {}
        if executable_path:
            launch_kwargs["executable_path"] = executable_path
        browser = pw.chromium.launch(**launch_kwargs)
        try:
            page = browser.new_page(viewport=viewport)
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
            if steps and allow_navigation is not None:
                def guard(route):
                    request = route.request
                    if (request.is_navigation_request() and request.frame == page.main_frame
                            and not allow_navigation(request.url)):
                        blocked.append(request.url)
                        route.abort()
                    else:
                        route.continue_()
                page.route("**/*", guard)
            page.goto(url)
            page.wait_for_timeout(wait_ms)
            records = _run_steps(page, steps or [], blocked, step_timeout_ms, deadline)
            if records:
                page.wait_for_timeout(min(wait_ms, 500))
            title = page.title()
            shot = out / "page.png"
            page.screenshot(path=str(shot), full_page=True)
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
        if deadline is not None and time.monotonic() > deadline:
            record["error"] = "capture time limit reached before this step"
            break
        before = len(blocked)
        try:
            if action == "click":
                page.click(selector, timeout=timeout_ms)
                page.wait_for_timeout(200)   # let a navigation the click started reach the guard
            elif action == "wait":
                page.wait_for_selector(selector, state="visible", timeout=timeout_ms)
            elif action == "file":
                page.set_input_files(selector, step["path"], timeout=timeout_ms)
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

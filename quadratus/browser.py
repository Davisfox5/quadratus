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
    #: ``{"n", "action", "selector", "ok", "error"?, "file"?}``, and on a
    #: wait ``visible_before_steps`` (its target was already showing before
    #: the first step ran). Empty when the page was captured at load.
    steps: List[dict] = field(default_factory=list)
    #: The document's width at the screenshot, and when it is wider than the
    #: viewport, up to five outermost elements past the right edge as
    #: ``{"element", "right", "width"}``. Measured by the harness, not a step.
    document_width: Optional[int] = None
    overflow: List[dict] = field(default_factory=list)

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
    pin_requests: bool = False,
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

    interactive = bool(steps) or allow_navigation is not None
    budget = _Budget(deadline if bool(steps) else None)
    budget.require("before the browser started")

    with sync_playwright() as pw:
        launch_kwargs = {}
        if executable_path:
            launch_kwargs["executable_path"] = executable_path
        if budget.active:
            launch_kwargs["timeout"] = budget.ms(30_000)
        browser = pw.chromium.launch(**launch_kwargs)
        try:
            # A service worker is its own target, outside the page's request
            # interception below, so a pinned capture allows none.
            context = browser.new_context(viewport=viewport, **(
                {"service_workers": "block"} if pin_requests else {}))
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
                if pin_requests and allow_navigation is not None:
                    # The harness's own capture (Codex reviews of 7a8c432,
                    # d731499 and 06ad58c). Chromium's own request
                    # interception pauses every request before it is sent,
                    # including each hop of a redirect chain (which a
                    # Playwright route never sees), and the browser still
                    # does its own networking, so a single-threaded preview
                    # keeps working. A request off the origin is failed
                    # before it leaves: nothing reaches another service.
                    try:
                        cdp = context.new_cdp_session(page)
                    except Exception as exc:  # noqa: BLE001 -- fail closed, named
                        raise RuntimeError(f"request pinning is unavailable in this browser: {exc}") from exc

                    def paused(event):
                        request_id, url = event["requestId"], event["request"]["url"]
                        if url.startswith(("data:", "blob:", "about:")) or allow_navigation(url):
                            cdp.send("Fetch.continueRequest", {"requestId": request_id})
                            return
                        navigation = event.get("resourceType") == "Document"
                        if event.get("redirectedRequestId"):
                            note = "redirect to " + url + " refused"
                        else:
                            note = url if navigation else "blocked outside the preview: " + url
                        (blocked if navigation else failed_requests).append(note)
                        cdp.send("Fetch.failRequest", {"requestId": request_id, "errorReason": "BlockedByClient"})
                    cdp.on("Fetch.requestPaused", paused)
                    try:
                        cdp.send("Fetch.enable", {"patterns": [{"urlPattern": "*", "requestStage": "Request"}]})
                    except Exception as exc:  # noqa: BLE001 -- fail closed, named
                        raise RuntimeError(f"request pinning is unavailable in this browser: {exc}") from exc

                    def socket_guard(socket):
                        # WebSockets are outside that interception (Codex
                        # review of c1fbab0): an off-origin socket is closed
                        # before any handshake is sent; an on-origin one is
                        # connected through to the app unchanged.
                        target = socket.url.replace("ws://", "http://", 1).replace("wss://", "https://", 1)
                        if allow_navigation(target):
                            socket.connect_to_server()
                            return
                        # Never connected: nothing is sent to any server, and
                        # the page's socket stays unanswered.
                        failed_requests.append("blocked outside the preview: " + socket.url[:200])
                    try:
                        context.route_web_socket("**", socket_guard)
                    except Exception as exc:  # noqa: BLE001 -- fail closed, named
                        raise RuntimeError(f"WebSocket pinning is unavailable in this browser: {exc}") from exc
                else:
                    context.route("**/*", guard)

                def refuse_popup(new_page):
                    blocked.append("a new window or tab: " + (new_page.url or "about:blank"))
                    try:
                        new_page.close()
                    except Exception:  # noqa: BLE001 -- it may already be gone
                        pass
                context.on("page", refuse_popup)

                def check_landing(frame):
                    # A server redirect is followed below the route guard,
                    # so where the main frame actually landed is checked too
                    # (Codex review of 3a55d82: a 302 to another loopback
                    # service was captured as the target).
                    if (frame == page.main_frame and allow_navigation is not None
                            and not allow_navigation(frame.url)):
                        blocked.append("landed on " + frame.url)
                page.on("framenavigated", check_landing)
                page.set_default_timeout(budget.ms(30_000))
                page.set_default_navigation_timeout(budget.ms(30_000))
            try:
                page.goto(url, **({"timeout": budget.ms(30_000)} if budget.active else {}))
            except Exception as exc:  # noqa: BLE001 -- re-raised, named when the guard caused it
                if blocked:
                    raise RuntimeError("navigation outside the preview was blocked: " + blocked[-1][:200]) from exc
                raise
            page.wait_for_timeout(budget.ms(wait_ms) if budget.active else wait_ms)
            if interactive and allow_navigation is not None and not allow_navigation(page.url):
                blocked.append("landed on " + page.url)
            records = _run_steps(page, steps or [], blocked, step_timeout_ms, deadline)
            if blocked and not records:
                # A pinned capture with no steps (the harness's own, Codex
                # review of contract v2): a navigation off the origin leaves
                # the render unclean rather than silently showing its start.
                failed_requests.append("navigation outside the preview was blocked: " + blocked[-1][:200])
            if records:
                seen = len(blocked)
                page.wait_for_timeout(budget.ms(min(wait_ms, 500)))
                if len(blocked) > seen and records[-1].get("ok"):
                    # A navigation or window that fired after the last step
                    # still leaves the evidence invalid.
                    records[-1]["ok"] = False
                    records[-1]["error"] = ("navigation outside the preview was blocked after this step: "
                                            + blocked[-1][:200])
            if pin_requests and allow_navigation is not None:
                # Every rendered frame, not only the main one, must stand on
                # the origin (Codex review of d731499: an iframe hopped away).
                for frame in page.frames:
                    where = frame.url or ""
                    if where and not where.startswith(("about:", "data:", "blob:")) and not allow_navigation(where):
                        failed_requests.append("frame outside the preview: " + where[:200])
            title = page.title()
            document_width, overflow = _measure_overflow(page, (viewport or {}).get("width"))
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
        document_width=document_width,
        overflow=overflow,
    )
    (out / "evidence.json").write_text(
        json.dumps(asdict(evidence), indent=2), encoding="utf-8"
    )
    return evidence


#: Elements examined for overflow. The report names at most five, and the
#: scan itself is bounded so a huge page cannot make the diagnostic costly.
OVERFLOW_SCAN_LIMIT = 5000

_OVERFLOW_SCRIPT = """([viewport, limit]) => {
  const doc = document.documentElement;
  const width = Math.max(doc.scrollWidth, document.body ? document.body.scrollWidth : 0);
  if (!viewport) return {width, offenders: [], scanned: 0};
  const past = new Set();
  const all = document.querySelectorAll('body *');
  const scanned = Math.min(all.length, limit);
  // Content that scrolls inside its own container is contained, not spilled:
  // a wide table in an overflow-x:auto wrapper does not widen the document.
  const contained = el => {
    for (let a = el.parentElement; a && a !== document.body; a = a.parentElement) {
      if (getComputedStyle(a).overflowX !== 'visible') return true;
    }
    return false;
  };
  for (let i = 0; i < scanned; i++) {
    const box = all[i].getBoundingClientRect();
    if (box.width > 0 && (box.right > viewport + 1 || box.left < -1) && !contained(all[i])) past.add(all[i]);
  }
  const outermost = [...past].filter(el => !past.has(el.parentElement));
  const name = el => el.tagName.toLowerCase() + (el.id ? '#' + el.id : '')
    + Array.from(el.classList).slice(0, 2).map(c => '.' + c).join('');
  return {width, scanned, offenders: outermost.slice(0, 5).map(el => {
    const box = el.getBoundingClientRect();
    return {element: name(el), left: Math.round(box.left), right: Math.round(box.right),
            width: Math.round(box.width), side: box.left < -1 ? 'left' : 'right'};
  })};
}"""


def _measure_overflow(page, viewport_width):
    """``(document_width, offenders)`` for the page as it will be captured.

    A harness diagnostic, not an interaction step (Codex, Run 15: the mobile
    render was 470px at a 390px viewport and nothing said what overflowed).
    Elements escaping either edge are named, fixed and sticky ones included,
    outermost only, at most five, from at most ``OVERFLOW_SCAN_LIMIT``
    elements. The width gate itself stays on the screenshot and its
    threshold is unchanged; this only names a target for whoever fixes it.
    A left escape does not widen the screenshot, so it is recorded here but
    does not by itself fail the gate. Never raises.
    """
    try:
        measured = page.evaluate(_OVERFLOW_SCRIPT, [viewport_width, OVERFLOW_SCAN_LIMIT])
        offenders = [dict(element=str(o.get("element"))[:120], side=str(o.get("side")),
                          left=int(o.get("left")), right=int(o.get("right")), width=int(o.get("width")))
                     for o in (measured.get("offenders") or [])][:5]
        return int(measured.get("width")), offenders
    except Exception:  # noqa: BLE001 -- a diagnostic never fails the capture
        return None, []


def _accepts(accept: str, path: str) -> bool:
    """Whether a file input's ``accept`` list admits ``path``, by name and type."""
    import mimetypes
    name = Path(path).name.lower()
    kind = (mimetypes.guess_type(name)[0] or "").lower()
    for token in (t.strip().lower() for t in accept.split(",")):
        if not token:
            continue
        if token.startswith("."):
            if name.endswith(token):
                return True
        elif token.endswith("/*"):
            if kind.startswith(token[:-1]):
                return True
        elif token == kind:
            return True
    return False


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


#: A recorded dialog message is the compared text itself (whitespace
#: collapsed), kept whole up to twice the longest message a declaration may
#: carry: any message that can match is recorded untruncated, and the
#: evidence check compares the record exactly as the step did. The raw text
#: is not what is kept (Codex review of ca0ad65: 700 leading spaces before the
#: declared text matched at the step and left a record of spaces).
_DIALOG_RECORD_CHARS = 600


def _dialog_text(text) -> str:
    """A dialog message as compared: whitespace collapsed, ends trimmed."""
    return " ".join(str(text or "").split())


def _run_steps(page, steps, blocked, timeout_ms, deadline) -> List[dict]:
    """Run interaction steps in order; stop at the first failure.

    Every step is recorded with its outcome, so a failure names the step and
    selector that did not happen instead of surfacing as a generic timeout.
    """
    records = []
    # What each wait's target looked like before anything ran: a final wait
    # on something already showing at load proves nothing about the change
    # (Codex, Run 15: an always-present status element).
    showing = {}
    for step in steps:
        if step.get("action") == "wait" and step.get("selector") not in showing:
            try:
                showing[step["selector"]] = bool(page.is_visible(step["selector"]))
            except Exception:  # noqa: BLE001 -- unknown stays unknown
                showing[step["selector"]] = None
    for n, step in enumerate(steps, 1):
        action, selector = step["action"], step["selector"]
        record = {"n": n, "action": action, "selector": selector, "ok": False}
        if action == "file":
            record["file"] = step.get("label") or Path(step["path"]).name
        if action == "wait":
            record["visible_before_steps"] = showing.get(selector)
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
            elif action == "confirm":
                # A click that opens a browser confirm dialog, answered only
                # when it is the one declared: a confirm whose message holds
                # the declared text is accepted; any other dialog (another
                # type, another message) is dismissed and the step fails; no
                # dialog at all fails the step. Playwright dismisses an
                # unanswered dialog, so a plain click on a delete control
                # cancels the delete and the state the declaration names is
                # never reached (series rule-58a4625 f5: the empty list sits
                # behind a confirm). Never a global auto-accept (Codex,
                # 6038178890): one handler, one click, one dialog.
                expected = _dialog_text(step.get("message"))
                seen: List[dict] = []

                def answer(dialog, seen=seen, expected=expected):
                    # Exactly the declared message, whitespace collapsed: a
                    # longer message that merely contains it is another
                    # operation (Codex review of 4a51291: "Delete project
                    # Alpha? Also delete every other project?" matched). The
                    # permission covers one dialog: the first; every later
                    # one is dismissed and recorded, and fails the step
                    # (Codex review of ca60892: two matching confirms were
                    # both accepted, a prompt after a confirm went unrecorded).
                    # Only a string message is text; anything else is recorded
                    # by repr and never matches (Codex review of ca0ad65: a
                    # non-string coerced to the declared text).
                    typed = isinstance(dialog.message, str)
                    text = _dialog_text(dialog.message) if typed else ""
                    matched = (not seen and dialog.type == "confirm" and bool(expected) and typed
                               and text == expected)
                    entry = dict(type=dialog.type, message=text[:_DIALOG_RECORD_CHARS] if typed else None,
                                 accepted=matched)
                    if not typed:
                        entry["message_repr"] = repr(dialog.message)[:_DIALOG_RECORD_CHARS]
                    elif len(text) > _DIALOG_RECORD_CHARS:
                        entry["truncated"] = True
                    seen.append(entry)
                    if matched:
                        dialog.accept()
                    else:
                        dialog.dismiss()
                # The permission lives exactly as long as this step's click:
                # the handler is removed on every exit (dialog or none, click
                # error, timeout), so a dialog a later step opens can never
                # be answered under this step's declaration (Codex review of
                # ce35fb6: a once handler stays armed until an event arrives).
                page.on("dialog", answer)
                try:
                    page.click(selector, timeout=limit)
                    page.wait_for_timeout(min(200, budget.ms(200)))
                finally:
                    page.remove_listener("dialog", answer)
                if not seen:
                    record["dialog"] = None
                    record["error"] = f"no dialog opened; expected the confirm {expected[:80]!r}"
                    break
                record["dialog"] = seen[0]
                if len(seen) > 1:
                    record["extra_dialogs"] = seen[1:]
                    record["error"] = (f"{len(seen)} dialogs opened where one confirm was declared; the extra "
                                       f"{seen[1]['type']} {str(seen[1]['message'])[:80]!r} was dismissed")
                    break
                if not seen[0]["accepted"]:
                    record["error"] = (f"the dialog did not match: {seen[0]['type']} {str(seen[0]['message'])[:80]!r}; "
                                       f"expected the confirm {expected[:80]!r}; dismissed")
                    break
            elif action == "wait":
                page.wait_for_selector(selector, state="visible", timeout=limit)
            elif action == "file":
                # The input's own accept list, checked before the upload:
                # Run 15 uploaded a Markdown document to a CSV control and
                # met the app's 400, which is the right answer to the wrong file.
                accept = page.get_attribute(selector, "accept", timeout=limit) or ""
                if accept.strip() and not _accepts(accept, step["path"]):
                    raise ValueError(f"the file {Path(step['path']).name} does not match the input's "
                                     f"accept list ({accept.strip()[:80]})")
                page.set_input_files(selector, step["path"], timeout=budget.ms(timeout_ms))
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

"""Rendered evidence for design work: the lead captures it, the harness checks it.

Davis's ruling (2026-09-25): design work is verified by the model that did it,
and cross-checked by another. Codex's review of the first version: asking for
a look is not verification. So the lead captures the page at a desktop and a
mobile width with this module, and the harness checks the files -- real PNGs,
the right widths, written during this task -- before the task can count as
complete. The cross-vendor design reviewer is pointed at the same files.

    python -m quadratus.design_evidence <url-or-html-file> <task-id> [project-root]
        [--click SELECTOR] [--wait SELECTOR] [--upload SELECTOR project/fixture.csv] ...

Steps reach the state the task changed before the screenshot (Codex review
of #25, Run 13: a dialog and its results appear only after a click and a
file choice, so a render at load proves nothing about them). They run in the
order given, each with a bound; every outcome is recorded in summary.json and
any failed step makes the evidence unverified. There is no script, typing or
navigation step. With steps, the page must be a local preview (localhost or
a file inside the project), and navigation off it is blocked. A file step
takes a regular, non-symlinked file inside the project, never under a hidden
directory (.git, .quadratus, .ssh, ...), never a hidden file, and never a
credential-like name. ``--file SELECTOR=path`` is kept for simple selectors;
a selector with ``[`` must use ``--upload``, since its ``=`` is ambiguous.
"""

from __future__ import annotations

import fnmatch
import json
import struct
import sys
import time
from pathlib import Path
from typing import List, Optional, Tuple
from urllib.parse import urlparse
from urllib.request import url2pathname

VIEWPORTS = {"desktop": {"width": 1280, "height": 800}, "mobile": {"width": 390, "height": 844}}
EVIDENCE_DIR = Path(".quadratus") / "design-evidence"


def evidence_dir(root, task_id: str) -> Path:
    return Path(root) / EVIDENCE_DIR / task_id


#: Bounds on an interactive capture: steps, per step, and per capture.
MAX_STEPS = 12
STEP_TIMEOUT_MS = 5000
CAPTURE_SECONDS = 90
_ACTIONS = ("click", "wait", "file")
_LOCAL_HOSTS = {"localhost", "127.0.0.1", "::1"}
#: Never uploaded, whatever the task asks. Any hidden path component is
#: refused outright (run state, VCS data, .env, .ssh, .codex, tool configs);
#: these names are refused anywhere else (Codex review of c222d62:
#: .codex/auth.json and .ssh/id_ecdsa were accepted).
_BLOCKED_NAMES = ("id_*", "*.pem", "*.key", "*.p12", "*.pfx", "*.jks", "*.keystore", "*.gpg", "*.asc",
                  "*.ppk", "*auth*.json", "*credential*", "*secret*", "*token*", "*password*")


def parse_steps(argv: List[str]) -> Tuple[List[str], List[dict]]:
    """Split ``--click/--wait/--upload/--file`` steps from positional arguments.

    ``--upload SELECTOR PATH`` takes two arguments, so neither may need
    escaping. ``--file SELECTOR=PATH`` splits at the first ``=``, which is
    only unambiguous when the selector has no attribute part; one with ``[``
    is refused and pointed at ``--upload``.
    """
    positional, steps = [], []
    items = list(argv)
    while items:
        item = items.pop(0)
        if item == "--upload":
            if len(items) < 2:
                raise ValueError("--upload takes SELECTOR PATH")
            selector, path = items.pop(0), items.pop(0)
            steps.append(dict(action="file", selector=selector, path=path))
        elif item in ("--click", "--wait", "--file"):
            if not items:
                raise ValueError(f"{item} needs a value")
            value = items.pop(0)
            action = item[2:]
            if action == "file":
                selector, sep, path = value.partition("=")
                if not sep:
                    raise ValueError("--file takes SELECTOR=project/relative/path")
                if "[" in selector:
                    raise ValueError("--file cannot split a selector with [ ]; use --upload SELECTOR PATH")
                steps.append(dict(action="file", selector=selector, path=path))
            else:
                steps.append(dict(action=action, selector=value))
        else:
            positional.append(item)
    return positional, steps


def _fixture(root: Path, relative: str) -> Path:
    """A project-owned, non-secret, non-symlinked regular file, or ValueError."""
    raw = Path(relative)
    if not relative or raw.is_absolute() or ".." in raw.parts:
        raise ValueError(f"file step path must be project-relative without '..': {relative!r}")
    if any(part.startswith(".") for part in raw.parts):
        raise ValueError(f"file step path is hidden or under a hidden directory: {relative!r}")
    if any(fnmatch.fnmatch(part.lower(), pattern) for part in raw.parts for pattern in _BLOCKED_NAMES):
        raise ValueError(f"file step path looks like a credential: {relative!r}")
    current = root
    for part in raw.parts:
        current = current / part
        if current.is_symlink():
            raise ValueError(f"file step path goes through a symlink: {relative!r}")
    if not current.is_file() or not current.resolve().is_relative_to(root.resolve()):
        raise ValueError(f"file step path is not a regular file in the project: {relative!r}")
    return current.resolve()


def _navigation_rule(target: str, root: Path):
    """The allowed origin for an interactive capture, as a URL predicate."""
    if "://" not in target or target.startswith("file://"):
        page = _file_url_path(target) if target.startswith("file://") else Path(target)
        if page is None or not page.resolve().is_relative_to(root.resolve()):
            raise ValueError("an interactive capture of a file must use a file inside the project")

        def allowed(url: str) -> bool:
            path = _file_url_path(url)
            return path is not None and path.resolve().is_relative_to(root.resolve())
        return allowed
    parsed = urlparse(target)
    if parsed.scheme not in ("http", "https") or parsed.hostname not in _LOCAL_HOSTS:
        raise ValueError("an interactive capture must target a local preview (http://localhost or 127.0.0.1)")
    origin = (parsed.scheme, parsed.hostname, parsed.port)

    def allowed(url: str) -> bool:
        other = urlparse(url)
        return (other.scheme, other.hostname, other.port) == origin
    return allowed


def _file_url_path(url: str) -> Optional[Path]:
    """The local path a file URL names, percent-decoded, or None.

    Chromium encodes a space as %20, so the raw URL path of a project in
    "My Project" never matched the root (Codex review of c222d62).
    """
    parsed = urlparse(url)
    if parsed.scheme != "file" or parsed.netloc not in ("", "localhost"):
        return None
    return Path(url2pathname(parsed.path))


def validate_steps(steps: List[dict], target: str, root) -> Tuple[List[dict], object]:
    """Checked steps (file paths resolved) and the navigation rule, or ValueError."""
    root = Path(root)
    if len(steps) > MAX_STEPS:
        raise ValueError(f"at most {MAX_STEPS} steps, not {len(steps)}")
    checked = []
    for step in steps:
        action, selector = step.get("action"), step.get("selector")
        if action not in _ACTIONS:
            raise ValueError(f"unknown step {action!r}; use --click, --wait or --file")
        if not isinstance(selector, str) or not selector.strip() or len(selector) > 300:
            raise ValueError("each step needs a selector of at most 300 characters")
        item = dict(action=action, selector=selector.strip())
        if action == "file":
            item.update(path=str(_fixture(root, step.get("path") or "")), label=step.get("path"))
        checked.append(item)
    return checked, (_navigation_rule(target, root) if checked else None)


def capture(target: str, task_id: str, root=".", steps: Optional[List[dict]] = None) -> dict:
    """Render ``target`` at each width into the task's evidence folder.

    With ``steps``, each width runs them on a fresh page before its
    screenshot. Refused steps are written into summary.json and raised, so the
    design check reports why instead of accepting an earlier render.
    """
    from .browser import render_page
    folder = evidence_dir(root, task_id)
    folder.mkdir(parents=True, exist_ok=True)
    # Invalidate before anything can fail: a capture that raises part-way
    # must never leave an earlier, successful summary and screenshots standing
    # as this attempt's evidence (Codex review of c222d62).
    _write_summary(folder, dict(target=target, views={}, capture_in_progress=True))
    for name in VIEWPORTS:
        for leftover in ("page.png", "evidence.json"):
            (folder / name / leftover).unlink(missing_ok=True)
    try:
        checked, allowed = validate_steps(list(steps or []), target, root)
    except ValueError as exc:
        _write_summary(folder, dict(target=target, views={}, steps_refused=str(exc)))
        raise
    deadline = time.monotonic() + CAPTURE_SECONDS if checked else None
    out = {}
    try:
        for name, viewport in VIEWPORTS.items():
            evidence = render_page(target, out_dir=folder / name, viewport=viewport, steps=checked,
                                   allow_navigation=allowed, step_timeout_ms=STEP_TIMEOUT_MS, deadline=deadline)
            out[name] = dict(screenshot=evidence.screenshot_path, clean=evidence.clean,
                             console_errors=evidence.console_errors[:10],
                             failed_requests=evidence.failed_requests[:10])
            if checked:
                out[name]["steps"] = evidence.steps
    except BaseException as exc:
        _write_summary(folder, dict(target=target, views={}, rendered=sorted(out),
                                    capture_failed=f"{type(exc).__name__}: {str(exc)[:300]}"))
        raise
    summary = dict(target=target, views=out)
    if checked:
        summary["steps"] = [{k: v for k, v in s.items() if k != "path"} for s in checked]
    (folder / "summary.json").write_text(json.dumps(summary, indent=2))
    return out


def _write_summary(folder: Path, summary: dict) -> None:
    (folder / "summary.json").write_text(json.dumps(summary, indent=2))


def _step_problem(view: str, requested, done) -> Optional[str]:
    """What is wrong with one view's step records against the request, if anything.

    Fails closed: every record must be a dict with the requested order,
    number, action, selector and file, and ``ok`` exactly True.
    """
    if not isinstance(requested, list) or not all(
            isinstance(r, dict) and r.get("action") in _ACTIONS and isinstance(r.get("selector"), str)
            for r in requested):
        return "the requested interaction steps are malformed"
    if not isinstance(done, list):
        return f"the {view} render has no interaction step record"
    for index, (want, got) in enumerate(zip(requested, done, strict=False)):  # lengths compared below
        if not isinstance(got, dict):
            return f"the {view} render's step {index + 1} record is malformed"
        same = (got.get("n") == index + 1 and got.get("action") == want["action"]
                and got.get("selector") == want["selector"]
                and (want["action"] != "file" or got.get("file") == want.get("label")))
        if not same:
            return (f"the {view} render's step {index + 1} record does not match the requested step "
                    f"({want['action']} {want['selector'][:80]})")
        if got.get("ok") is not True:
            return (f"the {view} render's step {index + 1} ({want['action']} {want['selector'][:80]}) "
                    f"failed: {str(got.get('error'))[:160]}")
    if len(done) != len(requested):
        return f"the {view} render ran {len(done)} of {len(requested)} interaction steps"
    return None


def _png_width(path: Path) -> Optional[int]:
    try:
        with open(path, "rb") as handle:
            head = handle.read(24)
    except OSError:
        return None
    if len(head) < 24 or head[:8] != b"\x89PNG\r\n\x1a\n" or head[12:16] != b"IHDR":
        return None
    return struct.unpack(">I", head[16:20])[0]


def check(root, task_id: str, since: float) -> Tuple[bool, str, list]:
    """Whether the task left fresh, clean desktop and mobile renders of a
    named page. Never raises.

    Clean means no console errors and no failed requests: a render of a
    broken page is evidence that it is broken, not that it works (Codex
    review of #25 rendered a page with a console.error and a missing image,
    and the first version of this check passed it).
    """
    try:
        return _check(root, task_id, since)
    except Exception as exc:  # noqa: BLE001 -- evidence is data; malformed data is a finding
        return False, f"the evidence could not be read ({type(exc).__name__}: {str(exc)[:160]})", []


def _check(root, task_id: str, since: float) -> Tuple[bool, str, list]:
    folder = evidence_dir(root, task_id)
    problems, shots = [], []
    try:
        summary = json.loads((folder / "summary.json").read_text())
    except (OSError, ValueError):
        summary = None
    if isinstance(summary, dict) and summary.get("capture_failed"):
        return False, f"the last capture failed: {str(summary['capture_failed'])[:200]}", []
    if isinstance(summary, dict) and summary.get("capture_in_progress"):
        return False, "the last capture did not finish", []
    if isinstance(summary, dict) and summary.get("steps_refused"):
        return False, f"the capture's interaction steps were refused: {str(summary['steps_refused'])[:200]}", []
    if (not isinstance(summary, dict) or not isinstance(summary.get("target"), str) or not summary["target"]
            or not isinstance(summary.get("views"), dict)
            or set(summary["views"]) != set(VIEWPORTS)
            or not all(isinstance(v, dict) for v in summary["views"].values())):
        return False, "no well-formed summary.json naming the rendered page (use python -m quadratus.design_evidence)", []
    for name, view in summary["views"].items():
        if not view.get("clean"):
            errors = "; ".join(str(e)[:120] for e in (view.get("console_errors") or [])[:3])
            failed = "; ".join(str(f)[:120] for f in (view.get("failed_requests") or [])[:3])
            problems.append(f"the {name} render is not clean"
                            + (f" (console: {errors})" if errors else "")
                            + (f" (failed requests: {failed})" if failed else ""))
    requested = summary.get("steps")
    for name, view in summary["views"].items():
        if requested is None:
            if "steps" in view:
                problems.append(f"the {name} render records steps that were never requested")
            continue
        problem = _step_problem(name, requested, view.get("steps"))
        if problem:
            problems.append(problem)
    for name, viewport in VIEWPORTS.items():
        shot = folder / name / "page.png"
        width = _png_width(shot)
        if width is None:
            problems.append(f"no {name} screenshot at {shot.relative_to(root) if shot.is_absolute() else shot}")
            continue
        if shot.stat().st_mtime < since:
            problems.append(f"the {name} screenshot predates this task")
            continue
        if abs(width - viewport["width"]) > 64:
            problems.append(f"the {name} screenshot is {width}px wide, not ~{viewport['width']}px")
            continue
        shots.append(str(shot))
    if not problems and summary is not None:
        shots.append(f"target: {summary['target']}")
        if requested:
            shots.append("steps: " + "; ".join(
                f"{s['action']} {s['selector']}" + (f" = {s.get('label')}" if s["action"] == "file" else "")
                for s in requested))
    return not problems, "; ".join(problems), shots


def main(argv=None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    try:
        positional, steps = parse_steps(argv)
    except ValueError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    if len(positional) not in (2, 3):
        print("usage: python -m quadratus.design_evidence <url-or-html-file> <task-id> [project-root] "
              "[--click SEL] [--wait SEL] [--upload SEL path] [--file SEL=path]", file=sys.stderr)
        return 2
    try:
        out = capture(positional[0], positional[1], positional[2] if len(positional) == 3 else ".", steps)
    except ValueError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    print(json.dumps(out, indent=2))
    failed = [s for view in out.values() for s in view.get("steps", []) if not s.get("ok")]
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())

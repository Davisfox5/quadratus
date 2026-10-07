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
import hashlib
import json
import os
import re
import struct
import sys
import time
from pathlib import Path
from typing import List, Optional, Tuple
from urllib.parse import urlparse
from urllib.request import url2pathname

from .project_files import SECRET_NAMES

VIEWPORTS = {"desktop": {"width": 1280, "height": 800}, "mobile": {"width": 390, "height": 844}}
EVIDENCE_DIR = Path(".quadratus") / "design-evidence"


def evidence_dir(root, task_id: str) -> Path:
    return Path(root) / EVIDENCE_DIR / task_id


#: Capture-only fixtures: harness state, not project source, so a lead can
#: make one without a CHANGED entry and it survives for later captures
#: (Codex, Run 16: a lead made a valid CSV in tests/, captured, then deleted
#: it to keep CHANGED empty, and no later call could re-capture).
FIXTURE_DIR = Path(".quadratus") / "capture-fixtures"
MAX_FIXTURE_BYTES = 1_000_000
#: All file steps of one capture together, ordinary project fixtures included.
MAX_UPLOAD_BYTES = 5_000_000


#: Written by the session: the project-relative paths it excludes from
#: source, so a capture and the check fingerprint the same selected source
#: the session measures (Codex review of 9a31aac).
EXCLUDES_FILE = Path(".quadratus") / "source-excludes.json"


def write_source_excludes(root, excludes) -> None:
    """Record ``excludes`` (absolute paths) that fall inside ``root``."""
    root = Path(root).resolve()
    inside = sorted({Path(e).resolve().relative_to(root).as_posix() for e in excludes
                     if Path(e).resolve().is_relative_to(root) and Path(e).resolve() != root})
    path = root / EXCLUDES_FILE
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(inside))


MAX_EXCLUDES_BYTES = 64_000
MAX_EXCLUDES = 200


class EvidenceIdentityMismatch(RuntimeError):
    """Evidence whose identity was positively observed not to hold: a
    screenshot that is a symlink, a fixture whose bytes differ from the
    digest recorded at capture, a harness capture recorded against a source
    the harness did not capture, or renders whose digests differ from the
    snapshot a reviewer approved (map J9b). A factual observation only: no
    intent is inferred, and nothing here says who or what changed the
    evidence. An integrity stop: never recaptured and never repaired, and
    the evidence is left as found. Missing, stale or malformed evidence is
    invalid proof, not this."""


class ExcludesError(ValueError):
    """The recorded exclusions cannot be trusted as written; never replaced
    by a different source boundary."""


def _source_excludes(root: Path) -> List[Path]:
    """The recorded in-project exclusions, or [] when none were recorded.

    Hardened (Codex review of 40ba65b): no symlink at any component and
    the size checked before the file is opened; malformed, out-of-project
    or over-budget content raises rather than being silently trimmed.
    This file only configures a capture: an enforcing session compares
    against its own configured exclusions (``check(expected_source=...)``).
    """
    path = root / EXCLUDES_FILE
    current = root
    for part in EXCLUDES_FILE.parts:
        current = current / part
        if current.is_symlink():
            raise ExcludesError("the recorded source exclusions go through a symlink")
    if not path.exists():
        return []
    if not path.is_file() or path.stat().st_size > MAX_EXCLUDES_BYTES:
        raise ExcludesError("the recorded source exclusions are not a small regular file")
    try:
        entries = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise ExcludesError("the recorded source exclusions cannot be read") from exc
    if not isinstance(entries, list) or len(entries) > MAX_EXCLUDES:
        raise ExcludesError("the recorded source exclusions are malformed or too many")
    out = []
    for entry in entries:
        raw = Path(entry) if isinstance(entry, str) and entry else None
        if raw is None or raw.is_absolute() or ".." in raw.parts:
            raise ExcludesError(f"the recorded source exclusions name an invalid path: {str(entry)[:80]}")
        out.append(root / raw)
    return out


def source_fingerprint(root) -> Optional[str]:
    """The selected source's fingerprint as the session measures it: Project's
    own skips plus the session's recorded exclusions, whose files are never
    read. None when it cannot be computed or the exclusions are untrusted."""
    try:
        from .project import Project
        root = Path(root)
        return Project(root, exclude=_source_excludes(root)).fingerprint()
    except Exception:  # noqa: BLE001 -- unknown, which the check treats as a mismatch
        return None


def _digest(path: Path) -> Optional[str]:
    try:
        return hashlib.sha256(path.read_bytes()).hexdigest()
    except OSError:
        return None


def fixture_dir(root, task_id: str) -> Path:
    return Path(root) / FIXTURE_DIR / task_id


#: Bounds on an interactive capture: steps, per step, and per capture.
MAX_STEPS = 12
#: The longest selector a step may carry (preview.validate_capture holds the same bound).
MAX_SELECTOR_CHARS = 300
STEP_TIMEOUT_MS = 5000
CAPTURE_SECONDS = 90
_ACTIONS = ("click", "wait", "file", "confirm")
_LOCAL_HOSTS = {"localhost", "127.0.0.1", "::1"}
#: Never uploaded, whatever the task asks. Any hidden path component is
#: refused outright (run state, VCS data, .env, .ssh, .codex, tool configs);
#: these names are refused anywhere else (Codex review of c222d62:
#: .codex/auth.json and .ssh/id_ecdsa were accepted).
_BLOCKED_NAMES = SECRET_NAMES   # shared with the context pack (project_files)


def parse_steps(argv: List[str]) -> Tuple[List[str], List[dict]]:
    """Split ``--click/--wait/--confirm/--upload/--file`` steps from positional arguments.

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
        elif item == "--confirm":
            if len(items) < 2:
                raise ValueError("--confirm takes SELECTOR MESSAGE (text the confirm dialog must show)")
            selector, message = items.pop(0), items.pop(0)
            steps.append(dict(action="confirm", selector=selector, message=message))
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


def _fixture(root: Path, relative: str, task_id: Optional[str] = None) -> Path:
    """A project-owned, non-secret, non-symlinked regular file, or ValueError.

    The one hidden location allowed is this task's own capture-fixture
    folder, ``.quadratus/capture-fixtures/<task_id>/<name>``.
    """
    raw = Path(relative)
    if not relative or raw.is_absolute() or ".." in raw.parts:
        raise ValueError(f"file step path must be project-relative without '..': {relative!r}")
    own = (task_id is not None and re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]*", task_id)
           and raw.parts[:3] == (*FIXTURE_DIR.parts, task_id) and len(raw.parts) == 4)
    visible = raw.parts[3:] if own else raw.parts
    if any(part.startswith(".") for part in visible):
        raise ValueError(f"file step path is hidden or under a hidden directory: {relative!r}"
                         + (f" (capture-only fixtures go in {FIXTURE_DIR.as_posix()}/{task_id}/)"
                            if task_id else ""))
    if any(fnmatch.fnmatch(part.lower(), pattern) for part in raw.parts for pattern in _BLOCKED_NAMES):
        raise ValueError(f"file step path looks like a credential: {relative!r}")
    current = root
    for part in raw.parts:
        current = current / part
        if current.is_symlink():
            raise ValueError(f"file step path goes through a symlink: {relative!r}")
    if not current.is_file() or not current.resolve().is_relative_to(root.resolve()):
        raise ValueError(f"file step path is not a regular file in the project: {relative!r}")
    if own and current.stat().st_size > MAX_FIXTURE_BYTES:
        raise ValueError(f"capture fixture is larger than {MAX_FIXTURE_BYTES:,} bytes: {relative!r}")
    if own and current.stat().st_size == 0:
        # A discarded partial write leaves an empty placeholder
        # (session._write_fixture_bound); an empty sample is no sample.
        raise ValueError(f"capture fixture is empty: {relative!r}")
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


def validate_steps(steps: List[dict], target: str, root, task_id: Optional[str] = None) -> Tuple[List[dict], object]:
    """Checked steps (file paths resolved, with provenance) and the navigation
    rule, or ValueError. ``task_id`` admits that task's capture fixtures."""
    root = Path(root)
    if len(steps) > MAX_STEPS:
        raise ValueError(f"at most {MAX_STEPS} steps, not {len(steps)}")
    checked = []
    uploaded = 0
    for step in steps:
        action, selector = step.get("action"), step.get("selector")
        if action not in _ACTIONS:
            raise ValueError(f"unknown step {action!r}; use --click, --wait, --confirm or --file")
        if not isinstance(selector, str) or not selector.strip() or len(selector) > MAX_SELECTOR_CHARS:
            raise ValueError(f"each step needs a selector of at most {MAX_SELECTOR_CHARS} characters")
        item = dict(action=action, selector=selector.strip())
        if action != "confirm" and step.get("message") is not None:
            raise ValueError("only a confirm step names a dialog message")
        if action == "confirm":
            message = step.get("message")
            if not isinstance(message, str) or not message.strip() or len(message) > MAX_SELECTOR_CHARS:
                raise ValueError(f"a confirm step names the text its dialog must show, at most "
                                 f"{MAX_SELECTOR_CHARS} characters")
            item["message"] = message.strip()
        if action == "file":
            path = _fixture(root, step.get("path") or "", task_id)
            size = path.stat().st_size
            uploaded += size
            if uploaded > MAX_UPLOAD_BYTES:
                raise ValueError(f"file steps upload more than {MAX_UPLOAD_BYTES:,} bytes in all")
            item.update(path=str(path), label=step.get("path"), bytes=size, sha256=_digest(path))
        checked.append(item)
    return checked, (_navigation_rule(target, root) if checked else None)


def capture(target: str, task_id: str, root=".", steps: Optional[List[dict]] = None, *,
            pinned: bool = False, views: Optional[List[str]] = None, attempt: Optional[str] = None) -> dict:
    """Render ``target`` at each width into the task's evidence folder.

    With ``steps``, each width runs them on a fresh page before its
    screenshot. ``pinned`` holds navigation to the target's origin even with
    no steps (the harness's own capture, quadratus.preview). Refused steps are written into summary.json and raised, so the
    design check reports why instead of accepting an earlier render.

    ``views`` renders only those widths (the harness captures a state-
    changing declaration one view per preview, quadratus.preview), under
    ``attempt``, the harness's token for that one capture. The other views'
    entries are kept from the previous summary only when they are siblings
    of this render: the same attempt, the same target, the same source
    fingerprint, the same checked steps, every step passed, and their files
    present; anything else is history, not a sibling (Codex review of
    4a51291: a kept view was relabelled with the new render's identity, and
    a stale failed sibling ended the next attempt). The returned dict holds
    only the views rendered now; a run that raises part-way still leaves
    nothing standing.
    """
    from .browser import render_page
    folder = evidence_dir(root, task_id)
    folder.mkdir(parents=True, exist_ok=True)
    wanted = {name: VIEWPORTS[name] for name in (views or VIEWPORTS) if name in VIEWPORTS}
    if views and set(views) - set(VIEWPORTS):
        raise ValueError(f"unknown view(s) {sorted(set(views) - set(VIEWPORTS))}; the views are {list(VIEWPORTS)}")
    previous = _read_summary(folder) if views else None
    # Invalidate before anything can fail: a capture that raises part-way
    # must never leave an earlier, successful summary and screenshots standing
    # as this attempt's evidence (Codex review of c222d62).
    _write_summary(folder, dict(target=target, views={}, capture_in_progress=True))
    for name in wanted:
        for leftover in ("page.png", "evidence.json"):
            (folder / name / leftover).unlink(missing_ok=True)
    try:
        checked, allowed = validate_steps(list(steps or []), target, root, task_id)
        if pinned and allowed is None:
            allowed = _navigation_rule(target, Path(root))
    except ValueError as exc:
        _write_summary(folder, dict(target=target, views={}, steps_refused=str(exc)))
        raise
    deadline = time.monotonic() + CAPTURE_SECONDS if checked else None
    out = {}
    # The tree the renders show (Codex review of 3d5c3f3): recorded, and
    # compared again by the check, so a render stands only for this source.
    source = source_fingerprint(root)
    declared = [{k: v for k, v in s.items() if k != "path"} for s in checked]
    kept, not_kept = (_kept_views(folder, previous, set(VIEWPORTS) - set(wanted), target=target, attempt=attempt,
                                  source=source, declared=declared) if views else ({}, {}))
    try:
        _source_excludes(Path(root))
        identity_error = None
    except ExcludesError as exc:
        identity_error = str(exc)
    try:
        for name, viewport in wanted.items():
            for item in checked:
                if item["action"] == "file" and _digest(Path(item["path"])) != item["sha256"]:
                    raise ValueError(f"fixture {item['label']} changed or vanished during the capture")
            evidence = render_page(target, out_dir=folder / name, viewport=viewport, steps=checked,
                                   pin_requests=pinned,
                                   allow_navigation=allowed, step_timeout_ms=STEP_TIMEOUT_MS, deadline=deadline)
            out[name] = dict(screenshot=evidence.screenshot_path, clean=evidence.clean,
                             console_errors=evidence.console_errors[:10],
                             failed_requests=evidence.failed_requests[:10],
                             document_width=evidence.document_width, overflow=evidence.overflow[:5])
            if checked:
                out[name]["steps"] = evidence.steps
            # The bytes this render produced, bound to its entry: a retained
            # view stands only while its files still hash to what the capture
            # wrote (Codex review of ca0ad65: a replaced page.png of the same
            # size was kept as a sibling and accepted).
            out[name]["files"] = {leaf: _digest(folder / name / leaf) for leaf in VIEW_FILES}
    except BaseException as exc:
        _write_summary(folder, dict(target=target, views={}, rendered=sorted(out),
                                    capture_failed=f"{type(exc).__name__}: {str(exc)[:300]}"))
        raise
    summary = dict(target=target, views={**kept, **out},
                   source_fingerprint=source if source and source == source_fingerprint(root) else None)
    if attempt:
        summary["attempt"] = attempt
    if not_kept:
        summary["not_kept"] = not_kept
    if identity_error:
        summary["source_identity_error"] = identity_error
    if checked:
        summary["steps"] = declared
    (folder / "summary.json").write_text(json.dumps(summary, indent=2))
    return out


_FIXTURE_CHANGED = "changed after the capture"


def _fixture_problem(root, step: dict, task_id: str) -> Optional[str]:
    """What stops a recorded fixture from standing as captured, if anything.

    The same boundary a file step passes before capture is applied again
    before any byte is read (Codex review of 9a31aac: a summary naming an
    absolute or ../ path had that file hashed), then a bounded read.
    Missing or malformed digest metadata is unverified, never a match.
    """
    label, recorded = step.get("label"), step.get("sha256")
    if not isinstance(recorded, str) or not re.fullmatch(r"[0-9a-f]{64}", recorded):
        return "has no valid recorded digest"
    try:
        path = _fixture(Path(root), label if isinstance(label, str) else "", task_id)
    except (ValueError, OSError) as exc:
        plain = (isinstance(label, str) and label and not Path(label).is_absolute()
                 and ".." not in Path(label).parts)
        if plain and not os.path.lexists(Path(root) / label):
            return "no longer exists, so the capture cannot be reproduced"
        return f"is not a permitted fixture now ({str(exc)[:120]})"
    try:
        if path.stat().st_size > MAX_UPLOAD_BYTES:
            return "is larger than the upload budget"
    except OSError:
        return "no longer exists or cannot be read, so the capture cannot be reproduced"
    now = _digest(path)
    if now is None:
        return "no longer exists or cannot be read, so the capture cannot be reproduced"
    return _FIXTURE_CHANGED if now != recorded else None


def _write_summary(folder: Path, summary: dict) -> None:
    (folder / "summary.json").write_text(json.dumps(summary, indent=2))


def _read_summary(folder: Path):
    try:
        data = json.loads((folder / "summary.json").read_text())
    except (OSError, ValueError):
        return None
    return data if isinstance(data, dict) else None


#: The files a render writes, each recorded by digest in the view's entry.
VIEW_FILES = ("page.png", "evidence.json")

_FILE_CHANGED = "changed after the capture"


def _symlinked_component(folder: Path) -> Optional[Path]:
    """The first symlink among a view folder and the evidence folders above
    it (task, design-evidence, .quadratus), if any: a leaf check alone
    endorses files reached through a symlinked directory (Codex review of
    180012d: the view folder replaced by a link to a tree outside the
    project)."""
    for component in (folder, folder.parent, folder.parent.parent, folder.parent.parent.parent):
        if component.is_symlink():
            return component
    return None


def _files_problem(folder: Path, view) -> Optional[Tuple[str, bool]]:
    """Why a view's files do not stand as the capture wrote them, if they do
    not: ``(problem, mismatch)``. The entry must record a well-formed digest
    for every file a render writes and nothing else, no folder on the way
    to them may be a symlink, and each file must hash to its digest now.
    Only a symlink or a digest that disagrees with the bytes is an observed
    mismatch; a missing or malformed record is unverified, never a match."""
    files = view.get("files") if isinstance(view, dict) else None
    if not isinstance(files, dict):
        return "carries no record of its files' digests", False
    for leaf in VIEW_FILES:
        if leaf not in files:
            return f"carries no record of its {leaf} digest", False
    for leaf, recorded in files.items():
        if (leaf not in VIEW_FILES or not isinstance(recorded, str)
                or not re.fullmatch(r"[0-9a-f]{64}", recorded)):
            return f"carries a malformed digest record ({str(leaf)[:40]})", False
    linked = _symlinked_component(folder)
    if linked is not None:
        return f"folder {linked.name} is a symlink, not a capture folder", True
    for leaf, recorded in files.items():
        path = folder / leaf
        if path.is_symlink():
            return f"{leaf} is a symlink, not a capture", True
        now = _digest(path)
        if now is None:
            return f"{leaf} is missing or unreadable", False
        if now != recorded:
            return f"{leaf} {_FILE_CHANGED}", True
    return None


def view_receipt(root, task_id: str, name: str) -> dict:
    """The digests of one view's files as they are on disk now, for a harness
    that measures right after its own capture and keeps the result outside
    the project (``check_records(receipt=...)``). A missing file is None."""
    folder = evidence_dir(root, task_id) / name
    return {leaf: _digest(folder / leaf) for leaf in VIEW_FILES}


def _kept_views(folder: Path, previous, names, *, target: str, attempt: Optional[str], source, declared) -> dict:
    """The named views' entries from the previous summary, kept for a
    partial render only as siblings: the previous summary finished under
    the same attempt token, for the same target, the same source
    fingerprint and the same checked steps, with every step of each kept
    view passed and its files present and hashing to the digests its entry
    records; otherwise nothing. A view that fails any of these is history
    and never joins this attempt. Returns ``(kept, refused)``: ``refused``
    names each sibling candidate whose files did not stand, with the
    problem and whether it was an observed mismatch."""
    if not names:
        return {}, {}
    if (not previous or not attempt or previous.get("attempt") != attempt or previous.get("target") != target
            or previous.get("capture_in_progress") or previous.get("capture_failed") or previous.get("steps_refused")
            or not isinstance(previous.get("views"), dict)
            or not source or previous.get("source_fingerprint") != source
            or previous.get("steps", []) != declared):
        return {}, {}
    kept, refused = {}, {}
    for name in names:
        view = previous["views"].get(name)
        if not isinstance(view, dict):
            continue
        # The bytes the capture wrote, still: a sibling whose files were
        # observed to differ is refused with that classification kept, so
        # the record says "replaced", never merely "missing" (Codex review
        # of 180012d).
        problem = _files_problem(folder / name, view)
        if problem:
            refused[name] = dict(problem=problem[0], mismatch=problem[1])
        elif all(isinstance(s, dict) and s.get("ok") is True for s in view.get("steps", [])):
            kept[name] = view
    return (kept if len(kept) == len(set(names)) else {}), refused


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
        if want["action"] == "confirm":
            # The evidence must show the declared dialog, exactly, was the one
            # answered and accepted (Codex reviews of ce35fb6 and 4a51291: a
            # record with no dialog, another dialog, or a longer message
            # passed the checker).
            from .browser import _dialog_text
            dialog = got.get("dialog")
            expected = _dialog_text(want.get("message"))
            if (not isinstance(dialog, dict) or dialog.get("accepted") is not True
                    or dialog.get("type") != "confirm" or not expected
                    or not isinstance(dialog.get("message"), str)   # typed evidence, never a stringified container
                    or dialog.get("truncated") or _dialog_text(dialog["message"]) != expected
                    or got.get("extra_dialogs")):
                return (f"the {view} render's step {index + 1} (confirm {want['selector'][:80]}) carries no "
                        f"record of the declared dialog being accepted")
    if len(done) != len(requested):
        return f"the {view} render ran {len(done)} of {len(requested)} interaction steps"
    return None


#: The record kind for a capture whose declared steps cannot show the change:
#: the declaration, not the page or the source, is what needs changing.
CAPTURE_DECLARATION = "capture.declaration"


def _final_wait_problem(view: str, requested, done) -> Optional[str]:
    """The final wait named an element present at load (Codex, Run 15), so
    the capture passed whether or not the feature produced anything.
    Insufficient evidence, not proof the feature failed: a valid flow can
    update a region that was already showing. The selector can name the new
    state itself, which keeps the step vocabulary as is. Reported under its
    own kind (series rule-3572b72 f2 t1: a design-fix call was spent on
    source that was not the problem and hit the turn cap at 961k tokens)."""
    if not (isinstance(requested, list) and requested and isinstance(done, list) and done):
        return None
    last = requested[-1]
    if (isinstance(last, dict) and last.get("action") == "wait" and isinstance(done[-1], dict)
            and done[-1].get("visible_before_steps") is True):
        return (f"the {view} render's final wait ({str(last.get('selector'))[:80]}) was already visible before "
                "any step ran, so seeing it is not evidence of the change; wait on a state only the "
                "result creates, which the selector can name (for example [data-state=done] or "
                "#results tr)")
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


def check(root, task_id: str, since: float, *, expected_source: Optional[str] = None) -> Tuple[bool, str, list]:
    """Whether the task left fresh, clean desktop and mobile renders of a
    named page. Never raises.

    Clean means no console errors and no failed requests: a render of a
    broken page is evidence that it is broken, not that it works (Codex
    review of #25 rendered a page with a console.error and a missing image,
    and the first version of this check passed it).
    """
    return check_records(root, task_id, since, expected_source=expected_source)[:3]


def check_records(root, task_id: str, since: float, *, expected_source: Optional[str] = None,
                  receipt: Optional[dict] = None):
    """:func:`check` plus typed problem records. Never raises.

    Kinds: ``integrity`` (the evidence itself cannot be trusted: missing,
    stale, identity, fixture, steps, capture or transfer failures, malformed
    data), ``page.unclean`` (console errors or failed requests), and
    ``product.overflow`` (a measured page wider than its viewport, with
    ``view``, ``width``, ``viewport`` and ``target``). Only the last is a
    measured product fact; see Session's audit findings. An ``integrity``
    record carrying ``mismatch=True`` was positively observed (a symlinked
    screenshot, a fixture whose bytes changed after capture); one carrying
    ``identity="source"`` names a source mismatch, which counts as observed
    only when the harness took the capture itself (map J9b).

    ``receipt`` is the harness's own measurement of each view's files
    (:func:`view_receipt`, taken right after its capture and held outside
    the project): with it, every view's recorded digests must equal what
    the harness measured, so a manifest rewritten beside replaced bytes is
    an observed mismatch and a view the harness never measured is no
    evidence (Codex review of 180012d: the digests lived only in the same
    mutable summary as the files).
    """
    try:
        return _check(root, task_id, since, expected_source, receipt)
    except Exception as exc:  # noqa: BLE001 -- evidence is data; malformed data is a finding
        message = f"the evidence could not be read ({type(exc).__name__}: {str(exc)[:160]})"
        return False, message, [], [dict(kind="integrity", message=message)]


def _check(root, task_id: str, since: float, expected_source: Optional[str] = None,
           receipt: Optional[dict] = None):
    """``(ok, message, shots, records)``; each record is a typed problem,
    ``{"kind", "message", ...}``, so callers branch on kinds, never text."""
    folder = evidence_dir(root, task_id)
    problems, shots, records = [], [], []

    def add(kind, message, **detail):
        problems.append(message)
        records.append(dict(kind=kind, message=message, **detail))

    def fail(message):
        return False, message, [], [dict(kind="integrity", message=message)]
    try:
        summary = json.loads((folder / "summary.json").read_text())
    except (OSError, ValueError):
        summary = None
    if isinstance(summary, dict) and summary.get("capture_failed"):
        return fail(f"the last capture failed: {str(summary['capture_failed'])[:200]}")
    if isinstance(summary, dict) and summary.get("capture_in_progress"):
        return fail("the last capture did not finish")
    if isinstance(summary, dict) and summary.get("steps_refused"):
        return fail(f"the capture's interaction steps were refused: {str(summary['steps_refused'])[:200]}")
    # A sibling the last partial capture refused because its files were
    # observed to differ: the mismatch is reported as such, ahead of the
    # incomplete view set it leaves behind.
    not_kept = summary.get("not_kept") if isinstance(summary, dict) else None
    for name, entry in (not_kept.items() if isinstance(not_kept, dict) else []):
        if isinstance(entry, dict) and entry.get("mismatch") is True and isinstance(entry.get("problem"), str):
            add("integrity", f"the {str(name)[:20]} render of this attempt was not kept: {entry['problem'][:160]}",
                mismatch=True)
    if (not isinstance(summary, dict) or not isinstance(summary.get("target"), str) or not summary["target"]
            or not isinstance(summary.get("views"), dict)
            or set(summary["views"]) != set(VIEWPORTS)
            or not all(isinstance(v, dict) for v in summary["views"].values())):
        message = "no well-formed summary.json naming the rendered page (use python -m quadratus.design_evidence)"
        add("integrity", message)
        return False, problems[0], [], records
    for name, view in summary["views"].items():
        if not view.get("clean"):
            errors = "; ".join(str(e)[:120] for e in (view.get("console_errors") or [])[:3])
            failed = "; ".join(str(f)[:120] for f in (view.get("failed_requests") or [])[:3])
            add("page.unclean", f"the {name} render is not clean"
                            + (f" (console: {errors})" if errors else "")
                            + (f" (failed requests: {failed})" if failed else ""), view=name)
    # Required, not optional: a render with no record of the source it shows
    # is not evidence for any particular tree (Codex review of 9a31aac).
    recorded = summary.get("source_fingerprint", "missing")
    if recorded == "missing":
        add("integrity", "the renders carry no record of the source they show")
    elif isinstance(summary.get("source_identity_error"), str):
        add("integrity", f"the source could not be identified at capture: {summary['source_identity_error'][:160]}")
    elif not isinstance(recorded, str) or not re.fullmatch(r"[0-9a-f]{64}", recorded):
        add("integrity", "the source changed while the renders were being captured")
    elif recorded != (expected_source if expected_source is not None else source_fingerprint(root)):
        # An enforcing session passes the fingerprint of its own configured
        # source; the recorded exclusions file, which a solver can write,
        # never decides what that session counts as source.
        add("integrity", "the renders were captured on a different source tree than the current one",
            identity="source")
    requested = summary.get("steps")
    for index, step in enumerate(requested if isinstance(requested, list) else [], 1):
        if isinstance(step, dict) and step.get("action") == "file":
            problem = _fixture_problem(root, step, task_id)
            if problem:
                # Only a digest that disagrees with the bytes is an observed
                # mismatch; a fixture gone or unreadable is invalid proof.
                add("integrity", f"step {index}'s fixture {str(step.get('label'))[:80]} {problem}",
                    **(dict(mismatch=True) if problem == _FIXTURE_CHANGED else {}))
    for name, view in summary["views"].items():
        if requested is None:
            if "steps" in view:
                add("integrity", f"the {name} render records steps that were never requested")
            continue
        problem = _step_problem(name, requested, view.get("steps"))
        if problem:
            add("integrity", problem)
            continue
        problem = _final_wait_problem(name, requested, view.get("steps"))
        if problem:
            add(CAPTURE_DECLARATION, problem)
    for name, viewport in VIEWPORTS.items():
        shot = folder / name / "page.png"
        if shot.is_symlink():
            add("integrity", f"the {name} screenshot is a symlink, not a capture", mismatch=True)
            continue
        width = _png_width(shot)
        if width is None:
            add("integrity", f"no {name} screenshot at {shot.relative_to(root) if shot.is_absolute() else shot}")
            continue
        if shot.stat().st_mtime < since:
            add("integrity", f"the {name} screenshot predates this task")
            continue
        # The files must still be the bytes the capture recorded for this
        # view; a record without digests is no evidence of what was rendered.
        problem = _files_problem(folder / name, summary["views"].get(name))
        if problem:
            add("integrity", f"the {name} render {problem[0]}", **(dict(mismatch=True) if problem[1] else {}))
            continue
        if receipt is not None:
            measured = receipt.get(name) if isinstance(receipt, dict) else None
            if not isinstance(measured, dict) or any(not isinstance(measured.get(leaf), str) for leaf in VIEW_FILES):
                add("integrity", f"the {name} render was not measured by the harness in this attempt")
                continue
            if summary["views"][name]["files"] != {leaf: measured[leaf] for leaf in VIEW_FILES}:
                add("integrity", f"the {name} render's record does not match the harness's measurement of "
                                 "its files", mismatch=True)
                continue
        if abs(width - viewport["width"]) > 64:
            view = summary["views"].get(name) or {}
            offenders = [o for o in (view.get("overflow") or []) if isinstance(o, dict)][:5]
            named = ", ".join(f"{str(o.get('element'))[:80]} (past the {o.get('side', 'right')} edge: "
                              f"left {o.get('left')}px, right {o.get('right')}px, {o.get('width')}px wide)"
                              for o in offenders)
            measured = view.get("document_width")
            # A full-page capture of an overflowing page is as wide as the
            # page it measured; a screenshot of another width (a desktop
            # image filed as mobile) is not evidence for this viewport
            # (Codex review of e47c7ed), so only a consistent pair is product.
            overflowing = (isinstance(measured, int) and not isinstance(measured, bool)
                           and measured > viewport["width"] + 1 and abs(width - measured) <= 64)
            add("product.overflow" if overflowing else "integrity",
                f"the {name} screenshot is {width}px wide, not ~{viewport['width']}px"
                + ("" if overflowing or not isinstance(measured, int) or isinstance(measured, bool)
                   else f", and does not match the {measured}px page width measured with it")
                + (f"; the page overflows its {viewport['width']}px viewport; elements past its "
                   f"edges: {named}" if named else ""),
                **(dict(view=name, width=measured, viewport=viewport["width"], target=summary["target"])
                   if overflowing else {}))
            continue
        # The measured document width as well as the screenshot's: a page
        # 60px wider than the viewport fits the 64px screenshot tolerance
        # and is still overflow (Codex, Run 16: 450px at 390px). Stricter,
        # never looser; the screenshot tolerance above is unchanged.
        view = summary["views"].get(name) or {}
        measured = view.get("document_width")
        # Unknown is not "no overflow" (Codex review of 3d5c3f3): a missing,
        # null or non-integer measurement leaves the render unverified.
        if measured is None:
            add("integrity", f"the {name} render's page width was not measured")
            continue
        if not isinstance(measured, int) or isinstance(measured, bool) or measured <= 0:
            add("integrity", f"the {name} render's measured page width is malformed ({str(measured)[:40]})")
            continue
        if measured > viewport["width"] + 1:
            offenders = [o for o in (view.get("overflow") or []) if isinstance(o, dict)][:5]
            named = ", ".join(f"{str(o.get('element'))[:80]} (past the {o.get('side', 'right')} edge: "
                              f"left {o.get('left')}px, right {o.get('right')}px)" for o in offenders)
            add("product.overflow", f"the {name} page is {measured}px wide at a {viewport['width']}px viewport, "
                "so it overflows" + (f"; elements past its edges: {named}" if named else ""),
                view=name, width=measured, viewport=viewport["width"], target=summary["target"])
            continue
        shots.append(str(shot))
    if not problems and summary is not None:
        shots.append(f"target: {summary['target']}")
        if requested:
            shots.append("steps: " + "; ".join(
                f"{s['action']} {s['selector']}"
                + (f" ({s.get('message')!r})" if s["action"] == "confirm" else "")
                + (f" = {s.get('label')}" + (f" (sha256 {str(s['sha256'])[:12]}, {s.get('bytes')} bytes)"
                                            if s.get("sha256") else "") if s["action"] == "file" else "")
                for s in requested))
    return not problems, "; ".join(problems), shots, records


def main(argv=None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    pinned = "--pinned" in argv
    argv = [a for a in argv if a != "--pinned"]
    views = None
    while "--view" in argv:
        at = argv.index("--view")
        if at + 1 >= len(argv):
            print("error: --view needs a name", file=sys.stderr)
            return 2
        views = (views or []) + [argv[at + 1]]
        del argv[at:at + 2]
    attempt = None
    if "--attempt" in argv:
        at = argv.index("--attempt")
        if at + 1 >= len(argv) or not re.fullmatch(r"[0-9a-f]{8,64}", argv[at + 1]):
            print("error: --attempt needs a hex token", file=sys.stderr)
            return 2
        attempt = argv[at + 1]
        del argv[at:at + 2]
    try:
        positional, steps = parse_steps(argv)
    except ValueError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    if len(positional) not in (2, 3):
        print("usage: python -m quadratus.design_evidence <url-or-html-file> <task-id> [project-root] "
              "[--click SEL] [--wait SEL] [--confirm SEL MESSAGE] [--upload SEL path] [--file SEL=path] "
              "[--view desktop|mobile] [--attempt HEX]", file=sys.stderr)
        return 2
    try:
        out = capture(positional[0], positional[1], positional[2] if len(positional) == 3 else ".", steps,
                      pinned=pinned, views=views, attempt=attempt)
    except ValueError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    except Exception as exc:  # noqa: BLE001 -- a crash is its own exit code, never a failed step
        # Exit 1 means exactly one thing, a declared step that did not
        # happen; a browser or runtime crash is 3, so the session never
        # reads a crash as a declaration defect (Codex review of f8d8c03).
        print(f"error: the capture crashed ({type(exc).__name__}: {str(exc)[:300]})", file=sys.stderr)
        return 3
    print(json.dumps(out, indent=2))
    # Exit status from the views this invocation rendered: a partial render
    # answers for itself, and final acceptance is the checker's, which needs
    # every view (Codex review of 4a51291: a stale failed sibling made a
    # successful desktop render exit 1).
    failed = [s for view in out.values() for s in view.get("steps", []) if not s.get("ok")]
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())

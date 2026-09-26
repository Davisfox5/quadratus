"""Project files the harness hands a lead, read fresh and bounded.

Codex, Run 14 (2026-09-26): the Grok lead spent all 14 rounds on 27
discovery tool calls and wrote nothing; the Claude continuation repeated the
same discovery and made its only edit on the last round. Each lead re-read
the files its task was scoped to, because nothing handed them over. An agent
re-sends its whole conversation every round, so a file read by a tool on
round 2 is paid for on every round after it; the same file placed in the
prompt once replaces those reads.

What goes in is decided by the harness from facts it holds (the task's exact
permitted paths, a capped predecessor's changed files), never by a model,
and is read from the selected project's working tree just before the call
with a hash per file, so it is current. What never goes in, with only its
path and the reason shown: patterns, anything outside the project, symlinks
at any component, hidden components, credential-like names, non-regular,
unreadable, oversized, binary or non-UTF-8 files, and excluded paths.

Reading is bounded as well as output (Codex review of 895cf67): no file
larger than ``MAX_INSPECT_BYTES`` is opened, and once the prompt budget is
spent no further candidate is opened at all. Hash and line count always
describe the whole file, which is why a file too big to read whole is
refused rather than summarised from a prefix.

A file a capped predecessor changed, and too long to show whole, is shown as
windows around the lines that differ from the task's starting point rather
than as its first lines: Run 14's continuation needed the tail of a
1,669-line file, and a prefix cut at line 297 showed none of it.
"""

from __future__ import annotations

import difflib
import fnmatch
import hashlib
from pathlib import Path, PurePosixPath
from typing import Dict, Iterable, List, Optional, Sequence, Tuple

#: Credential-like names, refused wherever they appear: a file step never
#: uploads one and a context pack never shows one.
SECRET_NAMES = ("id_*", "*.pem", "*.key", "*.p12", "*.pfx", "*.jks", "*.keystore", "*.gpg", "*.asc",
                "*.ppk", "*auth*.json", "*credential*", "*secret*", "*token*", "*password*")

#: Bounds on one pack. The prompt is re-sent on every round of the call, so
#: these trade a fixed cost against the reads they replace.
MAX_FILES = 6
MAX_FILE_BYTES = 12_000
MAX_TOTAL_BYTES = 32_000
#: The most any one file may be read to decide what to show. Larger files
#: are refused without being opened.
MAX_INSPECT_BYTES = 512_000
#: Lines of unchanged context either side of a changed region.
WINDOW_CONTEXT = 6


class _Refused(Exception):
    pass


def path_refusal(root: Path, relative: str, *, exclude: Sequence[Path] = ()) -> Optional[str]:
    """Why ``relative`` may not be shown from ``root``, or None if it may.

    Looks only at names and file metadata; never opens the file.
    """
    text = str(relative).strip().removeprefix("./")
    if not text:
        return "an empty path"
    if any(ch in text for ch in "*?["):
        return "a pattern, not a file"
    raw = PurePosixPath(text)
    if raw.is_absolute() or ".." in raw.parts:
        return "outside the project"
    if any(part.startswith(".") for part in raw.parts):
        return "hidden, or under a hidden directory"
    if any(fnmatch.fnmatch(part.lower(), pattern) for part in raw.parts for pattern in SECRET_NAMES):
        return "a credential-like name"
    try:
        root = Path(root)
        current = root
        for part in raw.parts:
            current = current / part
            if current.is_symlink():
                return "goes through a symlink"
        if not current.exists():
            return "does not exist yet"
        resolved = current.resolve()
        if not resolved.is_relative_to(root.resolve()):
            return "outside the project"
        if any(resolved.is_relative_to(Path(e).resolve()) for e in exclude):
            return "excluded from the project"
        if not current.is_file():
            return "not a regular file"
    except OSError as exc:
        return f"could not be inspected ({type(exc).__name__})"
    return None


def _read_text(path: Path) -> Tuple[bytes, str]:
    """The whole file and its text, within ``MAX_INSPECT_BYTES``, or _Refused."""
    try:
        size = path.stat().st_size
    except OSError as exc:
        raise _Refused(f"could not be read ({type(exc).__name__})") from None
    if size > MAX_INSPECT_BYTES:
        raise _Refused(f"larger than the {MAX_INSPECT_BYTES:,}-byte inspection bound")
    try:
        with path.open("rb") as handle:
            data = handle.read(MAX_INSPECT_BYTES + 1)
    except OSError as exc:
        raise _Refused(f"could not be read ({type(exc).__name__})") from None
    if len(data) > MAX_INSPECT_BYTES:
        raise _Refused(f"larger than the {MAX_INSPECT_BYTES:,}-byte inspection bound")
    if b"\0" in data:
        raise _Refused("binary (contains NUL bytes)")
    try:
        return data, data.decode("utf-8")
    except UnicodeDecodeError:
        raise _Refused("not UTF-8 text") from None


def context_pack(root, paths: Iterable[str], *, exclude: Sequence[Path] = (),
                 baselines: Optional[Dict[str, bytes]] = None) -> Tuple[List[dict], List[dict]]:
    """``(included, refused)`` for ``paths``, in order, deduplicated.

    ``baselines`` maps a path to its content at the start of the capped task
    that changed it; such a file, if too long to show whole, is shown as
    windows around the lines that now differ.

    Each included entry is ``{path, sha, lines, text, truncated, windows}``;
    ``windows`` is a list of 1-based inclusive line ranges when the file is
    shown in windows, else None. Each refused entry is ``{path, reason}`` and
    never carries content.
    """
    root = Path(root)
    baselines = baselines or {}
    included: List[dict] = []
    refused: List[dict] = []
    total = 0
    seen = set()
    for raw in paths:
        path = str(raw).strip().removeprefix("./")
        if path in seen:
            continue
        seen.add(path)
        # Budget and count first: a candidate past either is never opened.
        if total >= MAX_TOTAL_BYTES:
            refused.append(dict(path=path, reason="over the context budget for this prompt"))
            continue
        if len(included) >= MAX_FILES:
            refused.append(dict(path=path, reason=f"over the {MAX_FILES}-file limit for this prompt"))
            continue
        reason = path_refusal(root, path, exclude=exclude)
        if reason is not None:
            refused.append(dict(path=path, reason=reason))
            continue
        try:
            data, text = _read_text(root / path)
        except _Refused as exc:
            refused.append(dict(path=path, reason=str(exc)))
            continue
        lines = text.count("\n") + (0 if text.endswith("\n") or not text else 1)
        limit = min(MAX_FILE_BYTES, MAX_TOTAL_BYTES - total)
        windows = None
        if len(text.encode()) <= limit:
            shown, truncated = text, False
        elif path in baselines:
            shown, windows = _changed_windows(baselines[path], text, limit)
            truncated = True
            if not windows:
                shown, truncated = _bounded(text, limit)
                windows = None
        else:
            shown, truncated = _bounded(text, limit)
        total += len(shown.encode())
        included.append(dict(path=path, sha=hashlib.sha256(data).hexdigest()[:12], lines=lines,
                             text=shown, truncated=truncated, windows=windows))
    return included, refused


def _bounded(text: str, limit: int) -> Tuple[str, bool]:
    """``text`` cut to at most ``limit`` UTF-8 bytes at a line boundary."""
    if limit <= 0:
        return "", bool(text)
    if len(text.encode()) <= limit:
        return text, False
    cut = text.encode()[:limit].decode("utf-8", errors="ignore")
    newline = cut.rfind("\n")
    return (cut[:newline + 1] if newline > 0 else cut), True


def changed_ranges(before: bytes, text: str) -> List[Tuple[int, int]]:
    """1-based inclusive line ranges of ``text`` that differ from ``before``.

    A pure deletion is reported as the line it left behind. Empty when the
    baseline is not UTF-8 text or nothing differs.
    """
    try:
        old = before.decode("utf-8").splitlines()
    except UnicodeDecodeError:
        return []
    new = text.splitlines()
    ranges = []
    matcher = difflib.SequenceMatcher(None, old, new, autojunk=False)
    for tag, _i1, _i2, j1, j2 in matcher.get_opcodes():
        if tag == "equal":
            continue
        start = min(j1 + 1, max(len(new), 1))
        ranges.append((start, max(j2, start)))
    return ranges


def _changed_windows(before: bytes, text: str, limit: int) -> Tuple[str, List[Tuple[int, int]]]:
    """Windows of ``text`` around its changes, within ``limit`` bytes."""
    new = text.splitlines(keepends=True)
    merged: List[List[int]] = []
    for start, end in changed_ranges(before, text):
        lo, hi = max(1, start - WINDOW_CONTEXT), min(len(new), end + WINDOW_CONTEXT)
        if merged and lo <= merged[-1][1] + 1:
            merged[-1][1] = max(merged[-1][1], hi)
        else:
            merged.append([lo, hi])
    parts, shown, used = [], [], 0
    for lo, hi in merged:
        header = f"--- lines {lo}-{hi} ---\n"
        body = "".join(new[lo - 1:hi])
        room = limit - used - len(header.encode())
        if room <= 0:
            break
        if len(body.encode()) > room:
            body, _ = _bounded(body, room)
            if not body:
                break
            hi = lo + body.count("\n") - 1 if body.endswith("\n") else lo + body.count("\n")
            header = f"--- lines {lo}-{hi} ---\n"
        parts.append(header + body)
        shown.append((lo, hi))
        used += len((header + body).encode())
    return "".join(parts), shown


def render_pack(included: List[dict], refused: List[dict]) -> str:
    """The pack as a prompt section, or "" when there is nothing to say."""
    if not included and not refused:
        return ""
    parts = ["## Project files, read by the harness just before this call",
             "Current contents from the working tree, so there is no need to open these "
             "again to learn what they say. Read a file yourself only for a part not shown "
             "here, or after you change it."]
    for entry in included:
        if entry.get("windows"):
            spans = ", ".join(f"{lo}-{hi}" for lo, hi in entry["windows"])
            note = (f"; showing only lines {spans}, around what changed since the capped task "
                    "began, read the rest yourself if you need it")
        elif entry["truncated"]:
            note = (f"; cut at {entry['text'].count(chr(10))} of {entry['lines']} lines, read the rest "
                    "yourself if you need it")
        else:
            note = ""
        parts.append(f"### {entry['path']} ({entry['lines']} lines, sha256 {entry['sha']}{note})\n"
                     f"```\n{entry['text'].rstrip(chr(10))}\n```")
    if refused:
        parts.append("Not included: " + "; ".join(f"{r['path']} ({r['reason']})" for r in refused) + ".")
    return "\n\n".join(parts)

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
at any component, hidden components, credential-like names, non-regular or
binary files, and excluded paths.
"""

from __future__ import annotations

import fnmatch
import hashlib
from pathlib import Path, PurePosixPath
from typing import Iterable, List, Optional, Sequence, Tuple

#: Credential-like names, refused wherever they appear: a file step never
#: uploads one and a context pack never shows one.
SECRET_NAMES = ("id_*", "*.pem", "*.key", "*.p12", "*.pfx", "*.jks", "*.keystore", "*.gpg", "*.asc",
                "*.ppk", "*auth*.json", "*credential*", "*secret*", "*token*", "*password*")

#: Bounds on one pack. The prompt is re-sent on every round of the call, so
#: these trade a fixed cost against the reads they replace.
MAX_FILES = 6
MAX_FILE_BYTES = 12_000
MAX_TOTAL_BYTES = 32_000


def path_refusal(root: Path, relative: str, *, exclude: Sequence[Path] = ()) -> Optional[str]:
    """Why ``relative`` may not be shown from ``root``, or None if it may."""
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
    root = Path(root)
    current = root
    for part in raw.parts:
        current = current / part
        if current.is_symlink():
            return "goes through a symlink"
    resolved_root = root.resolve()
    if not current.exists():
        return "does not exist yet"
    resolved = current.resolve()
    if not resolved.is_relative_to(resolved_root):
        return "outside the project"
    if any(resolved.is_relative_to(Path(e).resolve()) for e in exclude):
        return "excluded from the project"
    if not current.is_file():
        return "not a regular file"
    return None


def context_pack(root, paths: Iterable[str], *, exclude: Sequence[Path] = ()) -> Tuple[List[dict], List[dict]]:
    """``(included, refused)`` for ``paths``, in order, deduplicated.

    Each included entry is ``{path, sha, lines, text, truncated}``; each
    refused one is ``{path, reason}`` and never carries content.
    """
    root = Path(root)
    included: List[dict] = []
    refused: List[dict] = []
    total = 0
    seen = set()
    for raw in paths:
        path = str(raw).strip().removeprefix("./")
        if path in seen:
            continue
        seen.add(path)
        reason = path_refusal(root, path, exclude=exclude)
        if reason is None and len(included) >= MAX_FILES:
            reason = f"over the {MAX_FILES}-file limit for this prompt"
        if reason is not None:
            refused.append(dict(path=path, reason=reason))
            continue
        data = (root / path).read_bytes()
        if b"\0" in data[:8192]:
            refused.append(dict(path=path, reason="binary"))
            continue
        text = data.decode("utf-8", errors="replace")
        lines = text.count("\n") + (0 if text.endswith("\n") or not text else 1)
        shown, truncated = _bounded(text, min(MAX_FILE_BYTES, MAX_TOTAL_BYTES - total))
        if not shown and text:
            refused.append(dict(path=path, reason="over the context budget for this prompt"))
            continue
        total += len(shown.encode())
        included.append(dict(path=path, sha=hashlib.sha256(data).hexdigest()[:12], lines=lines,
                             text=shown, truncated=truncated))
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


def render_pack(included: List[dict], refused: List[dict]) -> str:
    """The pack as a prompt section, or "" when there is nothing to say."""
    if not included and not refused:
        return ""
    parts = ["## Project files, read by the harness just before this call",
             "Current contents from the working tree, so there is no need to open these "
             "again to learn what they say. Read a file yourself only for a part cut off "
             "here, or after you change it."]
    for entry in included:
        cut = (f"; cut at {entry['text'].count(chr(10))} of {entry['lines']} lines, read the rest "
               "yourself if you need it" if entry["truncated"] else "")
        parts.append(f"### {entry['path']} ({entry['lines']} lines, sha256 {entry['sha']}{cut})\n"
                     f"```\n{entry['text'].rstrip(chr(10))}\n```")
    if refused:
        parts.append("Not included: " + "; ".join(f"{r['path']} ({r['reason']})" for r in refused) + ".")
    return "\n\n".join(parts)

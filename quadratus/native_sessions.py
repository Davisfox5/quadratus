"""Read native usage only for children linked to the dispatched Codex session.

Candidate discovery reads metadata, never conversations: it is restricted to
rollout files created during this parent call, in the same dated directory.
Only a matching cwd and explicit subagent parent link authorize reading a body.
No vendor session is modified, and observations cannot change model routing.
"""
from __future__ import annotations

import json
import re
from datetime import datetime, timezone
from pathlib import Path

from .delegation import NativeChild

#: Most bytes of one journal line that are read. A codex rollout's first
#: record carries the session's instructions, so it is not small; a longer
#: first line is not read as anyone's identity, and a longer later line is
#: passed over.
FIRST_RECORD_BYTES = 1024 * 1024


def bounded_lines(path, limit: int = FIRST_RECORD_BYTES):
    """The lines of a journal, none held past ``limit`` bytes.

    A journal records tool output, which a model can make as long as it
    likes, and a plain line-by-line read holds each line whole. A line past
    the bound is skipped to its end, never kept: the records read from these
    files (token counts, turn context) are short.
    """
    with open(path, "rb") as stream:
        while True:
            line = stream.readline(limit + 1)
            if not line:
                return
            if len(line) > limit:
                while line and not line.endswith(b"\n"):
                    line = stream.readline(limit + 1)
                continue
            yield line.decode("utf-8", errors="replace")


def _events(path):
    for line in bounded_lines(path):
        try:
            event = json.loads(line)
            if isinstance(event, dict):
                yield event
        except ValueError:
            continue


def first_record(path, limit: int = FIRST_RECORD_BYTES):
    """A journal's first line as parsed JSON, read with a byte bound.

    The vendor's session folder is not ours, and a journal whose first line
    never ends would otherwise be read whole into memory. Raises ValueError
    for a line past the bound or one that is not JSON, and OSError for a file
    that cannot be read; callers treat both as "not provably this journal".
    """
    with open(path, "rb") as stream:
        line = stream.readline(limit + 1)
    if len(line) > limit:
        raise ValueError("the journal's first record is longer than the read bound")
    return json.loads(line.decode("utf-8", errors="replace"))


def _metadata(path):
    # Child files contain inherited parent metadata later. Only the initial
    # record describes the file's own identity.
    event = first_record(path)
    if not isinstance(event, dict) or event.get("type") != "session_meta":
        return {}
    payload = event.get("payload")
    return payload if isinstance(payload, dict) else {}


def codex_children(root: Path, parent_id: str, cwd: str, *, ended=None):
    if not re.fullmatch(r"[a-f0-9-]{36}", parent_id):
        return []
    try:
        parents = list(root.glob(f"*/*/*/rollout-*-{parent_id}.jsonl"))
        if len(parents) != 1:
            return []
        parent = parents[0]
        meta = _metadata(parent)
        if (meta.get("id") != parent_id or not meta.get("cwd")
                or Path(str(meta["cwd"])).resolve() != Path(cwd).resolve()):
            return []
        started = datetime.fromisoformat(str(meta.get("timestamp") or "").replace("Z", "+00:00"))
        ended = ended or datetime.fromtimestamp(parent.stat().st_mtime, timezone.utc)
        # Filename clocks may use local time. Translate using this exact parent's
        # timestamp rather than assuming the host timezone matches old records.
        label_start = parent.name[8:27]
        label_clock = datetime.strptime(label_start, "%Y-%m-%dT%H-%M-%S")
        label_end = (label_clock + (ended - started)).strftime("%Y-%m-%dT%H-%M-%S")
    except (OSError, ValueError, TypeError, AttributeError, KeyError, OverflowError):
        # A parent journal that cannot be read as this call's own (no
        # timestamp, a clock with no zone, a first record that is not an
        # object) links no children. Raising here used to abort the rest of
        # the caller's accounting for the call.
        return []
    children = []
    for candidate in parent.parent.glob("rollout-*.jsonl"):
        if candidate == parent or not label_start <= candidate.name[8:27] <= label_end:
            continue
        try:
            child_meta = _metadata(candidate)
            spawn = child_meta.get("source", {})
            if not isinstance(spawn, dict):
                continue
            link = spawn.get("subagent", {}).get("thread_spawn", {}).get("parent_thread_id")
            if link != parent_id or Path(child_meta.get("cwd", "")).resolve() != Path(cwd).resolve():
                continue
            child_id = child_meta.get("id")
            if not child_id or child_id == parent_id:
                continue
            child = NativeChild(session_id=child_id, parent_session_id=parent_id,
                                tool_name="spawn_agent", detail="linked Codex rollout usage")
            # Forks can carry a prefix of the parent's history. Ordinals below
            # this boundary are inherited, not usage generated by the child.
            boundary = child_meta.get("subagent_history_start_ordinal", 0)
            for event in _events(candidate):
                if event.get("ordinal", boundary) < boundary:
                    continue
                payload = event.get("payload", {})
                if event.get("type") == "turn_context":
                    child.model = payload.get("model") or child.model
                if event.get("type") == "event_msg" and payload.get("type") == "token_count":
                    usage = (payload.get("info") or {}).get("total_token_usage") or {}
                    for field in ("input_tokens", "output_tokens"):
                        value = usage.get(field)
                        if type(value) is int and value >= 0:
                            setattr(child, field, max(getattr(child, field) or 0, value))
            children.append(child)
        except (OSError, ValueError, TypeError, AttributeError):
            continue
    return children

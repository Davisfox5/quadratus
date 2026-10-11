"""Read-only run monitor: what the engine is doing, read off the run directory.

Everything a human would want to know about a run is already written under
``.quadratus/runs/<id>/`` as the run goes (``usage.jsonl``,
``invocations.jsonl``, ``budget.json``, the artifact store) or when it ends
(``result.json``, ``report.md``, ``ledger.md``). Nothing rendered it live,
so the operator was asking the models whether a run was even running. This
module answers that from the files alone.

Three rules, in order of importance:

* **It never writes.** No lock is taken, no file is created, nothing under
  the project or the series directory is touched. The project's ``run.lock``
  is an ``flock`` with no pid in it, and probing it would need a shared lock
  that could make a starting run believe the project is busy, so it is not
  probed at all: a project-only run's liveness is read from whether its
  latest run directory has a ``result.json`` yet and how long ago its files
  were last written. A Stage B series lock does hold a pid, and that one is
  checked with ``os.kill(pid, 0)``.
* **It never raises on bad data.** Every field is the string ``"unknown"``
  with its reason under ``status["unknown"]`` when the file behind it is
  absent, truncated, or malformed. A monitor that crashes on the run it is
  meant to describe is worse than no monitor.
* **Every read is bounded.** JSONL files are tailed (the last
  ``TAIL_BYTES``), JSON files over ``MAX_JSON_BYTES`` are reported as too
  large rather than parsed, and the artifact directory is scanned up to
  ``MAX_ARTIFACT_ENTRIES``. A transcript is never loaded.

Task, stage and seat come from the last *completed* call: the engine appends
to ``invocations.jsonl`` when a call returns, so a call in progress is not
visible and a long one keeps showing its predecessor. ``last_call_ended``
carries the age of that record so the reader can tell.

The stage is inferred: the latest invocation's ``role`` maps to one of plan,
draft, review, gate, capture, close-out (``ROLE_STAGES``), and an artifact
written after that call refines it (``ARTIFACT_STAGES``: a check output means
the gate ran, design evidence means a capture). It is a reading of the
record, not a signal the engine emits, and it says so when it cannot tell.
"""

from __future__ import annotations

import json
import os
import re
import stat
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional

__all__ = [
    "UNKNOWN", "ROLE_STAGES", "ARTIFACT_STAGES", "TAIL_BYTES", "MAX_JSON_BYTES",
    "MAX_USAGE_BYTES", "MAX_ARTIFACT_ENTRIES", "read_status", "run_history",
    "render_text", "render_markdown", "history_rows", "HISTORY_COLUMNS",
]

UNKNOWN = "unknown"

#: How much of a JSONL file is read, from the end. Invocation records run a
#: few hundred bytes each, so this is dozens of recent calls, never a transcript.
TAIL_BYTES = 256 * 1024
#: A JSON file larger than this is reported as too large, never parsed.
MAX_JSON_BYTES = 8 * 1024 * 1024
#: usage.jsonl is summed whole (one ~120-byte line per call) up to this size.
MAX_USAGE_BYTES = 4 * 1024 * 1024
#: The artifact store is scanned for its newest record up to this many entries.
MAX_ARTIFACT_ENTRIES = 5000

#: The invocation role (delegation.InvocationEvent.role) each stage is read from.
ROLE_STAGES: Dict[str, str] = {
    "orchestrator": "plan",
    "requirements-review": "plan",
    "lead": "draft",
    "direct": "draft",
    "security-fix": "draft",
    "revision": "review",
    "verifier": "review",
    "collaborator": "review",
    "consultant": "review",
    "design-review": "review",
    "gate-fix": "gate",
    "design-fix": "capture",
    "closeout": "close-out",
    "auditor": "close-out",
}

#: Artifact kinds whose appearance after the latest call moves the stage on.
ARTIFACT_STAGES: Dict[str, str] = {
    "check-output": "gate",
    "design-evidence": "capture",
    "closeout-evidence": "close-out",
    "closeout-evidence-index": "close-out",
    "closeout-refused": "close-out",
}

HISTORY_COLUMNS = ["run", "started", "ended", "status", "tokens", "stop reason"]

_RUN_STAMP = re.compile(r"^(\d{8}T\d{6}Z)-[0-9a-f]{8}$")
_LOCK_LINE = re.compile(r"pid=(\d+)(?:\s+at=(\S+))?")


# -- bounded readers --------------------------------------------------------

def _size(path: Path) -> Optional[int]:
    try:
        return path.stat().st_size
    except OSError:
        return None


#: Largest token or call count read as a fact. A larger integer is not a count
#: any run produced, and dividing one into a float overflows the renderers.
MAX_COUNT = 10 ** 15


def _count(value: Any) -> Optional[int]:
    """``value`` when it is a plausible count: an int, not a bool, in range."""
    if isinstance(value, int) and not isinstance(value, bool) and 0 <= value < MAX_COUNT:
        return value
    return None


def _read_regular(path: Path, max_bytes: int, tail: bool = False):
    """At most ``max_bytes`` of ``path``, read only when it is a regular file.

    A FIFO, device or socket reports a size of 0 and would block or read
    without bound, so it is refused before any read; the file is opened
    non-blocking and checked through its own descriptor, so a swap after the
    size check is caught too. With ``tail`` the last ``max_bytes`` are read.
    Returns ``(data, offset)``, the offset being where ``data`` starts.
    """
    fd = os.open(path, os.O_RDONLY | getattr(os, "O_NONBLOCK", 0))
    try:
        info = os.fstat(fd)
        if not stat.S_ISREG(info.st_mode):
            raise OSError(f"{path.name} is not a regular file")
        offset = info.st_size - max_bytes if tail and info.st_size > max_bytes else 0
        if offset:
            os.lseek(fd, offset, os.SEEK_SET)
        chunks, left = [], max_bytes
        while left > 0:
            chunk = os.read(fd, min(left, 1 << 16))
            if not chunk:
                break
            chunks.append(chunk)
            left -= len(chunk)
        return b"".join(chunks), offset
    finally:
        os.close(fd)


def _mtime(path: Path) -> Optional[float]:
    try:
        return path.stat().st_mtime
    except OSError:
        return None


def _tail_jsonl(path: Path, max_bytes: int = TAIL_BYTES):
    """The parseable records in the last ``max_bytes`` of a JSONL file.

    Returns ``(records, problem)``; ``problem`` names why nothing could be
    read, or notes how many lines were skipped. A partial first line (the
    cut point) is dropped, never parsed.
    """
    size = _size(path)
    if size is None:
        return [], f"{path.name} is absent"
    if size == 0:
        return [], f"{path.name} is empty"
    try:
        data, offset = _read_regular(path, max_bytes, tail=True)
    except OSError as exc:
        return [], f"{path.name} unreadable: {exc.__class__.__name__}"
    text = data.decode("utf-8", errors="replace")
    lines = text.splitlines()
    if offset > 0 and lines:
        lines = lines[1:]  # the cut line
    records, skipped = [], 0
    for line in lines:
        line = line.strip()
        if not line:
            continue
        try:
            record = json.loads(line)
        except (ValueError, RecursionError):
            skipped += 1
            continue
        if isinstance(record, dict):
            records.append(record)
        else:
            skipped += 1
    if not records:
        return [], f"{path.name} holds no readable records" + (f" ({skipped} malformed)" if skipped else "")
    return records, (f"{skipped} malformed line(s) skipped in {path.name}" if skipped else "")


def _read_json(path: Path, max_bytes: int = MAX_JSON_BYTES):
    """``(object, problem)`` for a JSON file, bounded by size."""
    size = _size(path)
    if size is None:
        return None, f"{path.name} is absent"
    if size > max_bytes:
        return None, f"{path.name} is {size:,} bytes, over the {max_bytes:,} byte read bound"
    try:
        value = json.loads(_read_regular(path, max_bytes + 1)[0].decode("utf-8", errors="replace"))
    except (OSError, ValueError, RecursionError) as exc:
        return None, f"{path.name} unreadable: {exc.__class__.__name__}"
    if not isinstance(value, dict):
        return None, f"{path.name} is not a JSON object"
    return value, ""


def _sum_usage(path: Path):
    """Reported tokens summed over usage.jsonl, or unknown with a reason."""
    size = _size(path)
    if size is None:
        return None, "usage.jsonl is absent"
    if size > MAX_USAGE_BYTES:
        return None, f"usage.jsonl is {size:,} bytes, over the {MAX_USAGE_BYTES:,} byte read bound"
    records, problem = _tail_jsonl(path, max_bytes=size + 1)
    if not records:
        return None, problem or "usage.jsonl holds no records"
    total = 0
    for record in records:
        for key in ("input_tokens", "output_tokens"):
            value = _count(record.get(key))
            if value is not None:
                total += value
    return total, problem


def _first_line(text: Any, limit: int = 200) -> str:
    text = str(text or "").strip()
    line = text.splitlines()[0] if text else ""
    return line if len(line) <= limit else line[: limit - 1] + "…"


def _iso(ts: Optional[float]) -> Optional[str]:
    if ts is None:
        return None
    return datetime.fromtimestamp(ts, timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _stamp_to_iso(run_id: str) -> Optional[str]:
    match = _RUN_STAMP.match(run_id)
    if not match:
        return None
    try:
        when = datetime.strptime(match.group(1), "%Y%m%dT%H%M%SZ")
    except ValueError:
        return None
    return when.strftime("%Y-%m-%dT%H:%M:%SZ")


def _age(seconds: Optional[float]) -> str:
    if seconds is None:
        return "unknown"
    seconds = max(0, int(seconds))
    if seconds < 90:
        return f"{seconds} s ago"
    if seconds < 5400:
        return f"{seconds // 60} min ago"
    return f"{seconds / 3600:.1f} h ago"


# -- the run directory ------------------------------------------------------

def _runs_dir(project_root: Path, state_dir: Optional[Path]) -> Path:
    # Chosen as run_project chooses it (project_run.py): ``~`` expanded, and
    # a relative state dir (the default included) joined to the project once,
    # so a relative project path does not get prefixed twice.
    state = Path(state_dir).expanduser() if state_dir else Path(".quadratus")
    if not state.is_absolute():
        state = project_root / state
    return state / "runs"


def _run_dirs(runs: Path) -> List[Path]:
    """Run directories, newest first by their timestamp name."""
    try:
        with os.scandir(runs) as entries:
            found = [Path(e.path) for e in entries if e.is_dir(follow_symlinks=False)]
    except OSError:
        return []
    return sorted(found, key=lambda p: p.name, reverse=True)


def _newest_artifact(run_dir: Path):
    """``(kind, mtime)`` of the newest artifact record, bounded by entry count."""
    store = run_dir / "artifacts"
    newest, newest_mtime, seen = None, None, 0
    try:
        with os.scandir(store) as entries:
            for entry in entries:
                seen += 1
                if seen > MAX_ARTIFACT_ENTRIES:
                    break
                if not entry.name.endswith(".json"):
                    continue
                try:
                    mtime = entry.stat(follow_symlinks=False).st_mtime
                except OSError:
                    continue
                if newest_mtime is None or mtime > newest_mtime:
                    newest, newest_mtime = Path(entry.path), mtime
    except OSError:
        return None, None
    if newest is None:
        return None, None
    meta, problem = _read_json(newest, max_bytes=64 * 1024)
    if problem or not isinstance(meta, dict):
        return None, newest_mtime
    kind = meta.get("kind")
    return (kind if isinstance(kind, str) else None), newest_mtime


def _last_write(run_dir: Path) -> Optional[float]:
    """The newest mtime among the files a running engine appends to."""
    candidates = [run_dir / name for name in
                  ("invocations.jsonl", "usage.jsonl", "budget.json", "native-children.jsonl", "activity.jsonl")]
    times = [t for t in (_mtime(p) for p in candidates) if t is not None]
    _, artifact_mtime = _newest_artifact(run_dir)
    if artifact_mtime is not None:
        times.append(artifact_mtime)
    return max(times) if times else _mtime(run_dir)


def _pid_alive(pid: int):
    """``(alive, reason)`` from a zero signal; never raises."""
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False, f"pid {pid} is not running"
    except PermissionError:
        return True, f"pid {pid} is running (another user's process)"
    except (OverflowError, ValueError, OSError) as exc:
        return UNKNOWN, f"pid {pid} could not be signalled: {exc.__class__.__name__}"
    return True, f"pid {pid} is running"


def _read_series_lock(series_dir: Path):
    """``(pid, started_at, problem)`` from the Stage B series lock."""
    lock = series_dir / "run.lock"
    size = _size(lock)
    if size is None:
        return None, None, "no run.lock in the series directory"
    if size > 4096:
        return None, None, f"run.lock is {size:,} bytes; not a series lock"
    try:
        text = _read_regular(lock, 4097)[0].decode("utf-8", errors="replace")
    except OSError as exc:
        return None, None, f"run.lock unreadable: {exc.__class__.__name__}"
    match = _LOCK_LINE.search(text)
    if not match:
        return None, None, "run.lock holds no pid= line"
    try:
        pid = int(match.group(1))
    except ValueError:
        return None, None, "run.lock pid is not a number"
    return pid, match.group(2), ""


def _series_cell(manifest: dict):
    """The cell to watch: the running one, else the most recently started."""
    cells = manifest.get("cells")
    if not isinstance(cells, list):
        return None
    cells = [c for c in cells if isinstance(c, dict)]
    running = [c for c in cells if c.get("state") == "running"]
    if running:
        return running[-1]
    started = [c for c in cells if c.get("started_at")]
    if started:
        return max(started, key=lambda c: str(c.get("started_at")))
    return None


# -- the status dict --------------------------------------------------------

def read_status(project_root=None, series_dir=None, *, state_dir=None, now=None) -> Dict[str, Any]:
    """One plain dict describing the latest run, without raising.

    ``project_root`` is the project whose ``.quadratus/runs`` is read.
    ``series_dir`` is a Stage B series directory (``manifest.json`` and
    ``run.lock``); when given, the project defaults to the running cell's
    worktree and the packet's limits are reported. ``state_dir`` overrides
    the ``.quadratus`` location, as ``--state-dir`` does for a run.
    """
    now = time.time() if now is None else now
    status: Dict[str, Any] = {
        "read_at": _iso(now),
        "project": UNKNOWN,
        "series": None,
        "live": UNKNOWN,
        "liveness": UNKNOWN,
        "pid": UNKNOWN,
        "run_id": UNKNOWN,
        "run_dir": UNKNOWN,
        "started": UNKNOWN,
        "last_write": UNKNOWN,
        "cell": UNKNOWN,
        "task": UNKNOWN,
        "stage": UNKNOWN,
        "seat": UNKNOWN,
        "role": UNKNOWN,
        "calls": UNKNOWN,
        "tokens_reported": UNKNOWN,
        "max_reported_tokens": UNKNOWN,
        "reserve_tokens_per_call": UNKNOWN,
        "max_tokens_per_call": UNKNOWN,
        "last_event": UNKNOWN,
        "last_call_ended": UNKNOWN,
        "finished": UNKNOWN,
        "terminal_status": UNKNOWN,
        "stop_reason": UNKNOWN,
        "report": UNKNOWN,
        "unknown": {},
    }
    unknown: Dict[str, str] = status["unknown"]

    def mark(field: str, reason: str) -> None:
        status[field] = UNKNOWN
        unknown[field] = reason

    try:
        _fill(status, mark, project_root, series_dir, state_dir, now)
    except Exception as exc:  # noqa: BLE001 -- a monitor never fails on the run it describes
        mark("liveness", f"monitor error: {exc.__class__.__name__}: {_first_line(exc)}")
    for field, value in list(status.items()):
        if value == UNKNOWN and field not in unknown and field != "unknown":
            unknown[field] = "not determined"
    return status


def _fill(status, mark, project_root, series_dir, state_dir, now) -> None:
    series_project = None
    lock_pid = None
    if series_dir is not None:
        series = Path(series_dir).expanduser()
        info: Dict[str, Any] = {"dir": str(series), "stopped": None, "cells": {}, "lock": UNKNOWN}
        status["series"] = info
        manifest, problem = _read_json(series / "manifest.json")
        if manifest is None:
            mark("cell", problem)
            for field in ("max_reported_tokens", "reserve_tokens_per_call", "max_tokens_per_call"):
                mark(field, problem)
        else:
            limits = ((manifest.get("packet") or {}).get("limits") if isinstance(manifest.get("packet"), dict)
                      else None) or {}
            for field in ("max_reported_tokens", "reserve_tokens_per_call", "max_tokens_per_call"):
                value = _count(limits.get(field)) if isinstance(limits, dict) else None
                if value is not None:
                    status[field] = value
                else:
                    mark(field, f"packet limits carry no integer {field}")
            stopped = manifest.get("stopped")
            if isinstance(stopped, dict):
                info["stopped"] = _first_line(stopped.get("reason"))
            counts: Dict[str, int] = {}
            for cell in manifest.get("cells") or []:
                if isinstance(cell, dict):
                    state = str(cell.get("state") or UNKNOWN)
                    counts[state] = counts.get(state, 0) + 1
            info["cells"] = counts
            cell = _series_cell(manifest)
            if cell is None:
                mark("cell", "manifest lists no started cell")
            else:
                status["cell"] = f"{cell.get('name') or UNKNOWN} [{cell.get('state') or UNKNOWN}]"
                if cell.get("project"):
                    series_project = Path(str(cell["project"]))
        pid, started_at, problem = _read_series_lock(series)
        if pid is None:
            info["lock"] = problem
            mark("pid", problem)
            if manifest is not None:
                status["live"] = False
                status["liveness"] = "no series lock: no series process is running"
        else:
            lock_pid = pid
            status["pid"] = pid
            alive, reason = _pid_alive(pid)
            status["live"] = alive
            status["liveness"] = f"series lock: {reason}" + (f", started {started_at}" if started_at else "")
            info["lock"] = f"pid={pid}" + (f" at={started_at}" if started_at else "")
            if alive is UNKNOWN:
                mark("live", reason)
    else:
        for field in ("max_reported_tokens", "reserve_tokens_per_call", "max_tokens_per_call"):
            mark(field, "no series directory given; the packet is not read")
        mark("cell", "no series directory given")

    root = Path(project_root).expanduser() if project_root else series_project
    if root is None:
        mark("project", "no project folder given and the series names no cell project")
        for field in ("run_id", "run_dir", "started", "last_write", "task", "stage", "seat", "role",
                      "calls", "tokens_reported", "last_event", "finished", "terminal_status",
                      "stop_reason", "report"):
            mark(field, "no project to read")
        if status["live"] == UNKNOWN:
            mark("live", "no project to read")
            mark("liveness", "no project to read")
        return
    status["project"] = str(root)
    if not root.is_dir():
        reason = f"project folder {root} does not exist"
        for field in ("run_id", "run_dir", "started", "last_write", "task", "stage", "seat", "role",
                      "calls", "tokens_reported", "last_event", "finished", "terminal_status",
                      "stop_reason", "report"):
            mark(field, reason)
        if status["live"] == UNKNOWN:
            mark("live", reason)
            mark("liveness", reason)
        return

    runs = _runs_dir(root, state_dir)
    run_dirs = _run_dirs(runs)
    if not run_dirs:
        reason = f"no run directories under {runs}"
        for field in ("run_id", "run_dir", "started", "last_write", "task", "stage", "seat", "role",
                      "calls", "tokens_reported", "last_event", "finished", "terminal_status",
                      "stop_reason", "report"):
            mark(field, reason)
        if status["live"] == UNKNOWN:
            status["live"] = False
            status["liveness"] = reason
        return
    run_dir = run_dirs[0]
    status["run_id"] = run_dir.name
    status["run_dir"] = str(run_dir)
    started = _stamp_to_iso(run_dir.name)
    if started:
        status["started"] = started
    else:
        mark("started", "run directory name carries no timestamp")
    last_write = _last_write(run_dir)
    if last_write is None:
        mark("last_write", "no run files to date")
    else:
        status["last_write"] = f"{_iso(last_write)} ({_age(now - last_write)})"

    result, result_problem = _read_json(run_dir / "result.json")
    status["finished"] = result is not None or (run_dir / "result.json").exists()

    _fill_calls(status, mark, run_dir, now)
    _fill_tokens(status, mark, run_dir)

    if status["finished"]:
        if status["live"] == UNKNOWN or (lock_pid is None and status["live"] is not True):
            status["live"] = False
            status["liveness"] = "latest run has written result.json; it is over"
        _fill_terminal(status, mark, run_dir, result, result_problem)
    else:
        for field in ("terminal_status", "stop_reason"):
            mark(field, "run has not finished")
        report = run_dir / "report.md"
        status["report"] = str(report) if report.exists() else UNKNOWN
        if status["report"] == UNKNOWN:
            mark("report", "no report yet")
        if status["live"] == UNKNOWN:
            age = _age(now - last_write) if last_write is not None else "unknown"
            mark("live", "a project run records no pid; result.json is not written yet "
                          f"and the run files were last written {age}")
            status["liveness"] = status["unknown"]["live"]


def _fill_calls(status, mark, run_dir: Path, now: float) -> None:
    """Task, stage, seat and the last event, read from ``invocations.jsonl``.

    The engine appends a record when a call *returns*, so these fields
    describe the last completed call, never one in progress; a long call
    keeps showing its predecessor until it ends. ``last_call_ended`` says
    how old that record is, so a reader can tell a stale view from a live one.
    """
    events, problem = _tail_jsonl(run_dir / "invocations.jsonl")
    if not events:
        for field in ("task", "stage", "seat", "role", "last_event", "last_call_ended"):
            mark(field, problem)
        return
    latest = events[-1]
    task = latest.get("task")
    status["task"] = str(task) if task else UNKNOWN
    if not task:
        mark("task", "latest invocation names no task")
    role = latest.get("role")
    status["role"] = str(role) if role else UNKNOWN
    if not role:
        mark("role", "latest invocation names no role")
    model = latest.get("canonical_model") or latest.get("resolved_model") or latest.get("requested_model")
    if model:
        status["seat"] = str(model)
    else:
        mark("seat", "latest invocation names no model")
    stage = ROLE_STAGES.get(str(role or ""))
    if latest.get("origin") == "worker" and stage is None:
        stage = "draft"  # workers are commissioned by the lead while it drafts
    kind, artifact_mtime = _newest_artifact(run_dir)
    calls_mtime = _mtime(run_dir / "invocations.jsonl")
    if calls_mtime is None:
        mark("last_call_ended", "invocations.jsonl has no readable mtime")
    else:
        status["last_call_ended"] = f"{_iso(calls_mtime)} ({_age(now - calls_mtime)})"
    if (kind in ARTIFACT_STAGES and artifact_mtime is not None
            and (calls_mtime is None or artifact_mtime >= calls_mtime)):
        stage = ARTIFACT_STAGES[kind]
    if stage:
        status["stage"] = stage
    else:
        mark("stage", f"role {role!r} is not mapped to a stage")
    outcome = latest.get("outcome") or UNKNOWN
    tokens = None
    if isinstance(latest.get("input_tokens"), int) and isinstance(latest.get("output_tokens"), int):
        tokens = latest["input_tokens"] + latest["output_tokens"]
    seconds = latest.get("seconds")
    bits = [f"{task or '?'}/{role or '?'}", str(model or "(unresolved)"),
            "invoked" if latest.get("invoked") else "selected, never invoked", str(outcome),
            f"{seconds:.0f} s" if isinstance(seconds, (int, float)) else "duration unknown",
            f"{tokens:,} tokens" if tokens is not None else "usage unknown"]
    if isinstance(latest.get("attempt"), int) and latest["attempt"] > 1:
        bits.append(f"attempt {latest['attempt']}")
    detail = _first_line(latest.get("detail"), 120)
    if detail:
        bits.append(detail)
    status["last_event"] = " | ".join(bits)
    if problem:
        status["unknown"].setdefault("last_event_note", problem)


def _fill_tokens(status, mark, run_dir: Path) -> None:
    budget, problem = _read_json(run_dir / "budget.json")
    if budget is not None:
        reported = _count(budget.get("reported_tokens"))
        if reported is not None:
            status["tokens_reported"] = reported
        else:
            mark("tokens_reported", "budget.json carries no plausible integer reported_tokens")
        calls = _count(budget.get("reserved_attempts"))
        if calls is not None:
            status["calls"] = calls
        else:
            mark("calls", "budget.json carries no integer reserved_attempts")
        if status["max_reported_tokens"] == UNKNOWN:
            limits = budget.get("limits")
            if isinstance(limits, dict):
                for field in ("max_reported_tokens", "reserve_tokens_per_call", "max_tokens_per_call"):
                    value = _count(limits.get(field))
                    if value is not None:
                        status[field] = value
                        status["unknown"].pop(field, None)
        return
    total, usage_problem = _sum_usage(run_dir / "usage.jsonl")
    if total is None:
        mark("tokens_reported", f"{problem}; {usage_problem}")
    else:
        status["tokens_reported"] = total
    events, _ = _tail_jsonl(run_dir / "invocations.jsonl")
    size = _size(run_dir / "invocations.jsonl")
    if events and size is not None and size <= TAIL_BYTES:
        status["calls"] = len(events)
    else:
        mark("calls", f"{problem}; invocations.jsonl " + ("is absent" if size is None else "exceeds the tail bound"))


def _outcome(result: Dict[str, Any], open_reason: str):
    """``(status, stop_reason)`` for a run's result.json.

    A run given an explicit task list (``explicit_tasks``) reports
    ``completed`` when the listed tasks closed, and the goal was never judged
    (project_run's report says so in its title); that is not a completed
    goal, and the monitor must not call it one."""
    error = _first_line(result.get("error"))
    completed = result.get("completed") is True
    explicit = result.get("explicit_tasks")
    if completed and isinstance(explicit, dict) and explicit.get("goal_judged") is not True:
        return "listed tasks complete", "listed tasks complete; the goal was not judged"
    if completed:
        return "complete", "goal reported complete"
    if error:
        return "error", error
    budget = result.get("budget")
    stop = budget.get("stop_reason") if isinstance(budget, dict) else None
    return "incomplete", (f"budget: {_first_line(stop)}" if stop else open_reason)


def _fill_terminal(status, mark, run_dir: Path, result, problem) -> None:
    report = run_dir / "report.md"
    status["report"] = str(report) if report.exists() else UNKNOWN
    if status["report"] == UNKNOWN:
        mark("report", "report.md is absent")
    if result is None:
        mark("terminal_status", problem)
        mark("stop_reason", problem)
        return
    status["terminal_status"], status["stop_reason"] = _outcome(
        result, "the plan was declined, the task limit was reached, or findings/checks remain open")


# -- history ----------------------------------------------------------------

def run_history(project_root, *, state_dir=None, limit: int = 10) -> List[Dict[str, Any]]:
    """The last ``limit`` runs, newest first; one bounded read per run."""
    try:
        root = Path(project_root).expanduser()
        run_dirs = _run_dirs(_runs_dir(root, state_dir))[: max(0, int(limit))]
    except Exception:  # noqa: BLE001 -- bad input reads as no history
        return []
    rows = []
    for run_dir in run_dirs:
        try:
            rows.append(_history_row(run_dir))
        except Exception as exc:  # noqa: BLE001 -- one bad run never hides the others
            rows.append({"run": run_dir.name, "started": _stamp_to_iso(run_dir.name) or UNKNOWN,
                         "ended": UNKNOWN, "status": UNKNOWN, "tokens": UNKNOWN,
                         "stop_reason": f"unreadable: {exc.__class__.__name__}", "path": str(run_dir)})
    return rows


def _history_row(run_dir: Path) -> Dict[str, Any]:
    row: Dict[str, Any] = {"run": run_dir.name, "started": _stamp_to_iso(run_dir.name) or UNKNOWN,
                           "ended": UNKNOWN, "status": UNKNOWN, "tokens": UNKNOWN,
                           "stop_reason": UNKNOWN, "path": str(run_dir)}
    result_path = run_dir / "result.json"
    result, problem = _read_json(result_path)
    if result is None and not result_path.exists():
        row["status"] = "unfinished"
        row["stop_reason"] = "no result.json"
    elif result is None:
        row["ended"] = _iso(_mtime(result_path)) or UNKNOWN
        row["status"] = UNKNOWN
        row["stop_reason"] = problem
    else:
        row["ended"] = _iso(_mtime(result_path)) or UNKNOWN
        row["status"], row["stop_reason"] = _outcome(result, "open findings, checks or task limit")
    budget, _ = _read_json(run_dir / "budget.json")
    reported = _count(budget.get("reported_tokens")) if isinstance(budget, dict) else None
    if reported is not None:
        row["tokens"] = reported
    else:
        total, _ = _sum_usage(run_dir / "usage.jsonl")
        if total is not None:
            row["tokens"] = total
    return row


def history_rows(history: List[Dict[str, Any]]) -> List[List[Any]]:
    """The history as table rows under ``HISTORY_COLUMNS``."""
    return [[r.get("run", UNKNOWN), r.get("started", UNKNOWN), r.get("ended", UNKNOWN),
             r.get("status", UNKNOWN), r.get("tokens", UNKNOWN), r.get("stop_reason", UNKNOWN)]
            for r in history]


# -- rendering --------------------------------------------------------------

def _fmt(value: Any) -> str:
    if isinstance(value, bool):
        return "yes" if value else "no"
    if isinstance(value, int):
        return f"{value:,}"
    return str(value)


def _tokens_line(status: Dict[str, Any]) -> str:
    spent, cap = status["tokens_reported"], status["max_reported_tokens"]
    if isinstance(spent, int) and isinstance(cap, int) and cap > 0:
        return f"{spent:,} of {cap:,} ({100 * spent / cap:.0f}%)"
    if isinstance(spent, int):
        return f"{spent:,}" + ("" if cap == UNKNOWN else f" of {_fmt(cap)}")
    return UNKNOWN if cap == UNKNOWN else f"{UNKNOWN} of {_fmt(cap)}"


def _live_word(status: Dict[str, Any]) -> str:
    live = status["live"]
    if live is True:
        return "LIVE"
    if live is False:
        return "not running"
    return UNKNOWN


def render_text(status: Dict[str, Any], history: Optional[List[Dict[str, Any]]] = None) -> str:
    """One screen of plain text for a terminal over ssh."""
    lines = [f"Quadratus monitor  {status.get('read_at', '')}",
             f"Project:   {status['project']}"]
    series = status.get("series")
    if isinstance(series, dict):
        cells = ", ".join(f"{k} {v}" for k, v in sorted(series.get("cells", {}).items())) or "none"
        lines.append(f"Series:    {series.get('dir')}  lock: {series.get('lock')}")
        lines.append(f"Cells:     {cells}" + (f"  STOPPED: {series['stopped']}" if series.get("stopped") else ""))
    lines += [
        f"Run:       {_live_word(status)}  {status['liveness']}",
        f"Run id:    {status['run_id']}  started {status['started']}",
        f"Last write {status['last_write']}",
        f"Cell:      {status['cell']}",
        f"Task:      {status['task']}  stage {status['stage']}",
        f"Seat:      {status['seat']}  role {status['role']}",
        f"Tokens:    {_tokens_line(status)}  calls {_fmt(status['calls'])}",
        f"Per call:  reserve {_fmt(status['reserve_tokens_per_call'])}  ceiling {_fmt(status['max_tokens_per_call'])}",
        f"Last:      {status['last_event']}",
        f"Recorded:  last call ended {status['last_call_ended']}; a call in progress is not written until it returns",
    ]
    if status.get("finished") is True:
        lines += [f"Ended:     {status['terminal_status']}: {status['stop_reason']}",
                  f"Report:    {status['report']}"]
    unknown = {k: v for k, v in status.get("unknown", {}).items() if k != "last_event_note"}
    if unknown:
        lines.append("Unknown:   " + "; ".join(f"{k}: {v}" for k, v in unknown.items()))
    if history:
        lines += ["", "Recent runs:"]
        for row in history:
            lines.append(f"  {row['run']}  {row['started']}  ended {row['ended']}  "
                         f"{row['status']}  {_fmt(row['tokens'])} tokens  {row['stop_reason']}")
    return "\n".join(lines)


def render_markdown(status: Dict[str, Any]) -> str:
    """The same status for the GUI tab."""
    live = _live_word(status)
    lines = [f"### Run: {live}", f"_{status['liveness']}_", ""]
    series = status.get("series")
    if isinstance(series, dict):
        cells = ", ".join(f"{k} {v}" for k, v in sorted(series.get("cells", {}).items())) or "none"
        lines += [f"**Series:** `{series.get('dir')}` lock {series.get('lock')}  ", f"**Cells:** {cells}  "]
        if series.get("stopped"):
            lines.append(f"**Series stopped:** {series['stopped']}  ")
    lines += [
        f"**Project:** `{status['project']}`  ",
        f"**Run id:** {status['run_id']} started {status['started']}  ",
        f"**Last write:** {status['last_write']}  ",
        f"**Cell:** {status['cell']}  ",
        f"**Task:** {status['task']} (stage {status['stage']})  ",
        f"**Seat:** {status['seat']} (role {status['role']})  ",
        f"**Tokens:** {_tokens_line(status)}; calls {_fmt(status['calls'])}  ",
        f"**Per call:** reserve {_fmt(status['reserve_tokens_per_call'])}, "
        f"ceiling {_fmt(status['max_tokens_per_call'])}  ",
        f"**Last event:** {status['last_event']}  ",
        f"**Last call ended:** {status['last_call_ended']} (a call in progress is not written until it returns)  ",
    ]
    if status.get("finished") is True:
        lines += [f"**Ended:** {status['terminal_status']}: {status['stop_reason']}  ",
                  f"**Report:** `{status['report']}`  "]
    unknown = {k: v for k, v in status.get("unknown", {}).items() if k != "last_event_note"}
    if unknown:
        lines += ["", "<details><summary>Unknown fields</summary>", ""]
        lines += [f"- {k}: {v}" for k, v in unknown.items()]
        lines += ["", "</details>"]
    lines += ["", f"_Read at {status.get('read_at', '')}_"]
    return "\n".join(lines)

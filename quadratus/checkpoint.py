"""Durable quiescent checkpoints and the episodes that resume them (step 2).

A run writes ``checkpoint.json`` at every quiescent point: between tasks, with
no model call in flight and every closed task's record settled. A later
episode may resume a run whose process ended there (an interrupted run), and
only that. The checkpoint holds:

* the job's lineage (root run, parent run, episode number);
* the identities the work was measured against: source, goal, policy, the
  selected limits and settings, the configured gate and the dependency tree;
* the session state that makes the record of the job, carried exactly: the
  task history and ledger, requirements, findings and check debt, unfinished
  tasks and their baselines, typed outcomes, and the run-start test bytes.

Nothing is re-judged on resume. Verified work stays verified because its
record and its source are unchanged; unfinished work stays unfinished and
unaccepted; check debt and open findings stay open. The job's allowance is
never reset: the new episode starts from what the budget record says was
spent, to the end of the parent's process, not from what the checkpoint saw.

Every doubt refuses (``CheckpointRefused``) before any call: a sealed or
already-resumed run, a call in flight or unknown usage at the end, a
checkpoint whose digest does not match its body, a source, goal, policy,
limits, settings, gate or dependency identity that differs from now, state
the checkpoint does not carry, or no allowance left. A refusal is never a
partial resume.
"""

from __future__ import annotations

import base64
import dataclasses
import hashlib
import json
import logging
import os
from pathlib import Path
from typing import Dict, List, Optional

log = logging.getLogger(__name__)

__all__ = ["CheckpointRefused", "VERSION", "capture_state", "restore_state", "write", "load",
           "validate", "identities", "settings_identity", "consumed", "seal", "claim"]

VERSION = 1
NAME = "checkpoint.json"
RESUMED = "resumed-by.json"

#: Session attributes carried verbatim (JSON-safe after encoding).
_CARRIED = (
    "checks", "review_checks", "open_findings", "findings", "failed", "turn_limited",
    "failed_records", "turn_limited_records", "survey", "decisions", "unexecuted_acceptance",
    "original_test_runs", "requirement_audits", "requirement_reviews", "verification_handoffs",
    "verification_dispatched", "policy_plans", "parallel_batches", "_partial_tasks", "_cap_baselines",
    "_turn_limited_in_a_row", "_requirement_reopens", "_covers_corrections", "_original_tests",
    "_run_start_contents", "_original_selection",
)
#: State a checkpoint does not carry. Any of it present makes the point not
#: resumable, rather than resuming a job whose record would be incomplete.
_NOT_CARRIED = (
    "design_checks", "_design_unverified", "test_repairs", "skip_baselines", "_batch",
    "_capture_receipts", "_review_evidence", "in_flight", "explicit",
)


class CheckpointRefused(RuntimeError):
    """A run cannot be resumed from this checkpoint; nothing was resumed."""


# -- encoding -------------------------------------------------------------------------

def _encode(value):
    if isinstance(value, bytes):
        return {"__b64__": base64.b64encode(value).decode("ascii")}
    if isinstance(value, (set, frozenset)):
        return {"__set__": sorted((_encode(v) for v in value), key=repr)}
    if isinstance(value, tuple):
        return {"__tuple__": [_encode(v) for v in value]}
    if isinstance(value, dict):
        if not all(isinstance(k, str) for k in value):
            raise TypeError("checkpoint state keys must be strings")
        return {k: _encode(v) for k, v in value.items()}
    if isinstance(value, list):
        return [_encode(v) for v in value]
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    if dataclasses.is_dataclass(value):
        return _encode(dataclasses.asdict(value))
    raise TypeError(f"checkpoint state holds an unencodable {type(value).__name__}")


def _decode(value):
    if isinstance(value, dict):
        if set(value) == {"__b64__"}:
            return base64.b64decode(value["__b64__"])
        if set(value) == {"__set__"}:
            return {_decode(v) for v in value["__set__"]}
        if set(value) == {"__tuple__"}:
            return tuple(_decode(v) for v in value["__tuple__"])
        return {k: _decode(v) for k, v in value.items()}
    if isinstance(value, list):
        return [_decode(v) for v in value]
    return value


def _canonical(body) -> bytes:
    return json.dumps(body, sort_keys=True, separators=(",", ":")).encode()


def _digest(body) -> str:
    return hashlib.sha256(_canonical(body)).hexdigest()


# -- session state ----------------------------------------------------------------------

def _uncarried(session) -> List[str]:
    present = []
    for name in _NOT_CARRIED:
        value = getattr(session, name, None)
        if value:
            present.append(name)
    if getattr(session.config, "repair_tests", None):
        present.append("repair_tests")
    if getattr(session, "stop_reason", ""):
        present.append("stop_reason")
    return present


def capture_state(session) -> dict:
    """The carried session state, or a dict naming why it cannot be carried
    (``{"uncarried": [...]}``)."""
    missing = _uncarried(session)
    if missing:
        return {"uncarried": missing}
    ledger = session.memory.ledger
    state = {
        "history": [dataclasses.asdict(s) for s in session.history],
        "ledger": dict(entries=[dataclasses.asdict(e) for e in ledger.entries], rulings=list(ledger.rulings),
                       requirements=dict(ledger.requirements),
                       requirement_status=dict(ledger.requirement_status),
                       ambiguous=dict(ledger.ambiguous), ambiguous_since=dict(ledger.ambiguous_since),
                       decisions=dict(ledger.decisions)),
        "task_outcomes": [dataclasses.asdict(o) for o in session.task_outcomes],
        "breaker_counted": [o.task_id for o in getattr(session, "_breaker_counted", [])],
        "slots_used": int(getattr(session, "_slots_used", 0)),
    }
    for name in _CARRIED:
        state[name] = getattr(session, name, None)
    try:
        return _encode(state)
    except TypeError as exc:
        return {"uncarried": [f"state: {exc}"]}


def restore_state(session, state: dict) -> None:
    """Put carried state back on a fresh session, before its run starts."""
    from .artifacts import ArtifactRef
    from .memory import TaskSummary
    from .outcome import Fact, TaskOutcome

    state = _decode(state)

    def refs(items):
        return [ArtifactRef(**r) for r in items or []]

    session.history = [TaskSummary(**dict(s, refs=refs(s.get("refs")))) for s in state["history"]]
    ledger = session.memory.ledger
    data = state["ledger"]
    for entry in data["entries"]:
        ledger.append(task_id=entry["task_id"], author=entry["author"], summary=entry["summary"],
                      reasoning=entry["reasoning"], dead_ends=entry["dead_ends"], refs=refs(entry["refs"]),
                      harness=entry["harness"])
    ledger.rulings = list(data["rulings"])
    ledger.requirements = dict(data["requirements"])
    ledger.requirement_status = dict(data["requirement_status"])
    ledger.ambiguous = dict(data["ambiguous"])
    ledger.ambiguous_since = dict(data["ambiguous_since"])
    ledger.decisions = dict(data["decisions"])
    outcomes = []
    for item in state["task_outcomes"]:
        item = dict(item)
        item["facts"] = [Fact(**f) for f in item.get("facts") or []]
        outcomes.append(TaskOutcome(**item))
    session.task_outcomes = outcomes
    by_id = {o.task_id: o for o in outcomes}
    session._breaker_counted = [by_id[t] for t in state["breaker_counted"] if t in by_id]
    session._slots_used = int(state["slots_used"])
    for name in _CARRIED:
        setattr(session, name, state[name])
    session._carried_baselines = dict(_original_tests=state["_original_tests"],
                                      _run_start_contents=state["_run_start_contents"],
                                      _original_selection=state["_original_selection"])


# -- identities and budget ------------------------------------------------------------

def settings_identity(settings) -> str:
    """A digest of the settings that choose models, roles and transport.
    Fields that name a key, token, secret or password are left out: the
    digest is written to disk and a credential never is."""
    if settings is None:
        return "none"
    try:
        fields = dataclasses.asdict(settings) if dataclasses.is_dataclass(settings) else dict(vars(settings))
    except TypeError:
        fields = {}
    kept = {k: v for k, v in fields.items()
            if not any(word in k.lower() for word in ("key", "token", "secret", "password"))}
    return hashlib.sha256(json.dumps(kept, sort_keys=True, default=repr).encode()).hexdigest()


def identities(*, goal, source, policy, limits, settings, gate, dependency) -> Dict[str, str]:
    return dict(goal=hashlib.sha256(str(goal).encode()).hexdigest(), source=str(source or "unavailable"),
                policy=str(policy or "none"), limits=str(limits), settings=str(settings),
                gate=str(gate or "none"), dependency=str(dependency or "unavailable"))


def consumed(budget_record: Optional[dict]) -> dict:
    """What the job has spent, from a budget record (budget.json)."""
    record = budget_record or {}
    return dict(calls=int(record.get("reserved_attempts") or 0),
                input_tokens=int(record.get("input_tokens") or 0),
                output_tokens=int(record.get("output_tokens") or 0),
                api_cost_usd=float(record.get("api_cost_usd") or 0.0),
                elapsed_seconds=float(record.get("elapsed_seconds") or 0.0))


# -- the file --------------------------------------------------------------------------

def write(run_dir, body: dict) -> None:
    """Write the checkpoint atomically with the digest of its body. A write
    that fails removes the checkpoint rather than leave an older one that a
    resume could mistake for this point."""
    target = Path(run_dir) / NAME
    temporary = target.with_suffix(".tmp")
    try:
        temporary.write_text(json.dumps(dict(body=body, digest=_digest(body))) + "\n")
        os.replace(temporary, target)
    except OSError:
        log.warning("could not write the checkpoint; removing the old one", exc_info=True)
        for path in (temporary, target):
            try:
                path.unlink()
            except OSError:
                pass


def seal(run_dir, reason: str) -> None:
    """Mark a finished run's checkpoint sealed: a run that ended on its own
    terms (complete, stopped, refused) is never resumed."""
    target = Path(run_dir) / NAME
    try:
        data = json.loads(target.read_text())
        body = data["body"]
    except (OSError, ValueError, KeyError, TypeError):
        return
    if data.get("digest") != _digest(body):
        return
    write(run_dir, dict(body, sealed=True, resumable=False, sealed_reason=str(reason)[:400]))


def load(run_dir) -> dict:
    """The checkpoint body, digest verified, or CheckpointRefused."""
    target = Path(run_dir) / NAME
    try:
        data = json.loads(target.read_text())
        body = data["body"]
        digest = data["digest"]
    except (OSError, ValueError, KeyError, TypeError) as exc:
        raise CheckpointRefused(f"no readable checkpoint in {Path(run_dir).name}: {type(exc).__name__}") from exc
    if not isinstance(body, dict) or digest != _digest(body):
        raise CheckpointRefused("the checkpoint does not match its digest; it was altered or truncated")
    if body.get("version") != VERSION:
        raise CheckpointRefused(f"checkpoint version {body.get('version')!r} is not {VERSION}")
    return body


def validate(run_dir, body: dict, now: Dict[str, str]) -> dict:
    """Refuse unless ``body`` may be resumed now; return the job's consumed
    allowance to carry (from the parent's own budget record)."""
    run_dir = Path(run_dir)
    if body.get("sealed"):
        raise CheckpointRefused(f"the run is sealed: {body.get('sealed_reason') or 'it finished'}")
    if not body.get("resumable"):
        raise CheckpointRefused("the checkpoint is not resumable: "
                                + ", ".join(body.get("uncarried") or ["no carried state"]))
    if (run_dir / RESUMED).exists():
        raise CheckpointRefused("the run was already resumed; resume its latest episode instead")
    try:
        budget = json.loads((run_dir / "budget.json").read_text())
    except (OSError, ValueError) as exc:
        raise CheckpointRefused(f"the parent's budget record is unreadable: {type(exc).__name__}") from exc
    if budget.get("in_flight"):
        raise CheckpointRefused(f"{budget['in_flight']} call(s) were in flight when the run ended; their "
                                "usage and effect are unknown")
    if budget.get("unknown_usage_attempts") or budget.get("stop_reason"):
        raise CheckpointRefused("the parent's budget stopped or holds unknown usage: "
                                f"{budget.get('stop_reason') or 'unknown_usage'}")
    spent = consumed(budget)
    if spent["calls"] < int(body.get("consumed", {}).get("calls", 0)):
        raise CheckpointRefused("the budget record holds fewer calls than the checkpoint; it is not the "
                                "record this checkpoint was written against")
    held = body.get("identities") or {}
    differing = sorted(k for k in set(held) | set(now) if held.get(k) != now.get(k))
    if differing:
        raise CheckpointRefused("these differ from the checkpoint: " + ", ".join(differing)
                                + ". A resumed episode must meet the same source, goal, policy, "
                                "limits, settings, gate and dependency tree")
    return spent


def claim(run_dir, child_run_dir, episode: int) -> None:
    """Record that ``run_dir`` was resumed, before the episode makes a call:
    a second resume of the same parent is refused (one lineage, no forks)."""
    target = Path(run_dir) / RESUMED
    try:
        fd = os.open(target, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o644)
    except FileExistsError as exc:
        raise CheckpointRefused("the run was already resumed; resume its latest episode instead") from exc
    with os.fdopen(fd, "w") as handle:
        handle.write(json.dumps(dict(child=Path(child_run_dir).name, episode=episode)) + "\n")

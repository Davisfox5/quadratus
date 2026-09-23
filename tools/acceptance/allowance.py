"""Admit canary runs against one Davis allowance record, and remember each one.

The record says how many runs each version may have and how many reported
tokens the whole batch may spend. A slot is claimed in a ledger beside the
record (``<record>.slots.json``) before any launch and closed with the run's
own budget afterwards, so re-running a command cannot mint slots the record
never granted, and a run whose usage is unknown stops the batch.
"""

from __future__ import annotations

import fcntl
import hashlib
import json
import os
import re
import tempfile
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path

SCHEMA = "quadratus-canary-allowance/2"
ENVIRONMENTS = {"native-mac", "container-contained"}
VERSIONS = {"baseline", "candidate"}
SHA = re.compile(r"[0-9a-f]{40}")
SHA256 = re.compile(r"[0-9a-f]{64}")
LIMITS = ("max_calls_each", "max_reported_tokens_each", "internal_wall_seconds_each",
          "external_wall_seconds_each", "max_reported_tokens_batch")
REQUIRED = ("schema", "approved", "batch_id", "authorized_by", "fixture", "grader_sha256", "source", "instruction", "recorded_at",
            "environment", "baseline_sha", "candidate_sha", "runs", "runs_per_version",
            *LIMITS)


API_CREDENTIAL_NAMES = frozenset({
    "ANTHROPIC_API_KEY", "OPENAI_API_KEY", "XAI_API_KEY", "GROK_API_KEY",
    "GROK_DEPLOYMENT_KEY", "GEMINI_API_KEY", "GOOGLE_API_KEY",
})
API_CREDENTIAL_SUFFIXES = ("_API_KEY", "_DEPLOYMENT_KEY", "_API_TOKEN")


def scrub_api_credentials(env: dict) -> tuple:
    """A copy of ``env`` without API transport credentials, and the names removed.

    A canary runs on subscription CLIs only. ``Settings(*_api_key=None)`` keeps
    the engine off billed transport, but the vendor CLIs read their own
    variables (grok answers on ``XAI_API_KEY`` when it is set), so the child
    environment must not carry them. Sign-in stores on disk are untouched.
    Values are never returned or printed, only names."""
    removed = sorted(name for name in env
                     if name in API_CREDENTIAL_NAMES or name.endswith(API_CREDENTIAL_SUFFIXES))
    return {k: v for k, v in env.items() if k not in removed}, removed


def _refuse(field: str, why: str):
    raise SystemExit(f"allowance record refused at {field}: {why}")


def _positive_int(value) -> bool:
    return isinstance(value, int) and not isinstance(value, bool) and value > 0


def load_record(path) -> dict:
    """The record, or SystemExit naming the first field that fails. Any missing
    key fails closed."""
    if not path:
        raise SystemExit("A direct Davis allowance record is required")
    try:
        record = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise SystemExit(f"A direct Davis allowance record is required ({exc})") from exc
    if not isinstance(record, dict):
        _refuse("record", "not a JSON object")
    for key in REQUIRED:
        if key not in record:
            _refuse(key, "missing")
    if record["schema"] != SCHEMA:
        _refuse("schema", f"expected {SCHEMA!r}")
    if record["approved"] is not True:
        _refuse("approved", "must be true")
    if record["authorized_by"] != "Davis":
        _refuse("authorized_by", "must be 'Davis'")
    for key in ("batch_id", "source", "recorded_at", "instruction", "fixture"):
        if not isinstance(record[key], str) or not record[key].strip():
            _refuse(key, "empty")
    if not isinstance(record["grader_sha256"], str) or not SHA256.fullmatch(record["grader_sha256"]):
        _refuse("grader_sha256", "must be the 64 hex sha256 of the grader file")
    if record["environment"] not in ENVIRONMENTS:
        _refuse("environment", f"must be one of {sorted(ENVIRONMENTS)}")
    for key in ("baseline_sha", "candidate_sha"):
        if not isinstance(record[key], str) or not SHA.fullmatch(record[key]):
            _refuse(key, "must be 40 lowercase hex characters")
    if record["baseline_sha"] == record["candidate_sha"]:
        _refuse("candidate_sha", "equals baseline_sha")
    runs = record["runs"]
    if not isinstance(runs, list) or not runs or not all(r in VERSIONS for r in runs):
        _refuse("runs", f"must be a non-empty list drawn from {sorted(VERSIONS)}")
    per = record["runs_per_version"]
    if not isinstance(per, int) or isinstance(per, bool) or not 1 <= per <= 5:
        _refuse("runs_per_version", "must be an integer from 1 to 5")
    for key in LIMITS:
        if not _positive_int(record[key]):
            _refuse(key, "must be a positive integer")
    return record


def expected_sha(record: dict, version: str) -> str:
    if version not in VERSIONS:
        raise SystemExit(f"unknown version {version!r}")
    return record[f"{version}_sha"]


def check_runtime(record: dict, version: str, commit: str) -> None:
    if version not in record["runs"]:
        raise SystemExit(f"the allowance record grants no {version} runs")
    want = expected_sha(record, version)
    if commit == "unknown" or commit != want:
        raise SystemExit(f"runtime commit {commit} is not the {version} commit the "
                         f"allowance names ({want})")


def check_wall(record: dict, wall_seconds) -> None:
    if wall_seconds != record["external_wall_seconds_each"]:
        raise SystemExit(f"--wall-seconds {wall_seconds} differs from the allowance's "
                         f"external_wall_seconds_each {record['external_wall_seconds_each']}")


def grader_digest(path) -> str:
    """sha256 of the grader file's bytes, or SystemExit when it cannot be read."""
    try:
        return hashlib.sha256(Path(path).read_bytes()).hexdigest()
    except OSError as exc:
        raise SystemExit(f"fixture grader unreadable at {path} ({exc})") from exc


def check_grader(record: dict, fixture) -> Path:
    """The fixture copy's manifest must name the grader the record authorizes,
    and the grader file's bytes must hash to that figure right now.

    ``prepare.py`` writes ``.quadratus/fixture-manifest.json`` with the grader's
    path and the sha256 of the instrument directory's ``test_contract.py``. The
    manifest's string is a claim; the bytes are the check. A fixture without
    the manifest, a grader inside the solver tree, or a grader whose bytes hash
    to anything other than the record's figure, refuses. Returns the grader
    path so the caller runs exactly that file."""
    fixture = Path(fixture)
    path = fixture / ".quadratus" / "fixture-manifest.json"
    try:
        manifest = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise SystemExit(f"fixture has no readable manifest at {path} ({exc})") from exc
    hashes = manifest.get("instrument_sha256") if isinstance(manifest, dict) else None
    claimed = hashes.get("test_contract.py") if isinstance(hashes, dict) else None
    if claimed != record["grader_sha256"]:
        raise SystemExit(f"fixture grader sha256 {claimed} is not the allowance's "
                         f"grader_sha256 {record['grader_sha256']}")
    grader = manifest.get("grader") if isinstance(manifest, dict) else None
    if not isinstance(grader, str) or not grader.strip():
        raise SystemExit("fixture manifest names no grader file")
    grader = Path(grader).resolve()
    if fixture.resolve() in grader.parents:
        raise SystemExit(f"fixture grader {grader} sits inside the solver tree")
    verify_grader_bytes(record, grader)
    return grader


def verify_grader_bytes(record: dict, grader) -> None:
    """The grader file on disk, hashed now, must be the record's grader."""
    actual = grader_digest(grader)
    if actual != record["grader_sha256"]:
        raise SystemExit(f"grader file {grader} hashes to {actual}, not the allowance's "
                         f"grader_sha256 {record['grader_sha256']}")


def ledger_path(record_path) -> Path:
    return Path(f"{record_path}.slots.json")


def lock_path(record_path) -> Path:
    return Path(f"{record_path}.slots.lock")


@contextmanager
def _ledger_lock(record_path):
    """An exclusive lock on a stable file beside the ledger for the whole
    read, check and write of one claim or close. The ledger itself is
    replaced on every write, so its inode cannot carry the lock."""
    path = lock_path(record_path)
    with path.open("a+") as handle:
        fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(handle.fileno(), fcntl.LOCK_UN)


def record_sha256(record_path) -> str:
    return hashlib.sha256(Path(record_path).read_bytes()).hexdigest()


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _write(path: Path, ledger: dict) -> None:
    fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=path.name, suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            json.dump(ledger, fh, indent=2)
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(tmp, path)
    except BaseException:
        Path(tmp).unlink(missing_ok=True)
        raise


def read_ledger(record_path) -> dict:
    """The ledger for this record, or a fresh one when none exists yet. A
    ledger written against different record bytes is refused."""
    path = ledger_path(record_path)
    digest = record_sha256(record_path)
    if not path.exists():
        return {"record_sha256": digest, "batch_id": None, "slots": []}
    try:
        ledger = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise SystemExit(f"slot ledger {path} is unreadable ({exc})") from exc
    if ledger.get("record_sha256") != digest:
        raise SystemExit("ledger belongs to a different allowance record")
    return ledger


def check_batch(record: dict, ledger: dict) -> None:
    if ledger.get("batch_id") not in (None, record["batch_id"]):
        raise SystemExit(f"ledger belongs to batch {ledger.get('batch_id')}, "
                         f"not {record['batch_id']}")
    ledger["batch_id"] = record["batch_id"]


def slots_left(record_path, record: dict, version: str) -> int:
    used = sum(1 for s in read_ledger(record_path)["slots"] if s["version"] == version)
    return max(record["runs_per_version"] - used, 0)


def _check_usage(record: dict, slots: list) -> None:
    for s in slots:
        if (s.get("finished_at") is None or s.get("reported_tokens") is None
                or s.get("unknown_usage_attempts") != 0):
            raise SystemExit(f"usage of slot {s['version']}-{s['attempt']} is unknown; "
                             "no further run is admitted")
    spent = sum(s["reported_tokens"] for s in slots)
    each, batch = record["max_reported_tokens_each"], record["max_reported_tokens_batch"]
    if spent + each > batch:
        raise SystemExit(f"batch ceiling: {spent} reported tokens spent + {each} for the next "
                         f"run exceeds max_reported_tokens_batch {batch}")


def claim_slot(record_path, record: dict, version: str) -> dict:
    """Record a slot as consumed before the launch, under the ledger lock so
    two concurrent claims cannot both be admitted. The ledger is written
    atomically, so a killed series still shows the slot it took."""
    with _ledger_lock(record_path):
        try:
            on_disk = json.loads(Path(record_path).read_text(encoding="utf-8"))
        except (OSError, ValueError) as exc:
            raise SystemExit(f"allowance record unreadable at claim ({exc})") from exc
        if on_disk != record:
            raise SystemExit("allowance record changed after it was loaded")
        ledger = read_ledger(record_path)
        check_batch(record, ledger)
        mine = [s for s in ledger["slots"] if s["version"] == version]
        total = record["runs_per_version"]
        if len(mine) >= total:
            raise SystemExit(f"no run slot left for {version}: {len(mine)} of {total} consumed")
        _check_usage(record, ledger["slots"])
        slot = {"version": version, "attempt": len(mine) + 1, "run_dir": None,
                "started_at": _now(), "finished_at": None, "reported_tokens": None,
                "unknown_usage_attempts": None}
        ledger["slots"].append(slot)
        _write(ledger_path(record_path), ledger)
        return slot


def close_slot(record_path, slot: dict, run_dir) -> dict:
    """Fill the slot from the run's own budget.json, under the ledger lock. A
    missing or unreadable budget leaves the usage null, which blocks the next
    admission."""
    with _ledger_lock(record_path):
        ledger = read_ledger(record_path)
        for entry in ledger["slots"]:
            if entry["version"] == slot["version"] and entry["attempt"] == slot["attempt"]:
                break
        else:
            raise SystemExit(f"slot {slot['version']}-{slot['attempt']} is not in the ledger")
        try:
            budget = json.loads((Path(run_dir) / "budget.json").read_text(encoding="utf-8"))
        except (OSError, ValueError):
            budget = {}
        if not isinstance(budget, dict):
            budget = {}
        entry.update(finished_at=_now(), run_dir=str(run_dir),
                     reported_tokens=budget.get("reported_tokens"),
                     unknown_usage_attempts=budget.get("unknown_usage_attempts"))
        _write(ledger_path(record_path), ledger)
        return entry

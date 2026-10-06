"""The shared development record and its coordination tool (2026-10-01).

One machine-readable record of who is doing what against which exact
candidate (``docs/dev-record.json``), and one entry point for claiming work,
recording a delivery, recording a review and checking integration readiness.
The plan it implements was published on #35 (comment 5922888762) with
Davis's approval; ``docs/DEVELOPMENT_PROCESS.md`` carries the written rules.

The tool validates recorded facts. A reviewer still judges whether a
correction solves the problem; the acceptance manifest and the workflow
scorecard keep their own evidence, which this record references and never
duplicates. Nothing here calls a vendor.

    python3 tools/dev_record.py claim  --id T7 --purpose "..." --base <sha> --owns quadratus/x.py --author claude
    python3 tools/dev_record.py deliver --id T7 --sha <sha>
    python3 tools/dev_record.py unblock --id T7 --resolution "..." --by davis
    python3 tools/dev_record.py review --id T7 --reviewer codex --sha <sha> --verdict cleared --evidence <url>
    python3 tools/dev_record.py receipt --kind ci --sha <sha> --state passed --evidence <url>
    python3 tools/dev_record.py candidate --sha <sha> --reason "..." --by claude
    python3 tools/dev_record.py note --id T7 --text "..." --by claude
    python3 tools/dev_record.py extend --id T7 --owns docs/x.md --by claude
    python3 tools/dev_record.py ready
    python3 tools/dev_record.py render
"""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable, Dict, List, Optional

RECORD_PATH = Path(__file__).resolve().parent.parent / "docs" / "dev-record.json"

STATES = ("queued", "claimed", "delivered", "reviewed", "integrated", "blocked", "deferred")
ACTIVE = ("claimed", "delivered", "reviewed", "blocked")
RECEIPT_STATES = ("passed", "failed", "missing", "skipped", "unproven")
VERDICTS = ("cleared", "blocked", "stale")


class RecordError(ValueError):
    """A recorded fact the rules refuse."""


def _now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def load(path: Path = RECORD_PATH) -> dict:
    return json.loads(path.read_text(encoding="utf-8"))


def save(record: dict, path: Path = RECORD_PATH) -> None:
    path.write_text(json.dumps(record, indent=2) + "\n", encoding="utf-8")


def commit_exists_on_origin(sha: str, repo: Optional[Path] = None) -> bool:
    """Whether ``sha`` is reachable from any ``origin/*`` ref after a fetch.
    A delivery that exists only locally is not a delivery."""
    cwd = str(repo or Path(__file__).resolve().parent.parent)
    try:
        subprocess.run(["git", "fetch", "-q", "origin"], cwd=cwd, check=False, timeout=120,
                       capture_output=True)
        out = subprocess.run(["git", "branch", "-r", "--contains", sha], cwd=cwd, check=False,
                             capture_output=True, text=True, timeout=60)
    except (OSError, subprocess.TimeoutExpired):
        return False
    return out.returncode == 0 and any(line.strip().startswith("origin/") for line in out.stdout.splitlines())


def _task(record: dict, task_id: str) -> dict:
    for task in record["tasks"]:
        if task["id"] == task_id:
            return task
    raise RecordError(f"no task {task_id!r} in the record")


def _overlap(a: List[str], b: List[str]) -> List[str]:
    """Owned scopes overlap when one is the other or a path prefix of it."""
    def covers(x: str, y: str) -> bool:
        x, y = x.rstrip("/"), y.rstrip("/")
        return x == y or y.startswith(x + "/") or x.startswith(y + "/")
    return sorted({y for x in a for y in b if covers(x, y)})


def claim(record: dict, *, task_id: str, purpose: str, base: str, owns: List[str], author: str,
          reviewer: str = "", resolve: bool = False) -> dict:
    """Claim a unique scope against the current candidate. Refused when the
    scope overlaps another active claim or the base is not the candidate's
    exact SHA, unless the coordinator resolves it explicitly."""
    if any(t["id"] == task_id for t in record["tasks"]):
        raise RecordError(f"task {task_id!r} already exists")
    if not owns:
        raise RecordError("a claim names at least one owned file or subsystem")
    candidate = record["candidate"]["sha"]
    if base != candidate and not resolve:
        raise RecordError(f"stale base {base[:7]}: the candidate is {candidate[:7]}; "
                          "rebase or have the coordinator resolve it with --resolve")
    for other in record["tasks"]:
        if other["state"] in ACTIVE:
            hits = _overlap(owns, other.get("owns", []))
            if hits and not resolve:
                raise RecordError(f"scope overlaps active task {other['id']} on {', '.join(hits)}; "
                                  "the coordinator resolves overlaps with --resolve")
    task = dict(id=task_id, purpose=purpose, base=base, owns=list(owns), author=author,
                reviewer=reviewer, state="claimed", claimed_at=_now(), acknowledged=False,
                delivery=None, reviews=[], blockers=[], decisions=[])
    record["tasks"].append(task)
    return task


def acknowledge(record: dict, *, task_id: str) -> dict:
    """The assignee has started: assignment alone is not a start."""
    task = _task(record, task_id)
    task["acknowledged"] = True
    task["acknowledged_at"] = _now()
    return task


def _same_commit(a: str, b: str) -> bool:
    """One commit named two ways: an abbreviation (at least seven hex digits)
    matches the full SHA it begins. Exact-string comparison marked a review
    stale with the reason "reviewed 7590b13 but the delivery is 7590b13"
    (2026-10-06)."""
    a, b = a.strip().lower(), b.strip().lower()
    if len(a) < 7 or len(b) < 7:
        return a == b
    return a.startswith(b) or b.startswith(a)


def deliver(record: dict, *, task_id: str, sha: str, exists: Callable[[str], bool] = commit_exists_on_origin) -> dict:
    """Record a delivery. The commit must exist on origin. A new delivery
    marks every review of an earlier SHA stale; their evidence stays."""
    task = _task(record, task_id)
    if not exists(sha):
        raise RecordError(f"delivery {sha[:7]} is not on origin; push it first")
    previous = task.get("delivery")
    task["delivery"] = dict(sha=sha, at=_now())
    task["state"] = "delivered"
    if previous and not _same_commit(previous["sha"], sha):
        for review in task["reviews"]:
            if not _same_commit(review["sha"], sha) and review["verdict"] != "stale":
                review["verdict"] = "stale"
                review["stale_reason"] = f"delivery moved from {review['sha'][:7]} to {sha[:7]}"
    return task


def review(record: dict, *, task_id: str, reviewer: str, sha: str, verdict: str, evidence: str,
           scope: Optional[List[str]] = None, blocker: Optional[dict] = None) -> dict:
    """Bind a review to the exact reviewed commit and scope. Self-review is
    not an independent receipt. A review of a SHA other than the current
    delivery is recorded stale on arrival."""
    task = _task(record, task_id)
    if verdict not in VERDICTS:
        raise RecordError(f"verdict must be one of {VERDICTS}")
    if reviewer == task["author"]:
        raise RecordError(f"{reviewer} authored {task_id}; self-review is not an independent receipt")
    if not evidence:
        raise RecordError("a review names its evidence (a comment URL, a report path)")
    delivery = task.get("delivery")
    entry = dict(reviewer=reviewer, sha=sha, verdict=verdict, evidence=evidence,
                 scope=list(scope or task["owns"]), at=_now())
    if delivery is None or not _same_commit(delivery["sha"], sha):
        entry["verdict"] = "stale"
        entry["stale_reason"] = (f"reviewed {sha[:7]} but the delivery is "
                                 f"{delivery['sha'][:7] if delivery else 'absent'}")
    if verdict == "blocked":
        if not blocker or not all(blocker.get(k) for k in ("requirement", "failure", "evidence", "classification")):
            raise RecordError("a blocker names the violated requirement, the observable failure, its "
                              "evidence and a classification (reachable, invariant or future)")
        task["blockers"].append(dict(blocker, by=reviewer, sha=sha, at=_now()))
        task["state"] = "blocked"
    elif entry["verdict"] == "cleared":
        task["state"] = "reviewed"
    task["reviews"].append(entry)
    return entry


def unblock(record: dict, *, task_id: str, resolution: str, by: str) -> dict:
    """A blocker was resolved: the resolution is recorded against every open
    blocker and the task returns to claimed (or delivered, if it has a
    delivery). The blockers stay on the record as history."""
    task = _task(record, task_id)
    if task["state"] != "blocked":
        raise RecordError(f"{task_id} is {task['state']}, not blocked")
    if not resolution:
        raise RecordError("an unblock names its resolution")
    for blocker in task["blockers"]:
        if not blocker.get("resolved"):
            blocker["resolved"] = dict(resolution=resolution, by=by, at=_now())
    task["state"] = "delivered" if task.get("delivery") else "claimed"
    task["decisions"].append(f"unblocked by {by}: {resolution}")
    return task


def integrate(record: dict, *, task_id: str, sha: str, candidate: str) -> dict:
    """Mark a task integrated into the candidate; the candidate SHA moves."""
    task = _task(record, task_id)
    if task["state"] != "reviewed":
        raise RecordError(f"{task_id} is {task['state']}, not reviewed; integration needs a cleared "
                          "independent review of the delivered SHA")
    task["state"] = "integrated"
    task["integrated"] = dict(sha=sha, candidate=candidate, at=_now())
    record["candidate"]["sha"] = candidate
    return task


def move_candidate(record: dict, *, sha: str, reason: str, by: str) -> dict:
    """Move the candidate SHA outside ``integrate``: a merge that happened
    without a cleared review, a rebase, a correction. The move is refused
    without a reason, and the previous SHA stays in the candidate's history
    so a move nobody reviewed is visible rather than silent. Receipts are
    bound to exact SHAs, so a moved candidate starts with none."""
    if not reason:
        raise RecordError("a candidate move names its reason")
    previous = record["candidate"]["sha"]
    if _same_commit(previous, sha):
        raise RecordError(f"the candidate is already {sha[:7]}")
    record["candidate"].setdefault("history", []).append(
        dict(previous=previous, sha=sha, reason=reason, by=by, at=_now()))
    record["candidate"]["sha"] = sha
    return record["candidate"]


def extend(record: dict, *, task_id: str, owns: List[str], by: str, resolve: bool = False) -> dict:
    """Widen a task's owned scope after the fact (a delivery touched a file
    the claim did not name). The same overlap rule as a claim applies, and
    the extension is recorded as a decision rather than rewritten into the
    original claim."""
    task = _task(record, task_id)
    added = [o for o in owns if o not in task["owns"]]
    if not added:
        raise RecordError(f"{task_id} already owns {', '.join(owns)}")
    for other in record["tasks"]:
        if other["id"] != task_id and other["state"] in ACTIVE:
            hits = _overlap(added, other.get("owns", []))
            if hits and not resolve:
                raise RecordError(f"scope overlaps active task {other['id']} on {', '.join(hits)}; "
                                  "the coordinator resolves overlaps with --resolve")
    task["owns"].extend(added)
    task["decisions"].append(f"{by}: scope extended to {', '.join(added)}")
    return task


def note(record: dict, *, task_id: str, text: str, by: str) -> dict:
    """Append a dated decision to a task without changing its state."""
    task = _task(record, task_id)
    if not text:
        raise RecordError("a note says something")
    task["decisions"].append(f"{by}: {text}")
    return task


def receipt(record: dict, *, kind: str, sha: str, state: str, evidence: str = "") -> dict:
    """A required receipt (ci, acceptance, review) for an exact SHA. Failed,
    missing, skipped and unproven are kept distinct, never collapsed."""
    if state not in RECEIPT_STATES:
        raise RecordError(f"receipt state must be one of {RECEIPT_STATES}")
    entry = dict(kind=kind, sha=sha, state=state, evidence=evidence, at=_now())
    record.setdefault("receipts", []).append(entry)
    return entry


def readiness(record: dict) -> dict:
    """Whether the candidate is ready to integrate: every required receipt
    recorded for the candidate's exact SHA and passed; every active task
    either integrated or not blocking. Preserves what is failed, missing,
    skipped or unproven, distinctly."""
    sha = record["candidate"]["sha"]
    required = record["candidate"].get("required_receipts", ["ci", "acceptance", "review"])
    latest: Dict[str, dict] = {}
    for r in record.get("receipts", []):
        if _same_commit(r["sha"], sha):
            latest[r["kind"]] = r
    receipts = {kind: (latest[kind]["state"] if kind in latest else "missing") for kind in required}
    blockers = [dict(task=t["id"], blockers=t["blockers"]) for t in record["tasks"] if t["state"] == "blocked"]
    open_tasks = [t["id"] for t in record["tasks"] if t["state"] in ("claimed", "delivered")]
    stale = [dict(task=t["id"], reviews=[r for r in t["reviews"] if r["verdict"] == "stale"])
             for t in record["tasks"] if any(r["verdict"] == "stale" for r in t["reviews"])]
    ready = all(state == "passed" for state in receipts.values()) and not blockers and not open_tasks
    return dict(candidate=sha, gate=record["candidate"].get("gate"), ready=ready, receipts=receipts,
                blockers=blockers, open_tasks=open_tasks, stale_reviews=stale)


def render(record: dict) -> str:
    """The concise view for the active PR body."""
    c = record["candidate"]
    lines = ["**Development record** (docs/dev-record.json)", "",
             f"Candidate: PR #{c['pr']} at `{c['sha']}`; gate: {c.get('gate', '')}", "",
             "| Task | Purpose | State | Author | Reviewer | Delivery | Review |", "|---|---|---|---|---|---|---|"]
    for t in record["tasks"]:
        delivery = t["delivery"]["sha"][:7] if t.get("delivery") else ""
        live = [r for r in t["reviews"] if r["verdict"] != "stale"]
        rev = f"{live[-1]['verdict']} @{live[-1]['sha'][:7]}" if live else ("stale" if t["reviews"] else "")
        lines.append(f"| {t['id']} | {t['purpose']} | {t['state']} | {t['author']} | {t.get('reviewer') or ''} "
                     f"| {delivery} | {rev} |")
    r = readiness(record)
    lines += ["", f"Readiness: {'ready' if r['ready'] else 'not ready'}; receipts "
              + ", ".join(f"{k}={v}" for k, v in r["receipts"].items())
              + (f"; blocked: {', '.join(b['task'] for b in r['blockers'])}" if r["blockers"] else "")
              + (f"; open: {', '.join(r['open_tasks'])}" if r["open_tasks"] else "")]
    return "\n".join(lines)


def main(argv: Optional[List[str]] = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--record", default=str(RECORD_PATH))
    sub = parser.add_subparsers(dest="command", required=True)

    def add(name, *flags, **options):
        p = sub.add_parser(name)
        for flag in flags:
            p.add_argument(flag, **options.get(flag, {}))
        return p

    add("claim", "--id", "--purpose", "--base", "--owns", "--author", "--reviewer", "--resolve",
        **{"--id": dict(required=True), "--purpose": dict(required=True), "--base": dict(required=True),
           "--owns": dict(nargs="+", required=True), "--author": dict(required=True),
           "--reviewer": dict(default=""),
           "--resolve": dict(action="store_true", help="coordinator override for an overlap or stale base")})
    add("ack", "--id", **{"--id": dict(required=True)})
    add("deliver", "--id", "--sha", **{"--id": dict(required=True), "--sha": dict(required=True)})
    add("review", "--id", "--reviewer", "--sha", "--verdict", "--evidence", "--scope",
        "--requirement", "--failure", "--blocker-evidence", "--classification",
        **{"--id": dict(required=True), "--reviewer": dict(required=True), "--sha": dict(required=True),
           "--verdict": dict(required=True, choices=VERDICTS), "--evidence": dict(required=True),
           "--scope": dict(nargs="*")})
    add("unblock", "--id", "--resolution", "--by",
        **{"--id": dict(required=True), "--resolution": dict(required=True), "--by": dict(required=True)})
    add("integrate", "--id", "--sha", "--candidate",
        **{"--id": dict(required=True), "--sha": dict(required=True), "--candidate": dict(required=True)})
    add("receipt", "--kind", "--sha", "--state", "--evidence",
        **{"--kind": dict(required=True), "--sha": dict(required=True),
           "--state": dict(required=True, choices=RECEIPT_STATES), "--evidence": dict(default="")})
    add("candidate", "--sha", "--reason", "--by",
        **{"--sha": dict(required=True), "--reason": dict(required=True), "--by": dict(required=True)})
    add("extend", "--id", "--owns", "--by", "--resolve",
        **{"--id": dict(required=True), "--owns": dict(nargs="+", required=True), "--by": dict(required=True),
           "--resolve": dict(action="store_true")})
    add("note", "--id", "--text", "--by",
        **{"--id": dict(required=True), "--text": dict(required=True), "--by": dict(required=True)})
    add("ready")
    add("render")
    args = parser.parse_args(argv)
    path = Path(args.record)
    record = load(path)
    try:
        if args.command == "claim":
            claim(record, task_id=args.id, purpose=args.purpose, base=args.base, owns=args.owns,
                  author=args.author, reviewer=args.reviewer, resolve=args.resolve)
        elif args.command == "ack":
            acknowledge(record, task_id=args.id)
        elif args.command == "deliver":
            deliver(record, task_id=args.id, sha=args.sha)
        elif args.command == "review":
            blocker = None
            if args.verdict == "blocked":
                blocker = dict(requirement=args.requirement, failure=args.failure,
                               evidence=args.blocker_evidence, classification=args.classification)
            review(record, task_id=args.id, reviewer=args.reviewer, sha=args.sha, verdict=args.verdict,
                   evidence=args.evidence, scope=args.scope, blocker=blocker)
        elif args.command == "unblock":
            unblock(record, task_id=args.id, resolution=args.resolution, by=args.by)
        elif args.command == "integrate":
            integrate(record, task_id=args.id, sha=args.sha, candidate=args.candidate)
        elif args.command == "receipt":
            receipt(record, kind=args.kind, sha=args.sha, state=args.state, evidence=args.evidence)
        elif args.command == "candidate":
            move_candidate(record, sha=args.sha, reason=args.reason, by=args.by)
        elif args.command == "extend":
            extend(record, task_id=args.id, owns=args.owns, by=args.by, resolve=args.resolve)
        elif args.command == "note":
            note(record, task_id=args.id, text=args.text, by=args.by)
        elif args.command == "ready":
            print(json.dumps(readiness(record), indent=2))
            return 0
        elif args.command == "render":
            print(render(record))
            return 0
    except RecordError as exc:
        print(f"refused: {exc}", file=sys.stderr)
        return 2
    save(record, path)
    print(render(record))
    return 0


if __name__ == "__main__":
    sys.exit(main())

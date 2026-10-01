"""Offline, immutable Jev experiment preparation. Never invokes a provider.

Run with the intended engine installed/importable, using --engine to bind its
source. The private Session methods are intentional: a policy-table simulation
would miss capability inference, kind pins and session load spreading.
"""
from __future__ import annotations

import argparse
import hashlib
import itertools
import json
import re
import subprocess
import sys
import tempfile
from dataclasses import asdict
from pathlib import Path, PurePosixPath


def canonical(value):
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode()


def digest(value):
    return hashlib.sha256(canonical(value)).hexdigest()


def git(root, *args):
    return subprocess.check_output(["git", "-C", str(root), *args], text=True).strip()


def safe_relative(value):
    p = PurePosixPath(value)
    if not value or p.is_absolute() or ".." in p.parts or "\\" in value:
        raise ValueError(f"not a repository-relative path: {value!r}")
    return p


def engine_identity(root):
    root = root.resolve()
    sha = git(root, "rev-parse", "HEAD")
    files = git(root, "ls-files", "quadratus", "pyproject.toml").splitlines()
    hashes = {}
    for name in files:
        path = root / name
        if path.is_symlink() or not path.is_file():
            raise ValueError(f"engine file unavailable: {name}")
        raw = path.read_bytes()
        committed = subprocess.check_output(["git", "-C", str(root), "show", f"{sha}:{name}"])
        if raw != committed:
            raise ValueError(f"uncommitted engine bytes: {name}")
        hashes[name] = hashlib.sha256(raw).hexdigest()
    if not hashes:
        raise ValueError("no engine files")
    return {"sha": sha, "files": hashes, "digest": digest(hashes)}


def bind_engine(root):
    root = root.resolve()
    sys.path.insert(0, str(root))
    import quadratus.session as session
    if Path(session.__file__).resolve() != root / "quadratus/session.py":
        raise ValueError("already imported a different engine; use a fresh process")
    return session


def validate(packet, engine):
    from quadratus.session import Complexity
    from quadratus.task_kinds import KNOWN_NEEDS, ROUTING
    if packet.get("version") != 1 or not re.fullmatch(r"[0-9a-f]{40}", packet.get("base_sha", "")):
        raise ValueError("version 1 and full base SHA required")
    ids = set()
    if not packet.get("tasks"):
        raise ValueError("empty sample")
    for task in packet["tasks"]:
        tid = task["id"]
        if not re.fullmatch(r"[A-Za-z0-9_-]+", tid) or tid in ids:
            raise ValueError("duplicate or unsafe task id")
        ids.add(tid)
        if task["split"] not in {"development", "held_out"}:
            raise ValueError(f"{tid}: invalid split")
        text = task["task_text"]
        if not text.strip() or re.search(r"(?im)^\s*(KIND|NEEDS|SCOPE|TIER|LEAD)\s*:", text):
            raise ValueError(f"{tid}: task text must not inject control metadata")
        for key, allowed in (("acceptable_kinds", set(ROUTING)),
                             ("acceptable_difficulties", set(Complexity._COLLABORATORS))):
            if not task[key] or set(task[key]) - allowed or len(set(task[key])) != len(task[key]):
                raise ValueError(f"{tid}: invalid {key}")
        accepted_pairs(task)
        if len(task_input(task)) > 8000:
            raise ValueError(f"{tid}: input exceeds the engine context slice")
        if set(task["required_capabilities"]) - KNOWN_NEEDS:
            raise ValueError(f"{tid}: unknown capabilities")
        scope = task["scope"]
        if type(scope["max_lines"]) is not int or not 0 < scope["max_lines"] <= 100:
            raise ValueError(f"{tid}: invalid scope ceiling")
        if not scope["acceptance"] or not scope["permitted_paths"] or not scope["intended_result"]:
            raise ValueError(f"{tid}: incomplete scope")
        for name in scope["permitted_paths"] + scope.get("forbidden_paths", []):
            safe_relative(name)
        if not task["source_refs"] or not task["checks"] or not task["rationale"].strip():
            raise ValueError(f"{tid}: missing references/checks/rationale")
        for ref in task["source_refs"]:
            safe_relative(ref["path"])
            # Verify the reference at the sample's immutable base, not HEAD.
            git(engine, "cat-file", "-e", packet["base_sha"] + ":" + ref["path"])
        for check in task["checks"]:
            if check["status"] not in {"existing", "proposed"} or not check["argv"]:
                raise ValueError(f"{tid}: invalid check")
            if any(not isinstance(a, str) or not a for a in check["argv"]):
                raise ValueError(f"{tid}: check must be an argv list")


def accepted_pairs(task):
    explicit = task.get("acceptable_pairs")
    if explicit is not None:
        pairs = [(p["kind"], p["difficulty"]) for p in explicit]
        allowed = set(itertools.product(task["acceptable_kinds"], task["acceptable_difficulties"]))
        if not pairs or len(set(pairs)) != len(pairs) or set(pairs) - allowed:
            raise ValueError("invalid explicit acceptable pairs")
        return pairs
    return list(itertools.product(task["acceptable_kinds"], task["acceptable_difficulties"]))


def task_input(task):
    # No acceptable answers, difficulty hints or rationale enter model context.
    return ("SCOPE: " + json.dumps(task["scope"], sort_keys=True) + "\n"
            + "NEEDS: " + json.dumps(task["required_capabilities"]) + "\n"
            + task["task_text"])


def route(task, engine, answers=None):
    session = bind_engine(engine)
    from quadratus.artifacts import ArtifactStore
    from quadratus.decisions import Verdict
    from quadratus.task_kinds import seat_satisfies
    requests = []

    class RecordedDecider:
        def decide(self, decision):
            requests.append(asdict(decision))
            return Verdict(decision=decision.id, answer=answers[decision.id], source="offline:fixed-answer")

    def no_model(*args, **kwargs):
        raise AssertionError("offline preparation attempted a vendor invocation")

    with tempfile.TemporaryDirectory(prefix="jev-offline-") as scratch:
        config = session.SessionConfig(project=Path(scratch), allow_writes=True,
                                       decider=RecordedDecider() if answers is not None else None)
        s = session.Session("Offline route observation", ArtifactStore(Path(scratch)/"artifacts"),
                            no_model, config=config, available=lambda _: True)
        s._explicit_tasks = [task_input(task)]
        s.explicit = {"listed": 1, "ran": [], "invalid": None}
        spec = s._next_explicit_task()
        lead = s._pick_lead(spec)
        return {"kind": spec.kind, "difficulty": spec.complexity, "lead": lead,
                "inferred_and_declared_needs": sorted(spec.needs),
                "capabilities_satisfied": seat_satisfies(lead, spec.needs),
                "work_class": spec.work_class, "requests": requests,
                "decisions": s.decisions, "rotation_after": s._rotation,
                "vendor_load_after": dict(s._leads_by_vendor)}


def prepare(packet, engine):
    bind_engine(engine)
    validate(packet, engine)
    identity = engine_identity(engine)
    rows = []
    for task in packet["tasks"]:
        default = route(task, engine)
        alternatives = []
        for kind, difficulty in accepted_pairs(task):
            observed = route(task, engine, {"task.kind": kind, "task.difficulty": difficulty})
            observed["lead_changed"] = observed["lead"] != default["lead"]
            alternatives.append(observed)
        rows.append({"id": task["id"], "split": task["split"], "input": task_input(task),
                     "input_digest": digest(task_input(task)), "default": default,
                     "acceptable_routes": alternatives,
                     "author_default_matches": task["expected_default_lead"] == default["lead"],
                     "proposed_checks": sum(c["status"] == "proposed" for c in task["checks"])})
    content = {"format": 1, "packet": packet, "packet_digest": digest(packet), "engine": identity,
               "python": sys.version, "routing_state": {"rotation": 0, "vendor_history": {},
               "availability": "all configured seats assumed available", "session": "fresh per arm/task",
               "tier": "normal; dispatch/admission and code execution not exercised"},
               "rows": rows, "provider_calls": 0, "authorization": "offline preparation only",
               "acceptance": "unqualified until independent executable graders and baseline checks pass"}
    return {"freeze_digest": digest(content), "content": content}


def verify_freeze(frozen):
    content = frozen["content"]
    if frozen["freeze_digest"] != digest(content) or content["packet_digest"] != digest(content["packet"]):
        raise ValueError("freeze content changed")
    return content


def score(frozen, observations, engine, repeats=3):
    """Score externally collected decisions. Does not launch or accept code work."""
    content = verify_freeze(frozen)
    if engine_identity(engine) != content["engine"]:
        raise ValueError("scoring engine changed")
    tasks = {t["id"]: t for t in content["packet"]["tasks"]}
    routes = {r["id"]: r for r in content["rows"]}
    seen, results = set(), []
    for obs in observations:
        key = (obs["task_id"], obs["repeat"])
        if key in seen or key[0] not in tasks or type(key[1]) is not int or not 0 <= key[1] < repeats:
            raise ValueError("duplicate/unknown task or invalid repeat")
        seen.add(key)
        if obs["freeze_digest"] != frozen["freeze_digest"]:
            raise ValueError("observation uses another freeze")
        task = tasks[key[0]]
        if obs["input_digest"] != routes[key[0]]["input_digest"]:
            raise ValueError("observation input differs")
        answers = obs["answers"]
        refused = bool(obs.get("refusal"))
        result = {"task_id": key[0], "repeat": key[1], "split": task["split"],
                  "refusal": obs.get("refusal"), "evidence": obs}
        if refused:
            result.update(acceptable=False, selected_lead=None, lead_changed=None)
        else:
            # Unknown labels cannot be treated as a successful route.
            from quadratus.task_kinds import DIFFICULTY_LADDER, ROUTING
            if answers.get("task.kind") not in ROUTING or answers.get("task.difficulty") not in DIFFICULTY_LADDER:
                raise ValueError("out-of-set answer without refusal evidence")
            actual = route(task, engine, answers)
            result.update(acceptable=(answers["task.kind"], answers["task.difficulty"]) in accepted_pairs(task),
                          selected_lead=actual["lead"],
                          lead_changed=actual["lead"] != routes[key[0]]["default"]["lead"])
        results.append(result)
    missing = sorted(set(itertools.product(tasks, range(repeats))) - seen)
    return {"freeze_digest": frozen["freeze_digest"], "rows": results, "missing": missing,
            "complete": not missing, "counts_by_split": {
                split: {"observed": sum(r["split"] == split for r in results),
                        "acceptable": sum(r["split"] == split and r["acceptable"] for r in results)}
                for split in ("development", "held_out")},
            "conclusion_boundary": "Label consistency and actual offline lead changes only; no task quality or savings inference"}


def write_new(path, value):
    with path.open("x") as f:
        json.dump(value, f, indent=2)
        f.write("\n")


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("command", choices=["prepare", "score"])
    p.add_argument("--engine", type=Path, required=True)
    p.add_argument("--packet", type=Path)
    p.add_argument("--freeze", type=Path)
    p.add_argument("--observations", type=Path)
    p.add_argument("--out", type=Path, required=True)
    args = p.parse_args()
    bind_engine(args.engine)
    if args.command == "prepare":
        result = prepare(json.loads(args.packet.read_text()), args.engine)
    else:
        result = score(json.loads(args.freeze.read_text()), json.loads(args.observations.read_text()), args.engine)
    write_new(args.out, result)


if __name__ == "__main__":
    main()

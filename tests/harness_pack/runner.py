"""Deterministic replay pack: controller determinism, not live reliability.

Each case under ``cases/`` replays captured or abridged vendor replies into
the session engine through a scripted ``invoke`` and asserts what the
controller did with them. No vendor CLI, API key, network or database is
touched. A pass here means the harness reacts to a given reply the same way
every time; it says nothing about how often a live model produces that
reply. Families validated only by this pack carry the label
"live reliability not measured" (report, Reconciliation table).

Case format (JSON)::

    {
      "id": "scope-overrun-stops", "shape": "scope-trap",
      "source": "which incident or report section this replays",
      "family": "pure-logic", "grader": false,
      "session": {"project": true, "git": false, "allow_writes": true,
                  "config": {...SessionConfig fields...}, "ask_operator": "answer",
                  "available_down": ["claude:fable"], "invariants": []},
      "files": {"app.py": "x = 1\\n"},
      "policy": {...a .quadratus/policy.json document...},
      "gate": [{"id": "tests", "code": "print('3 passed')", "cheap": false}],
      "drive": {"kind": "run_task", "task": {...TaskSpec fields, scope as dict...}},
      "script": [{"role": "lead", "reply": "...", "writes": {...}, "raise": {...},
                  "times": 1, "mark_down": "claude:fable"}],
      "expect": {...},
      "variants": {"good": {"script": [...], "expect": {...}}, "bad": {...}}
    }

A variant overrides ``script`` and merges its ``expect`` over the base. Every
case that involves a grader (a reply the controller judges: a review, a
recheck, a verdict) carries at least a ``good`` and a ``bad`` variant.
"""

from __future__ import annotations

import dataclasses
import json
import re
import subprocess
import sys
import traceback
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional

from quadratus.artifacts import ArtifactStore
from quadratus.delegation import invocation_context
from quadratus.integration import GateCommand, GateSuite
from quadratus.providers import PartialWorkSuspected, ProviderError, ProviderRefusal
from quadratus.scope import TaskScope
from quadratus.session import Session, SessionConfig, TaskSpec

LABEL = "controller determinism, not live reliability"
CASES_DIR = Path(__file__).parent / "cases"
GRADED_VARIANTS = ("good", "bad")


class ReplayGap(AssertionError):
    """The engine asked for a reply the case did not script."""


class ReplayWindowExhausted(RuntimeError):
    """Stands in for a vendor 429: recognised by attribute, as in runtime.py."""

    window_exhausted = True


_RAISES: Dict[str, Callable[[str], BaseException]] = {
    "ProviderError": ProviderError,
    "ProviderRefusal": lambda m: ProviderRefusal(m, model="fixture", category="cyber"),
    "PartialWorkSuspected": PartialWorkSuspected,
    "WindowExhausted": ReplayWindowExhausted,
    "TimeoutError": TimeoutError,
    "RuntimeError": RuntimeError,
}


# -- case loading ----------------------------------------------------------

def load_cases(directory: Path = CASES_DIR) -> List[dict]:
    cases = []
    for path in sorted(directory.glob("*.json")):
        with path.open(encoding="utf-8") as handle:
            case = json.load(handle)
        case.setdefault("_path", str(path))
        cases.append(case)
    return cases


def variants_of(case: dict) -> List[str]:
    return list(case.get("variants") or {"base": None})


def resolve_variant(case: dict, name: str) -> dict:
    """The case with one variant's script and expectations folded in."""
    resolved = dict(case)
    variant = (case.get("variants") or {}).get(name) or {}
    if "script" in variant:
        resolved["script"] = variant["script"]
    if "session" in variant:
        session = dict(case.get("session", {}))
        session.update(variant["session"])
        resolved["session"] = session
    expect = dict(case.get("expect", {}))
    expect.update(variant.get("expect", {}))
    resolved["expect"] = expect
    resolved["_variant"] = name
    return resolved


# -- the scripted model ----------------------------------------------------

class Replay:
    """Scripted replies, matched by role, model and prompt text, in order."""

    def __init__(self, script: List[dict], project: Optional[Path], down: set):
        self.entries = [dict(e, _fired=0) for e in script]
        self.project = project
        self.down = down
        self.calls: List[dict] = []

    def _role(self) -> str:
        context = invocation_context.get() or {}
        return str(context.get("role", "direct"))

    def _matches(self, entry: dict, role: str, model: str, prompt: str) -> bool:
        times = entry.get("times")
        if times is not None and entry["_fired"] >= times:
            return False
        want = entry.get("role")
        if want and not (role == want or role.startswith(want + ":")):
            return False
        if entry.get("model") and entry["model"] != model:
            return False
        if entry.get("contains") and entry["contains"] not in prompt:
            return False
        return True

    def __call__(self, model: str, prompt: str, system=None, allow_writes: bool = False) -> str:
        role = self._role()
        self.calls.append(dict(role=role, model=model, prompt=prompt, allow_writes=allow_writes))
        if model in self.down:
            raise ReplayWindowExhausted(f"{model}: window exhausted (replayed)")
        for entry in self.entries:
            if self._matches(entry, role, model, prompt):
                entry["_fired"] += 1
                return self._perform(entry, model, prompt)
        raise ReplayGap(f"no scripted reply for role={role!r} model={model!r}; "
                        f"prompt starts: {prompt[:120]!r}")

    def _perform(self, entry: dict, model: str, prompt: str) -> str:
        for rel, content in (entry.get("writes") or {}).items():
            if self.project is None:
                raise ReplayGap("a scripted write needs a project")
            target = self.project / rel
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(content, encoding="utf-8")
        if entry.get("mark_down"):
            self.down.add(entry["mark_down"])
        if entry.get("raise"):
            spec = entry["raise"]
            kind = spec["type"] if isinstance(spec, dict) else spec
            message = spec.get("message", kind) if isinstance(spec, dict) else kind
            raise _RAISES[kind](message)
        return _fill(entry.get("reply", ""), prompt)


def _fill(reply: str, prompt: str) -> str:
    """Substitute the few prompt-derived values a captured reply must echo."""
    if "{snapshot_hash}" in reply:
        match = re.search(r"Snapshot hash: ([0-9a-f]{64})", prompt)
        reply = reply.replace("{snapshot_hash}", match.group(1) if match else "missing")
    if "{criterion}" in reply:
        match = re.search(r"Acceptance criteria: (\[.*\])", prompt)
        criteria = json.loads(match.group(1)) if match else ["missing"]
        reply = reply.replace("{criterion}", json.dumps(criteria[0])[1:-1])
    return reply


# -- building the session ----------------------------------------------------

def _gate(spec: List[dict], root: Path) -> GateSuite:
    commands = []
    for item in spec:
        argv = (sys.executable, "-c", item["code"]) if "code" in item else tuple(item.get("argv", ()))
        extra = {k: v for k, v in item.items() if k not in ("id", "code", "argv")}
        commands.append(GateCommand(item["id"], argv, **extra))
    return GateSuite(commands, cwd=root)


def _policy(document: dict, root: Path):
    from quadratus.policy import load_library, load_policy
    origin, _, _ = load_library()
    document = json.loads(json.dumps(document))
    document.setdefault("library", {})["version"] = origin["version"]
    target = root / ".quadratus/policy.json"
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(json.dumps(document), encoding="utf-8")
    return load_policy(root)


def _task(spec: dict) -> TaskSpec:
    spec = dict(spec)
    scope = spec.pop("scope", None)
    if scope is not None:
        scope = TaskScope(**scope)
    return TaskSpec(scope=scope, **spec)


@dataclasses.dataclass
class Outcome:
    result: Any = None
    error: Optional[BaseException] = None
    session: Optional[Session] = None
    replay: Optional[Replay] = None
    notes: List[str] = dataclasses.field(default_factory=list)
    root: Optional[Path] = None

    @property
    def outcome(self) -> str:
        return type(self.error).__name__ if self.error is not None else "completed"


def execute(case: dict, tmp_path: Path) -> Outcome:
    settings = case.get("session", {})
    root: Optional[Path] = None
    if settings.get("project"):
        root = tmp_path / "project"
        root.mkdir()
        if settings.get("git"):
            subprocess.run(["git", "init", "-q"], cwd=root, check=True)
        for rel, content in (case.get("files") or {}).items():
            path = root / rel
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(content, encoding="utf-8")
    notes: List[str] = []
    config_fields = dict(settings.get("config", {}))
    config = SessionConfig(project=root, allow_writes=bool(settings.get("allow_writes")),
                           progress=notes.append, **config_fields)
    if "ask_operator" in settings:
        answer = settings["ask_operator"]
        config.ask_operator = lambda question: answer
    if case.get("gate") is not None:
        config.integration_gate = _gate(case["gate"], root)
    if case.get("policy") is not None:
        config.repository_policy = _policy(case["policy"], root)
    down = set(settings.get("available_down", ()))
    replay = Replay(case.get("script", []), root, down)
    store = ArtifactStore(tmp_path / "artifacts")
    session = Session(settings.get("goal", "Fixture goal"), store, replay, config=config,
                      invariants=settings.get("invariants"),
                      available=lambda key: key not in down)
    outcome = Outcome(session=session, replay=replay, notes=notes, root=root)
    drive = case["drive"]
    try:
        if drive["kind"] == "run_task":
            outcome.result = session.run_task(_task(drive["task"]))
        elif drive["kind"] == "next_task":
            outcome.result = session.next_task()
        elif drive["kind"] == "run":
            outcome.result = session.run(max_tasks=drive.get("max_tasks", 3))
        else:
            raise ValueError(f"unknown drive kind {drive['kind']!r}")
    except Exception as exc:  # noqa: BLE001 -- the outcome is the assertion subject
        outcome.error = exc
    return outcome


# -- checking expectations ---------------------------------------------------

def _count(calls: List[dict], selector: str) -> int:
    kind, _, value = selector.partition(":")
    if kind == "role":
        return sum(1 for c in calls if c["role"] == value or c["role"].startswith(value + ":"))
    if kind == "model":
        return sum(1 for c in calls if c["model"] == value)
    if selector == "total":
        return len(calls)
    raise ValueError(f"unknown call selector {selector!r}")


def _bounds(actual: int, bound: Any, label: str, failures: List[str]) -> None:
    if isinstance(bound, int):
        bound = {"eq": bound}
    if "eq" in bound and actual != bound["eq"]:
        failures.append(f"{label}: expected {bound['eq']}, got {actual}")
    if "min" in bound and actual < bound["min"]:
        failures.append(f"{label}: expected at least {bound['min']}, got {actual}")
    if "max" in bound and actual > bound["max"]:
        failures.append(f"{label}: expected at most {bound['max']}, got {actual}")


def _store_texts(session: Session) -> List[str]:
    return [session.store.get(i) for i in session.store.ids()]


def _store_kinds(session: Session) -> List[str]:
    return [ref.kind for i in session.store.ids() for ref in session.store.refs(i)]


def check(expect: dict, out: Outcome) -> List[str]:
    failures: List[str] = []
    session, replay = out.session, out.replay
    if "outcome" in expect and out.outcome != expect["outcome"]:
        detail = f": {out.error}" if out.error else ""
        failures.append(f"outcome: expected {expect['outcome']}, got {out.outcome}{detail}")
    if "error_contains" in expect:
        text = str(out.error or "")
        for needle in _list(expect["error_contains"]):
            if needle not in text:
                failures.append(f"error text lacks {needle!r}: {text[:200]!r}")
    if "history" in expect:
        _bounds(len(session.history), expect["history"], "history", failures)
    if "calls" in expect:
        for selector, bound in expect["calls"].items():
            _bounds(_count(replay.calls, selector), bound, f"calls[{selector}]", failures)
    if "open_findings" in expect:
        spec = expect["open_findings"]
        if spec.get("empty") and session.open_findings:
            failures.append(f"open_findings expected empty: {session.open_findings}")
        if "count" in spec:
            _bounds(len(session.open_findings), spec["count"], "open_findings", failures)
        joined = "\n".join(session.open_findings)
        for needle in _list(spec.get("contains", [])):
            if needle not in joined:
                failures.append(f"open_findings lack {needle!r}: {joined[:300]!r}")
    if "checks" in expect:
        spec = expect["checks"]
        if "count" in spec:
            _bounds(len(session.checks), spec["count"], "checks", failures)
        if session.checks:
            last = session.checks[-1]
            if "last_passed" in spec and last["passed"] != spec["last_passed"]:
                failures.append(f"checks[-1].passed: expected {spec['last_passed']}, got {last['passed']}")
            for needle in _list(spec.get("output_contains", [])):
                if needle not in last["output"]:
                    failures.append(f"checks[-1].output lacks {needle!r}")
            if "last_receipt" in spec:
                receipt = last["receipts"][0] if last["receipts"] else {}
                for key, value in spec["last_receipt"].items():
                    if receipt.get(key) != value:
                        failures.append(f"receipt.{key}: expected {value!r}, got {receipt.get(key)!r}")
        elif spec.get("count", 0) != 0 or "last_passed" in spec:
            failures.append("no checks were recorded")
    if "notes_contain" in expect:
        joined = "\n".join(out.notes)
        for needle in _list(expect["notes_contain"]):
            if needle not in joined:
                failures.append(f"progress notes lack {needle!r}: {joined[:300]!r}")
    if "prompts" in expect:
        for spec in expect["prompts"]:
            hits = [c for c in replay.calls
                    if (not spec.get("role") or c["role"] == spec["role"]
                        or c["role"].startswith(spec["role"] + ":"))
                    and spec["contains"] in c["prompt"]]
            _bounds(len(hits), spec.get("count", {"min": 1}), f"prompts containing {spec['contains']!r}", failures)
    if "files" in expect:
        for rel, spec in expect["files"].items():
            path = out.root / rel
            if spec.get("exists") is not None and path.exists() != spec["exists"]:
                failures.append(f"file {rel}: exists={path.exists()}, expected {spec['exists']}")
            if path.exists():
                text = path.read_text(encoding="utf-8")
                for needle in _list(spec.get("contains", [])):
                    if needle not in text:
                        failures.append(f"file {rel} lacks {needle!r}")
                if "equals" in spec and text != spec["equals"]:
                    failures.append(f"file {rel} differs from the expected content")
    if "in_flight" in expect:
        spec = expect["in_flight"]
        changed = set(session.in_flight.get("changed", []))
        for rel in _list(spec.get("changed_contains", [])):
            if rel not in changed:
                failures.append(f"in_flight.changed lacks {rel!r}: {sorted(changed)}")
        if spec.get("inspected") is not None and session.in_flight.get("inspected") != spec["inspected"]:
            failures.append(f"in_flight.inspected: {session.in_flight.get('inspected')}")
    if "store_contains" in expect:
        texts = "\n".join(_store_texts(session))
        for needle in _list(expect["store_contains"]):
            if needle not in texts:
                failures.append(f"artifact store lacks {needle!r}")
    if "store_kinds_contain" in expect:
        kinds = _store_kinds(session)
        for kind in _list(expect["store_kinds_contain"]):
            if kind not in kinds:
                failures.append(f"artifact kinds lack {kind!r}: {sorted(set(kinds))}")
    if "rulings_contain" in expect:
        joined = "\n".join(session.memory.ledger.rulings)
        for needle in _list(expect["rulings_contain"]):
            if needle not in joined:
                failures.append(f"rulings lack {needle!r}: {joined!r}")
    if "policy_plans" in expect:
        spec = expect["policy_plans"]
        plans = session.policy_plans
        if "count" in spec:
            _bounds(len(plans), spec["count"], "policy_plans", failures)
        if plans:
            plan = plans[-1]
            if "primary_family" in spec and plan["primary_family"] != spec["primary_family"]:
                failures.append(f"primary_family: {plan['primary_family']}")
            if spec.get("has_hash") and not re.fullmatch(r"[0-9a-f]{64}", plan.get("hash", "")):
                failures.append("plan hash missing")
            if spec.get("blocked") is not None and bool(plan["blocked"]) != spec["blocked"]:
                failures.append(f"plan blocked: {plan['blocked']}")
    if "scope_reports" in expect:
        spec = expect["scope_reports"]
        reports = session.scope_reports
        if "count" in spec:
            _bounds(len(reports), spec["count"], "scope_reports", failures)
        if reports:
            last = reports[-1]
            for key in ("blocking", "oversized"):
                if key in spec and bool(getattr(last, key)) != spec[key]:
                    failures.append(f"scope report {key}: {getattr(last, key)}")
    if "seat" in expect and session.seat().key != expect["seat"]:
        failures.append(f"seat: expected {expect['seat']}, got {session.seat().key}")
    if "task" in expect:
        spec = expect["task"]
        task = out.result
        if task is None:
            failures.append("next_task returned None")
        else:
            for key, value in spec.items():
                if key == "description_startswith":
                    if not task.description.startswith(value):
                        failures.append(f"task.description: {task.description[:80]!r}")
                elif getattr(task, key) != value:
                    failures.append(f"task.{key}: expected {value!r}, got {getattr(task, key)!r}")
    return failures


def _list(value) -> List[str]:
    return [value] if isinstance(value, str) else list(value)


# -- the pack -----------------------------------------------------------------

@dataclasses.dataclass
class CaseResult:
    case_id: str
    variant: str
    shape: str
    passed: bool
    failures: List[str]


def run_case(case: dict, variant: str, tmp_path: Path) -> CaseResult:
    resolved = resolve_variant(case, variant)
    try:
        out = execute(resolved, tmp_path)
        failures = check(resolved["expect"], out)
    except Exception:  # noqa: BLE001 -- a broken replay is a failed case, reported
        failures = ["runner error: " + traceback.format_exc(limit=3)]
    return CaseResult(case["id"], variant, case.get("shape", ""), not failures, failures)


def pack_problems(cases: List[dict]) -> List[str]:
    """Structural rules every pack must satisfy before any case runs."""
    problems = []
    ids = [c["id"] for c in cases]
    if len(set(ids)) != len(ids):
        problems.append("duplicate case ids")
    for case in cases:
        for key in ("id", "shape", "source", "drive", "expect"):
            if key not in case:
                problems.append(f"{case.get('id', case.get('_path'))}: missing {key}")
        if case.get("grader"):
            missing = [v for v in GRADED_VARIANTS if v not in (case.get("variants") or {})]
            if missing:
                problems.append(f"{case['id']}: graded case lacks variants {missing}")
    return problems


def run_pack(cases: List[dict], scratch: Path) -> List[CaseResult]:
    import tempfile
    results = []
    for case in cases:
        for variant in variants_of(case):
            tmp = Path(tempfile.mkdtemp(prefix=f"{case['id']}-{variant}-", dir=scratch))
            results.append(run_case(case, variant, tmp))
    return results


def render(results: List[CaseResult], problems: List[str]) -> str:
    lines = [f"Harness replay pack: {LABEL}.",
             "Replays of captured replies test that the controller behaves the same way",
             "every time. They say nothing about how often a live model produces those",
             "replies; families validated only here carry 'live reliability not measured'.", ""]
    for problem in problems:
        lines.append(f"PACK ERROR  {problem}")
    for result in results:
        mark = "pass" if result.passed else "FAIL"
        lines.append(f"{mark:4}  {result.case_id:40} {result.variant:12} {result.shape}")
        for failure in result.failures:
            lines.append(f"      - {failure}")
    passed = sum(r.passed for r in results)
    lines.append("")
    lines.append(f"{passed} of {len(results)} case variants passed across {len({r.case_id for r in results})} cases.")
    lines.append(f"Label: {LABEL}.")
    return "\n".join(lines)


def main(argv: Optional[List[str]] = None) -> int:
    import tempfile
    cases = load_cases()
    problems = pack_problems(cases)
    with tempfile.TemporaryDirectory(prefix="harness-pack-") as scratch:
        results = run_pack(cases, Path(scratch))
    print(render(results, problems))
    return 0 if not problems and all(r.passed for r in results) else 1

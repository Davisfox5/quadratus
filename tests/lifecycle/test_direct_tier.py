"""The direct execution tier (first slice, 2026-09-30; Davis via Codex on #41).

Opt-in. The orchestrator labels a task TIER: direct; a deterministic
admission on the contract decides; an admitted task runs with no collaborator
review (and so no revision round) and no model close-out, and everything
else stays: scope measurement, the check and its fix round, the harness
capture and the design review where required, every stop. The favicon task
from diagnostic run 20260930T020711Z is replayed on both tiers with the same
scripted deliveries, and the record's per-role call counts are what a saving
is read from. Negative controls prove the admission refuses what the rules
name and records why.
"""

import json

import pytest

from tests.lifecycle import harness as H
from tests.lifecycle.test_lifecycle_matrix import FILES, FIXED, Script

pytestmark = pytest.mark.requirements_ledger

REQS = "REQUIREMENTS:\nR1: GET /favicon.ico returns 200 with an SVG icon\n"
FAVICON = dict(
    permitted_paths=["app.py", "static/favicon.svg", "templates/index.html", "tests/test_basic.py"],
    intended_result="GET /favicon.ico returns 200 with a project-owned SVG icon",
    acceptance=["static/favicon.svg exists", "tests pass"], max_lines=40,
    capture={"path": "/", "steps": []})


def _decl(scope=FAVICON, tier="direct", extra=""):
    return ("KIND: frontend simple\nSCOPE: " + json.dumps(scope) + "\nAdd a favicon route and link.\nCOVERS: R1"
            + (f"\nTIER: {tier}" if tier else "") + extra)


def _files():
    return {**FILES, "app.py": FIXED, "templates/index.html": "<html><head></head></html>\n",
            "static/favicon.svg": ""}


def _lead(call, replay):
    H.write(call, {"static/favicon.svg": "<svg/>\n",
                   "templates/index.html": '<html><head><link rel="icon" href="/favicon.ico"></head></html>\n'})
    H.evidence(replay.project if False else __import__("pathlib").Path(call.cwd), call.task, age=H.FRESH)
    return 'Added the favicon.\nCHANGED: ["static/favicon.svg", "templates/index.html"]'


def _run(tmp_path, monkeypatch, decl, *, direct=True, lead=_lead, roles=None, **kw):
    plan = [REQS + decl]

    def orchestrator(call, replay):
        return plan.pop(0) if plan else "DONE"
    overrides = dict(orchestrator=orchestrator, lead=lead,
                     **{"requirements-review": lambda c, r: "COMPLETE",
                        "auditor": lambda c, r: "R1: MET - static/favicon.svg",
                        "design-review": lambda c, r: "APPROVED",
                        "collaborator": lambda c, r: "BLOCKING: the link tag has no type attribute"},
                     **(roles or {}))
    replay = H.run(tmp_path, monkeypatch, Script(**overrides), files=_files(), max_tasks=3,
                   direct_tier=direct, **kw)
    return replay


def _task(replay, tid="t1"):
    return next(t for t in replay.workflow["tasks"] if t["task_id"] == tid)


def _roles(replay, tid="t1"):
    return {role: row["calls"] for role, row in replay.workflow["calls_by_task"].get(tid, {}).items()}


def test_the_favicon_task_on_the_direct_tier_drops_review_and_closeout_only(tmp_path, monkeypatch):
    replay = _run(tmp_path, monkeypatch, _decl())
    t1 = _task(replay)
    assert t1["contract"]["required"]["tier"] == "direct" and t1["contract"]["required"]["tier_refused"] == ""
    assert t1["contract"]["required"]["design_collaboration_applicable"] is False
    roles = _roles(replay)
    assert roles.get("collaborator", 0) == 0 and roles.get("revision", 0) == 0 and roles.get("closeout", 0) == 0
    assert roles["lead"] == 1 and roles["design-review"] == 1, "the evidence review stays"
    assert "review" not in t1["stages"] and "closeout" in t1["stages"]
    assert t1["evidence"]["verified"] is True and t1["checks"][-1]["passed"] is True
    assert t1["mismatches"] == [], "the contract and the live reading agree"
    assert replay.result.completed, replay.result.error
    assert replay.artifacts("direct-closeout")
    summary = replay.of("orchestrator")[1].prompt
    assert "Direct tier, recorded by the harness" in summary and "check passed" in summary


def test_the_same_task_on_the_normal_tier_keeps_the_full_call_graph(tmp_path, monkeypatch):
    replay = _run(tmp_path, monkeypatch, _decl(tier=""))
    t1 = _task(replay)
    assert t1["contract"]["required"]["tier"] == "normal"
    roles = _roles(replay)
    assert roles["collaborator"] >= 1 and roles["revision"] == 1 and roles["closeout"] == 1
    assert roles["design-review"] == 1 and "review" in t1["stages"]
    assert replay.result.completed, replay.result.error


def test_the_orchestrator_is_told_the_tier_rules_only_when_enabled(tmp_path, monkeypatch):
    (tmp_path / "on").mkdir()
    (tmp_path / "off").mkdir()
    on = _run(tmp_path / "on", monkeypatch, _decl())
    assert "TIER: direct" in on.of("orchestrator")[0].prompt and "max_lines is at most 40" in on.of("orchestrator")[0].prompt
    off = _run(tmp_path / "off", monkeypatch, _decl(tier=""), direct=False)
    assert "TIER: direct" not in off.of("orchestrator")[0].prompt


@pytest.mark.parametrize("case,scope,extra,reason", [
    ("wildcard", dict(FAVICON, permitted_paths=["static/*"]), "", "no exact file paths"),
    ("too-big", dict(FAVICON, max_lines=41), "", "exceeds the direct bound 40"),
    ("dependency", dict(FAVICON, permitted_paths=FAVICON["permitted_paths"] + ["node_modules/x/index.js"]), "",
     "inside a dependency tree"),
    ("audit", dict(FAVICON, max_lines=1, edits="none"), "", "an audit (edits none)"),
])
def test_admission_refuses_and_records_why(tmp_path, monkeypatch, case, scope, extra, reason):
    replay = _run(tmp_path, monkeypatch, _decl(scope=scope, extra=extra), record_complete=False)
    t1 = _task(replay)
    assert t1["contract"]["required"]["tier"] == "normal"
    assert reason in t1["contract"]["required"]["tier_refused"], t1["contract"]["required"]["tier_refused"]


def test_without_the_flag_a_label_is_refused_and_recorded(tmp_path, monkeypatch):
    replay = _run(tmp_path, monkeypatch, _decl(), direct=False)
    t1 = _task(replay)
    assert t1["contract"]["required"]["tier"] == "normal"
    assert t1["contract"]["required"]["tier_refused"] == "the direct tier is not enabled on this run"
    assert _roles(replay)["closeout"] == 1


def _unit_session(tmp_path, **config):
    from types import SimpleNamespace

    from quadratus.artifacts import ArtifactStore
    from quadratus.integration import IntegrationGate
    from quadratus.session import Session, SessionConfig
    root = tmp_path / "unit"
    root.mkdir()
    config.setdefault("integration_gate", IntegrationGate(["true"], cwd=root))
    policy = SimpleNamespace(document={"capability_policy": {
        "deny_write": [".git/**", ".env"], "sensitive": [{"paths": ["config/secrets.py"]}]}})
    return Session("goal", ArtifactStore(tmp_path / "artifacts"), lambda *a, **k: "",
                   config=SessionConfig(project=root, allow_writes=True, direct_tier=True,
                                        repository_policy=policy, **config))


def _spec(**scope):
    from quadratus.scope import TaskScope
    from quadratus.session import TaskSpec
    from quadratus.task_kinds import TaskKind
    fields = dict(FAVICON)
    fields.pop("capture", None)
    fields.update(scope)
    return TaskSpec("t1", "Add a favicon.", kind=TaskKind.FRONTEND, tier="direct", scope=TaskScope(**fields))


def test_admission_refuses_a_sensitive_path_and_a_run_without_a_check(tmp_path):
    session = _unit_session(tmp_path)
    assert session._direct_refusal(_spec()) == ""
    assert "policy-denied or sensitive path" in session._direct_refusal(
        _spec(permitted_paths=FAVICON["permitted_paths"] + ["config/secrets.py"]))
    assert "policy-denied or sensitive path" in session._direct_refusal(
        _spec(permitted_paths=FAVICON["permitted_paths"] + [".git/hooks/pre-commit"]))
    session.config.integration_gate = None
    assert session._direct_refusal(_spec()) == "the run has no check to run"
    session.config.direct_tier = False
    assert session._direct_refusal(_spec()) == "the direct tier is not enabled on this run"


def test_a_direct_task_that_overruns_its_scope_still_fails_the_task(tmp_path, monkeypatch):
    def lead(call, replay):
        H.write(call, {"static/favicon.svg": "<svg/>\n", "README.md": "# rewritten\n"})
        return 'Did both.\nCHANGED: ["README.md", "static/favicon.svg"]'
    replay = _run(tmp_path, monkeypatch, _decl(), lead=lead, record_complete=False)
    t1 = _task(replay)
    assert t1["contract"]["required"]["tier"] == "direct" and t1["closed_as"] == "failed"
    assert _roles(replay).get("closeout", 0) == 0


def test_a_direct_task_whose_check_fails_still_gets_its_fix_round(tmp_path, monkeypatch):
    def lead(call, replay):
        H.write(call, {"app.py": "def add(a, b):\n    return 0\n", "static/favicon.svg": "<svg/>\n"})
        __import__("pathlib")
        H.evidence(__import__("pathlib").Path(call.cwd), call.task, age=H.FRESH)
        return 'Broke add.\nCHANGED: ["app.py", "static/favicon.svg"]'

    def gate_fix(call, replay):
        H.write(call, {"app.py": FIXED})
        return 'Fixed add.\nCHANGED: ["app.py"]'
    replay = _run(tmp_path, monkeypatch, _decl(), lead=lead, roles={"gate-fix": gate_fix})
    roles = _roles(replay)
    assert roles["gate-fix"] == 1 and roles.get("collaborator", 0) == 0
    assert H.gate_results(replay)[-1] == "PASSED"


def test_a_refused_direct_lead_is_still_the_refusal_stop(tmp_path, monkeypatch):
    replay = _run(tmp_path, monkeypatch, _decl(), lead=lambda c, r: H.claude_refusal("cyber"),
                  record_complete=False)
    assert replay.result.error.startswith("Provider") and not replay.result.completed

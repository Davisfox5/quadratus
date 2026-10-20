"""Seats that judge a source get the harness's own run of the task's checks
on exactly that source, and a fix that changes nothing is not re-checked.

r08 (20261011T203457Z-eaf86c90): reviewers' source copies carry no
dependency tree, so their runs of the project's browser tests failed with
MODULE_NOT_FOUND while the same tests passed in the project; revisions
re-ran all 209 tests, re-checks repeated the verdicts, and the run hit its
token reserve with no harness check, capture or audit. Its t1 had stopped at
its turn cap, and that source never met a check either.

Whole-controller replays: the gate is real pytest in the project, the vendor
launch is faked, scripted replies prove routing, not judgement. An observed
run is evidence for the seats only: it is never a task check, never a
handoff, never a MET.
"""

import pytest

from tests.lifecycle import harness as H
from tests.lifecycle.test_dependency_identity import _run
from tests.lifecycle.test_lifecycle_matrix import FILES, FIXED, Script, judging

BROKEN = "def add(a, b):\n    return a - b\n"
HANDED = "THE HARNESS HAS ALREADY RUN THE PROJECT'S CHECK COMMANDS ON EXACTLY THIS SOURCE"


def _text(call):
    """What the seat received: the role text travels in argv or the prompt."""
    return call.prompt + " " + " ".join(call.argv)


def _writes(content, reply='Wrote add.\nCHANGED: ["app.py"]'):
    def edit(call, replay):
        H.write(call, {"app.py": content})
        return reply
    return edit


def _observed(replay):
    return H.result_json(replay)["review_checks"]


def test_reviewers_get_the_harness_run_of_the_source_they_judge(tmp_path, monkeypatch):
    replay = _run(tmp_path, monkeypatch, Script(lead=_writes(FIXED)), files=FILES)
    reviews = replay.of("collaborator")
    assert reviews, "the task drew reviewers"
    for call in reviews:
        text = _text(call)
        assert HANDED in text and "APPROVED CHECK COMMANDS" not in text
        assert "before review" in text and "1 passed, 0 failed, 0 skipped" in text
        assert "quadratus-report" not in text, "no command line reaches a seat"
    [seen] = _observed(replay)
    assert seen["stage"] == "review" and seen["passed"] is True and seen["source"]


def test_a_failing_draft_is_shown_failing_and_the_task_gate_still_decides(tmp_path, monkeypatch):
    replay = _run(tmp_path, monkeypatch, Script(lead=_writes(BROKEN), revision=_writes(FIXED)), files=FILES)
    for call in replay.of("collaborator"):
        assert "- check: FAILED; 0 passed, 1 failed, 0 skipped" in _text(call)
    assert H.gate_results(replay) == ["PASSED"], "the task's own check ran on the revision and decided"
    [seen] = _observed(replay)
    assert seen["stage"] == "review" and seen["passed"] is False, "a failure on the source is evidence, kept"
    assert all(c["passed"] for c in H.result_json(replay)["checks"])


def test_a_recheck_sees_the_run_of_the_revised_source(tmp_path, monkeypatch):
    script = Script(lead=_writes(BROKEN), revision=_writes(FIXED),
                    collaborator=lambda c, r: "BLOCKING: add subtracts\nREVIEW: COMPLETE",
                    recheck=lambda c, r: "UNRESOLVED: still not convinced")
    edits = iter([_writes(FIXED), lambda c, r: "Nothing more to change.\nCHANGED: []"])
    script.overrides["revision"] = lambda c, r: next(edits)(c, r)
    replay = _run(tmp_path, monkeypatch, script, files=FILES)
    rechecks = replay.of("recheck")
    assert rechecks and all(HANDED in _text(c) and "before recheck" in _text(c) for c in rechecks)
    # One observation per source: the unchanged fix round reuses the run of
    # the revised source rather than running the gate again.
    assert [s["stage"] for s in _observed(replay)] == ["review", "recheck"]
    assert _observed(replay)[1]["passed"] is True, "the recheck saw the run of the revised source"
    assert not replay.result.completed, "the findings stay open"


def test_a_capped_lead_leaves_a_harness_run_the_orchestrator_sees_but_cannot_hand_off(tmp_path, monkeypatch):
    def capped(call, replay):
        if call.task == "t1":
            H.write(call, {"app.py": FIXED})
            return H.claude_cap()
        return "Nothing to change.\nCHANGED: []"
    replay = _run(tmp_path, monkeypatch, Script(lead=capped), files=FILES, max_tasks=2)
    second = replay.of("orchestrator")[1].prompt
    assert "harness run of the task's checks on this exact source before turn-limited" in second
    assert "evidence only, not the task's check" in second
    assert "is not dispatched while these show" not in second, "an observation never offers a handoff"
    [seen, *_] = _observed(replay)
    assert seen["stage"] == "turn-limited" and seen["task"] == "t1" and seen["passed"] is True


@pytest.mark.parametrize("stage", ["review"])
def test_an_observation_is_never_a_task_check(tmp_path, monkeypatch, stage):
    replay = _run(tmp_path, monkeypatch, Script(lead=_writes(FIXED)), files=FILES)
    record = H.result_json(replay)
    assert len(record["checks"]) == len(H.gate_results(replay)) == 1, "only the task gate is a check"
    assert record["review_checks"][0]["stage"] == stage


def test_an_observation_is_bound_to_its_gate_as_well_as_its_source(tmp_path):
    # A cheap subset or another task's gate on the same source is not this
    # run: the identity changes with any command or its settings.
    import sys

    from quadratus.integration import GateCommand, GateSuite, IntegrationGate
    from quadratus.session import _gate_identity
    full = GateSuite([GateCommand(id="py", argv=(sys.executable, "-m", "pytest")),
                      GateCommand(id="node", argv=("node", "--test"))], cwd=tmp_path)
    cheap = GateSuite([GateCommand(id="py", argv=(sys.executable, "-m", "pytest"))], cwd=tmp_path)
    slower = GateSuite([GateCommand(id="py", argv=(sys.executable, "-m", "pytest"), timeout=60),
                        GateCommand(id="node", argv=("node", "--test"))], cwd=tmp_path)
    again = GateSuite([GateCommand(id="py", argv=(sys.executable, "-m", "pytest")),
                       GateCommand(id="node", argv=("node", "--test"))], cwd=tmp_path)
    assert len({_gate_identity(g) for g in (full, cheap, slower)}) == 3
    assert _gate_identity(full) == _gate_identity(again)
    one = IntegrationGate([sys.executable, "-m", "pytest"], cwd=tmp_path)
    two = IntegrationGate([sys.executable, "-m", "pytest", "-x"], cwd=tmp_path)
    assert _gate_identity(one) != _gate_identity(two)
    # Root's three probes of 82615bd: each setting changes the identity.
    (tmp_path / "sub").mkdir()
    counted = IntegrationGate([sys.executable, "-m", "pytest"], cwd=tmp_path, minimum_tests=5)
    elsewhere = IntegrationGate([sys.executable, "-m", "pytest"], cwd=tmp_path / "sub")
    moved = GateSuite(list(full.commands), cwd=tmp_path / "sub")
    assert _gate_identity(counted) != _gate_identity(one)
    assert _gate_identity(elsewhere) != _gate_identity(one)
    assert _gate_identity(moved) != _gate_identity(full)


# -- the gate in force, not only the source ---------------------------------------------

def _two_command_gate(project):
    import sys

    from quadratus.integration import GateCommand, GateSuite
    return GateSuite([GateCommand(id="tests", argv=(sys.executable, "-m", "pytest", "-q")),
                      GateCommand(id="extra", argv=(sys.executable, "-c", "print('1 passed')"))], cwd=project)


def test_a_check_of_another_gate_on_the_same_source_is_not_a_seat_receipt(tmp_path, monkeypatch):
    # Root and SAME review of 82615bd: a later task's policy gate adds a
    # command; t1's passing check of the old gate on the same bytes is
    # history, so t2's reviewers get a run of t2's own gate.
    from quadratus.session import Session, _gate_identity
    original = Session._prepare_dispatch

    def dispatch(self, spec):
        original(self, spec)
        if spec.task_id == "t2":
            self.config.integration_gate = _two_command_gate(self.project)

    monkeypatch.setattr(Session, "_prepare_dispatch", dispatch)

    def lead(call, replay):
        if call.task == "t1":
            H.write(call, {"app.py": FIXED})
            return 'Wrote add.\nCHANGED: ["app.py"]'
        return "Nothing to change.\nCHANGED: []"
    import json

    from tests.lifecycle.test_lifecycle_matrix import DECL_T1, T2
    reread = "KIND: architect complex\nSCOPE: " + json.dumps(T2) + "\nRe-read the README against app.py."
    plan = [DECL_T1, reread]
    replay = _run(tmp_path, monkeypatch, Script(lead=lead, orchestrator=lambda c, r: plan.pop(0) if plan else "DONE"),
                  files=FILES, max_tasks=2)
    second = [c for c in replay.of("collaborator") if c.task == "t2"]
    assert second, "t2 drew reviewers"
    for call in second:
        text = _text(call)
        assert HANDED in text and "extra (required): passed" in text, "the run of t2's own gate"
    seen = [s for s in _observed(replay) if s["task"] == "t2"]
    assert seen and seen[0]["gate"] == _gate_identity(_two_command_gate(replay.project))


@pytest.mark.requirements_ledger
@pytest.mark.parametrize("gate_changes", [False, True, "forbidden"], ids=["same-gate", "other-gate", "forbidden"])
def test_a_verification_handoff_needs_a_check_of_the_gate_the_task_would_run(tmp_path, monkeypatch, gate_changes):
    import json

    from quadratus.session import Session
    from tests.lifecycle.test_lifecycle_matrix import T2
    reqs = "REQUIREMENTS:\nR1: add returns the sum\nR2: the README documents add\n"
    build = ("KIND: architect complex\nSCOPE: " + json.dumps(dict(
        permitted_paths=["app.py"], intended_result="add works", acceptance=["add(1, 2) == 3"], max_lines=40,
        proofs=H.proofs_for("R1", "app.py"))) + "\nImplement add in app.py.\nCOVERS: R1")
    verify = ("KIND: docs simple\nSCOPE: " + json.dumps(dict(T2, max_lines=1, edits="none"))
              + "\nCheck the README against app.py.\nCOVERS: R2")
    forbid = ("README.md",) if gate_changes == "forbidden" else ()
    plan = [reqs + build, verify]
    original = Session.next_task

    def next_task(self):
        if gate_changes is True and len(self.history) == 1:
            # The gate the next task would run changes: the policy derives
            # each task's gate from the operator's, as dispatch does.
            self._original_gate = self.config.integration_gate = _two_command_gate(self.project)
        return original(self)

    monkeypatch.setattr(Session, "next_task", next_task)
    script = Script(lead=_writes(FIXED), orchestrator=lambda c, r: plan.pop(0) if plan else "DONE",
                    auditor=judging(lambda c, r: "R1: MET - app.py\nR2: MET - README.md",
                                    reading="SUFFICIENT README.md names add(a, b) and its result, which is all R2 asks",
                                    legacy="SUFFICIENT README.md names add(a, b), checked against app.py"))
    replay = _run(tmp_path, monkeypatch, script, files=FILES, max_tasks=4, forbid=forbid)
    requirements = H.result_json(replay)["requirements"]
    if gate_changes == "forbidden":
        # SAME review of f5c04b5: the policy blocks the task's path, so it is
        # dispatched to meet the same refusal any task would, never covered.
        assert requirements["verification_handoffs"] == []
        assert requirements["verification_dispatched"][0]["why"] == "the repository policy refuses this task"
        assert "PolicyError" in replay.result.error and not replay.result.completed
        assert requirements["status"].get("R2", "open") == "open"
    elif gate_changes:
        assert requirements["verification_handoffs"] == []
        assert requirements["verification_dispatched"][0]["why"] == \
            "the latest project check ran a different gate than this task's"
    else:
        assert [h["task"] for h in requirements["verification_handoffs"]] == ["t2"], "the positive control"


@pytest.mark.parametrize("refusal", ["blocked", "runner", "limits"])
def test_a_plan_dispatch_would_refuse_predicts_no_gate(tmp_path, refusal):
    # SAME review of f5c04b5: every refusal _prepare_dispatch makes (a
    # blocked plan, a required runner that is not configured, paths beyond
    # the operator's limits) is no predicted gate, so no handoff.
    from quadratus.artifacts import ArtifactStore
    from quadratus.policy import PolicyError
    from quadratus.scope import TaskScope
    from quadratus.session import Session, SessionConfig, TaskSpec

    class Policy:
        explicit = True

        def resolve(self, paths, writing=False):
            return dict(blocked=["needs an operator ruling: secrets/"] if refusal == "blocked" else [],
                        gates=[], primary_family="x", hash="h")

    (tmp_path / "project").mkdir()
    limit = TaskScope(permitted_paths=["app.py"]) if refusal == "limits" else None
    session = Session("goal", ArtifactStore(tmp_path / "a"), lambda *a, **k: "",
                      config=SessionConfig(project=tmp_path / "project", allow_writes=True, default_scope=limit))
    session.config.repository_policy = Policy()
    if refusal == "runner":
        import quadratus.policy as policy_module

        def refuse(*a, **k):
            raise PolicyError("Required runner is not configured: endpoint (http)")
        original, policy_module.task_gate = policy_module.task_gate, refuse
    try:
        spec = TaskSpec("t2", "Check README.", scope=TaskScope(permitted_paths=["README.md"]))
        assert session._gate_for(spec) is None
    finally:
        if refusal == "runner":
            policy_module.task_gate = original

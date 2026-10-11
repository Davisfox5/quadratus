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
from tests.lifecycle.test_lifecycle_matrix import FILES, FIXED, Script

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


def test_a_recheck_sees_the_revised_source_and_an_unchanged_fix_is_not_rechecked(tmp_path, monkeypatch):
    script = Script(lead=_writes(BROKEN), revision=_writes(FIXED),
                    collaborator=lambda c, r: "BLOCKING: add subtracts\nREVIEW: COMPLETE",
                    recheck=lambda c, r: "UNRESOLVED: still not convinced")
    edits = iter([_writes(FIXED), lambda c, r: "Nothing more to change.\nCHANGED: []"])
    script.overrides["revision"] = lambda c, r: next(edits)(c, r)
    replay = _run(tmp_path, monkeypatch, script, files=FILES)
    peers = len(replay.of("collaborator"))
    rechecks = replay.of("recheck")
    assert len(rechecks) == peers, "one re-check per reviewer; the unchanged fix bought none"
    assert all(HANDED in _text(c) and "before recheck" in _text(c) for c in rechecks)
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

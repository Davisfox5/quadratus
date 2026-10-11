"""A capture path keeps its #fragment, and the call that redeclares a
capture sees the goal that prescribed it.

r05 (20261011T183849Z-869a1896): the goal prescribed a capture of
/#q=Alpha (wait for an enabled Clear, click it, wait for it disabled). The
orchestrator declared "/" with the same steps, believing the harness would
apply the hash "if supported"; with no hash Clear is disabled at load, so
step 1 timed out. The one redeclaration was asked without the goal, spent
228,533 reported tokens and hit its round cap with no declaration.

Whole-controller replays with a real harness capture in Chromium; scripted
replies are routing proof, not judgement.
"""

import json

import pytest

from tests.lifecycle import harness as H
from tests.lifecycle.test_audit_findings import REPAIR_SCOPE
from tests.lifecycle.test_harness_capture import _decl, _profile, _run, browser

pytestmark = pytest.mark.requirements_ledger

GOAL = ("Disable Clear when the search field is empty. Capture /#q=Alpha: wait for an enabled Clear, "
        "click Clear, then wait for Clear disabled.")
REQS = ("REQUIREMENTS:\nR1: Clear is disabled exactly when the search field is empty\n"
        "R2: the capture loads /#q=Alpha and reaches the disabled Clear\n")
STEPS = [{"action": "wait", "selector": "#clear:enabled"}, {"action": "click", "selector": "#clear"},
         {"action": "wait", "selector": "#clear:disabled"}]
PRESCRIBED = {"path": "/#q=Alpha", "steps": STEPS}
DROPPED = {"path": "/", "steps": STEPS}
PAGE = ("<html><head><link rel=icon href='data:,'></head><body><div class=toolbar style='max-width:100%'>"
        "<input id=q aria-label=Search><button id=clear disabled>Clear</button></div><script>"
        "const q = document.getElementById('q'), c = document.getElementById('clear');"
        "if (location.hash.startsWith('#q=')) { q.value = decodeURIComponent(location.hash.slice(3)); "
        "c.disabled = false; }"
        "c.onclick = () => { q.value = ''; c.disabled = true; };"
        "</script></body></html>\n")


def _build(call, replay):
    H.write(call, {"templates/index.html": PAGE})
    return 'Clear follows the field.\nCHANGED: ["templates/index.html"]'


def _task(capture):
    return _decl("KIND: frontend standard", dict(REPAIR_SCOPE, capture=capture), "Disable Clear when empty.")


def _redeclare(capture):
    def reply(call, replay):
        return f"CAPTURE: {json.dumps(capture)}"
    return reply


def _summary(replay):
    return json.loads((replay.project / ".quadratus" / "design-evidence" / "t1" / "summary.json").read_text())


@browser
def test_a_dropped_fragment_is_redeclared_from_the_goal_and_captured_as_written(tmp_path, monkeypatch):
    profile, _ = _profile(tmp_path)
    replay = _run(tmp_path, monkeypatch, [REQS + _task(DROPPED)], {"t1": _build}, profile=profile,
                  goal=GOAL, roles={"capture-redeclare": _redeclare(PRESCRIBED)})
    [redeclare] = replay.of("capture-redeclare")
    assert f"## Goal (verbatim)\n\n{GOAL}" in redeclare.prompt
    assert "R2: the capture loads /#q=Alpha and reaches the disabled Clear" in redeclare.prompt
    assert "The harness captured /, exactly as declared with nothing added to it" in redeclare.prompt
    assert "It may end in a #fragment (for example /#q=Alpha), which the harness loads exactly" in redeclare.prompt
    assert "quadratus-report" not in redeclare.prompt
    record = H.result_json(replay)["design_checks"][-1]
    assert record["recapture"]["capture"] == PRESCRIBED and record["recapture"]["verified"] is True
    assert _summary(replay)["target"].endswith("/#q=Alpha")
    assert replay.result.completed, replay.result.error


@browser
def test_the_prescribed_fragment_declared_at_dispatch_needs_no_redeclaration(tmp_path, monkeypatch):
    profile, _ = _profile(tmp_path)
    replay = _run(tmp_path, monkeypatch, [REQS + _task(PRESCRIBED)], {"t1": _build}, profile=profile, goal=GOAL)
    first = replay.of("orchestrator")[0].prompt
    assert "It may end in a #fragment (for example /#q=Alpha)" in first
    assert "The harness adds nothing to the path" in first
    assert replay.of("capture-redeclare") == []
    assert _summary(replay)["target"].endswith("/#q=Alpha")
    assert replay.result.completed, replay.result.error


@browser
def test_a_redeclaration_that_drops_the_fragment_again_stays_unverified(tmp_path, monkeypatch):
    # The one redeclaration is still one: a second wrong path is not retried.
    profile, _ = _profile(tmp_path)
    other = {"path": "/index.html", "steps": STEPS}
    replay = _run(tmp_path, monkeypatch, [REQS + _task(DROPPED)], {"t1": _build}, profile=profile,
                  goal=GOAL, roles={"capture-redeclare": _redeclare(other)})
    assert len(replay.of("capture-redeclare")) == 1
    record = H.result_json(replay)["design_checks"][-1]
    assert record["verified"] is False and record["recapture"]["verified"] is False
    assert not replay.result.completed


@pytest.mark.parametrize("path", ["/#q=$(id)", "/#q=a b", "/#q=a;b", "/../#q=a", "//evil#q=a", "/#q=a`b`"])
def test_a_fragment_carries_no_shell_space_or_parent_segment(path):
    from quadratus.preview import validate_capture
    with pytest.raises(ValueError):
        validate_capture({"path": path, "steps": []})


def test_a_plain_fragment_path_is_accepted_unchanged():
    from quadratus.preview import validate_capture
    assert validate_capture(PRESCRIBED)["path"] == "/#q=Alpha"

"""J31: the design repair receives its lead contract in a whole Session run."""

import json
from pathlib import Path

import pytest

from tests.lifecycle import harness as H

FILES = {
    "app.py": "def add(a, b):\n    return a + b\n",
    "tests/test_app.py": "from app import add\n\ndef test_add():\n    assert add(1, 2) == 3\n",
    "templates/index.html": "<p>projects</p>\n",
    "static/style.css": "",
}
SCOPE = dict(permitted_paths=["templates/index.html", "static/style.css"],
             intended_result="an import button", acceptance=["the page shows an Import button"],
             max_lines=40)
DECLARATION = "KIND: frontend standard\nSCOPE: " + json.dumps(SCOPE) + "\nAdd an Import button."


@pytest.mark.parametrize("recaptured", [True, False])
def test_design_fix_gets_the_lead_packet_in_a_bounded_session_journey(
        tmp_path, monkeypatch, recaptured):
    def respond(call, replay):
        if call.role == "orchestrator":
            return DECLARATION
        if call.role == "lead":
            H.write(call, {"templates/index.html": "<button id=import>Import</button>\n"})
            H.evidence(Path(call.cwd), "t1", age=H.STALE)
            return 'Added the button.\nCHANGED: ["templates/index.html"]'
        if call.role == "revision":
            H.write(call, {"static/style.css": "#import { padding: 8px; }\n"})
            return 'Styled the button.\nCHANGED: ["static/style.css"]'
        if call.role == "design-fix":
            if recaptured:
                H.evidence(Path(call.cwd), "t1", age=H.FRESH)
            return "Attempted to capture current renders.\nCHANGED: []"
        if call.role == "design-review":
            return "APPROVED"
        if call.role == "closeout":
            return "SUMMARY: done\nDECISIONS: none recorded\nDEAD ENDS: none"
        if call.role == "recheck":
            return "RESOLVED"
        return "No blocking findings."

    replay = H.run(tmp_path, monkeypatch, respond, files=FILES)
    fixes = replay.of("design-fix")
    assert len(fixes) == 1
    fix = fixes[0]
    assert "Role: lead" in fix.prompt
    assert "Stop with an ASK" in fix.prompt
    assert "Role: reviewer" not in fix.prompt
    assert "The rendered evidence for this design task is missing" in fix.prompt
    assert SCOPE["permitted_paths"][0] in fix.prompt
    assert fix.vendor == replay.of("lead")[0].vendor
    assert fix.argv == replay.of("revision")[0].argv
    assert len(H.gate_results(replay)) == 2 and set(H.gate_results(replay)) == {"PASSED"}
    record = json.loads(replay.artifact_texts("design-evidence")[0])
    assert record["verified"] is recaptured
    assert (replay.project / "templates/index.html").read_text() == "<button id=import>Import</button>\n"
    assert (replay.project / "static/style.css").read_text() == "#import { padding: 8px; }\n"
    assert not replay.of("gate-fix")
    # Read the persisted result, not Replay.workflow or the evidence artifact:
    # J31's report claim is about what an operator can inspect after the run.
    result = H.result_json(replay)
    (task,) = result["workflow"]["tasks"]
    assert task["task_id"] == "t1"
    assert task["dispatch"]["owner"] == task["contract"]["owner"]
    assert task["invoked_owner"] == task["lead"]
    # The seat key names the API family (openai), while the CLI executable is
    # named codex; the model id is the stable identity shared by both records.
    assert task["invoked_owner"].partition(":")[2] == fix.model
    assert task["attempts"]["design_fix"] == 1
    assert "design" in task["stages"]
    assert result["design_checks"][-1]["verified"] is recaptured
    assert task["evidence"]["verified"] is recaptured
    assert result["completed"] is False
    if recaptured:
        assert H.ended_at_cap(replay, 1)
        assert len(replay.of("design-review")) == 1
        assert result["error"] == replay.result.error
        assert result["workflow"]["run"]["facts"][-1]["legacy"] == "GoalUnconfirmedAtCap"
    else:
        assert replay.result.error.startswith("DesignUnverified:")
        assert not replay.of("design-review")
        assert not replay.result.completed
        assert result["error"] == replay.result.error
        assert result["workflow"]["run"]["facts"][-1]["legacy"] == "DesignUnverified"

"""Role packet size is checked before the calls that would carry it.

Run 20261010T135407Z-f3d33e9d: the operator's 260 forbidden paths alone put
every role packet over the 12,000-byte cap, and the run learned it only when
building t1's lead packet, after the planning and requirements-review calls
had been spent. The saved list is replayed here with scripted replies: the
run must refuse with zero model calls, a normal configuration must still run,
and a task whose own scope overflows the packet must be refused at dispatch,
before its lead is called.
"""

import json
from pathlib import Path

import pytest

from tests.lifecycle import harness as H
from tests.lifecycle.test_direct_tier import _decl, _run

pytestmark = pytest.mark.requirements_ledger

SAVED_FORBID = json.loads((Path(__file__).parent / "fixtures" / "forbid-20261010T135407Z.json").read_text())


def _result(replay):
    return json.loads((Path(replay.result.run_dir) / "result.json").read_text())


def test_the_saved_oversized_forbidden_list_refuses_with_zero_model_calls(tmp_path, monkeypatch):
    assert len(SAVED_FORBID) == 260 and len(json.dumps(SAVED_FORBID).encode()) > 12_000
    replay = _run(tmp_path, monkeypatch, _decl(), forbid=SAVED_FORBID, expect_session=False)
    assert replay.calls == []
    assert not replay.result.completed
    assert "Required role packet exceeds 12000 bytes" in replay.result.error
    data = _result(replay)
    assert data["tasks"] == 0 and data["source_changed"] is False
    assert (Path(replay.result.run_dir) / "report.md").read_text().startswith("# Run incomplete")


def test_a_normal_forbidden_list_still_runs(tmp_path, monkeypatch):
    replay = _run(tmp_path, monkeypatch, _decl(), forbid=["README.md", "tests/test_app.py"])
    assert replay.result.completed, replay.result.error
    assert replay.of("lead")


def test_a_task_scope_that_overflows_its_packet_is_refused_before_the_lead_call(tmp_path, monkeypatch):
    scope = dict(permitted_paths=["app.py", "static/favicon.svg", "templates/index.html"],
                 intended_result="favicon", acceptance=["x" * 13_000], max_lines=40,
                 capture={"path": "/", "steps": []})
    replay = _run(tmp_path, monkeypatch, _decl(scope=scope))
    assert replay.of("orchestrator"), "the run itself was admitted"
    assert not replay.of("lead") and not replay.of("collaborator")
    assert not replay.result.completed
    assert "Required role packet exceeds 12000 bytes" in replay.result.error
    assert H.gate_results(replay) == []


def test_a_reviewer_packet_over_the_cap_is_refused_before_the_lead_call(tmp_path, monkeypatch):
    # Built-in families make the lead packet the largest by a few bytes; a
    # repository card with reviewer-only rules can reverse that. Every packet
    # the task will carry is built at dispatch, so the lead is not spent on a
    # task whose review could never be sent.
    from quadratus.policy import PolicyError, RepositoryPolicy
    build = RepositoryPolicy.role_packet

    def packet(self, scope, role, conventions=""):
        if role == "reviewer" and scope is not None and scope.acceptance:
            raise PolicyError("Required role packet exceeds 12000 bytes; narrow the task")
        return build(self, scope, role, conventions)
    monkeypatch.setattr(RepositoryPolicy, "role_packet", packet)
    replay = _run(tmp_path, monkeypatch, _decl())
    assert replay.of("orchestrator") and not replay.of("lead")
    assert "Required role packet exceeds 12000 bytes" in replay.result.error

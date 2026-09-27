"""A turn cap on the recovery redraft or the security draft is a capped task
(phase 3, map G1/G2), never a raw TurnLimitReached escaping the run.

G1 is driven through the whole controller (a grok cancel recovered once on
claude, which then caps). G2 is driven through a whole Session, since the
OpenAI CLI that normally does security work has no turn cap: the only real
route to a capped security draft is the chain's fallback worker."""

import pytest

from quadratus.artifacts import ArtifactStore
from quadratus.providers import TurnLimitReached
from quadratus.session import Session
from tests.lifecycle import harness as H
from tests.lifecycle.test_lifecycle_matrix import DECL_T2, FILES_OK, Script


def _settings():
    from quadratus.config import Settings
    return Settings(backend="cli", lead_max_turns=14)


def _task(replay, tid="t1"):
    return next(t for t in replay.workflow["tasks"] if t["task_id"] == tid)


def _recovered_then_capped(tmp_path, monkeypatch, writes):
    # The recovery lands on a claude seat, whose CLI has a turn cap (codex's
    # does not); the selection logic itself is not under test here.
    from quadratus import session as session_module
    monkeypatch.setattr(session_module, "escalate_from", lambda lead, **kw: "claude:opus")

    def lead(call, replay):
        if call.vendor == "grok":
            return H.grok_ok("I'll create it", stop="cancelled", num_turns=3)
        if writes:
            H.write(call, writes)
        return H.claude_cap("Documenting add; not finished.", num_turns=14)
    return H.run(tmp_path, monkeypatch, Script(orchestrator=lambda call, replay: DECL_T2, lead=lead),
                 files=FILES_OK, max_tasks=1, settings=_settings())


def test_g1_a_capped_redraft_is_a_capped_task_with_its_work_kept(tmp_path, monkeypatch):
    replay = _recovered_then_capped(tmp_path, monkeypatch, {"README.md": "# app\n\npartial\n"})
    t1 = _task(replay)
    assert t1["closed_as"] == "turn_limited", replay.result.error
    assert t1["partial"]["changed"] == ["README.md"]
    assert (replay.project / "README.md").read_text() == "# app\n\npartial\n", "the capped work is kept"
    assert [c.vendor for c in replay.of("lead")] == ["grok", "claude"], "one recovery, no retry after the cap"
    assert not replay.of("collaborator") and not replay.of("gate-fix") and not replay.of("closeout")
    change, = t1["owner_changes"]
    assert t1["contract"]["owner"] == t1["dispatch"]["owner"] == change["from"] and t1["lead"] == change["to"]
    assert [f["kind"] for f in t1["facts"] if f["terminal"] and not f["recovered"]] == ["cap"]
    assert H.result_json(replay)["turn_limited_tasks"] == ["t1"] and not replay.result.completed
    assert "TurnLimitReached" not in replay.result.error


def test_g1_a_capped_redraft_with_no_change_is_recorded_as_such(tmp_path, monkeypatch):
    replay = _recovered_then_capped(tmp_path, monkeypatch, None)
    t1 = _task(replay)
    assert t1["closed_as"] == "turn_limited" and t1["partial"]["changed"] == []
    assert (replay.project / "README.md").read_text() == FILES_OK["README.md"]


def test_g1_a_capped_redraft_outside_its_scope_still_stops_with_the_work_kept(tmp_path, monkeypatch):
    replay = _recovered_then_capped(tmp_path, monkeypatch, {"app.py": "def add(a, b):\n    return 1\n"})
    assert replay.result.error.startswith("PartialWorkStopped: Turn-limited edits exceed the declared scope")
    assert (replay.project / "app.py").read_text() == "def add(a, b):\n    return 1\n"
    assert _task(replay)["closed_as"] == "stopped:PartialWorkStopped"


def test_g2_a_capped_security_draft_is_a_capped_task_and_nothing_is_verified(tmp_path):
    calls = []

    def invoke(model, prompt, system=None):
        calls.append(prompt)
        if "Name the single next task" in prompt:
            return "KIND: security\naudit the auth flow" if len(calls) == 1 else "DONE"
        if "verifying security work" in prompt or "The task is finished" in prompt:
            pytest.fail("no verification or close-out call after a capped security draft")
        raise TurnLimitReached("stopped at 14 turns", partial_text="Reading the token check.", turns=14)
    session = Session("Audit auth", ArtifactStore(tmp_path / "a"), invoke)
    session.run(max_tasks=3)
    (outcome,) = session.task_outcomes
    assert outcome.closed_as == "turn_limited" and "verification" not in outcome.edges
    assert "verification" in outcome.unsatisfied() and not session.completed
    assert "TurnLimitReached" not in session.stop_reason
    assert session.turn_limited == ["t1"]

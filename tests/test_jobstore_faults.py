"""The durable job store under real process and file faults (blueprint
section 8, stage B).

Each fault case runs a child process that publishes generations through
``quadratus.jobstore.JobStore`` and dies (``os._exit``) at a named durable
step. The parent then reads the run directory as a restarting episode would:
only the committed manifest counts, an orphan record is not acceptance, a
corrupt current generation is refused rather than replaced by an older one,
and a budget whose publication failed before invocation leaves no ticket.
"""

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

from quadratus import checkpoint, generations, jobstore
from quadratus.run_budget import RunBudget, RunBudgetExceeded, RunLimits

CHILD = r"""
import json, os, sys
from quadratus.jobstore import JobStore
run_dir, fault_at, steps = sys.argv[1], sys.argv[2], int(sys.argv[3])
seen = []
def fault(stage):
    seen.append(stage)
    if stage == fault_at and seen.count(stage) == steps:
        sys.stdout.write(json.dumps(dict(died_at=stage, seen=seen)) + "\n"); sys.stdout.flush()
        os._exit(7)
store = JobStore(run_dir, fault=fault)
store.publish({"budget": {"reserved_attempts": 1, "generation": 1}})
store.publish({"budget": {"reserved_attempts": 2, "generation": 2,
                          "operations": {"started": [{"opid": "O-2", "status": "started"}], "settled": []}},
               "checkpoint": {"body": {"n": 2}}},
              budget_unsettled=["O-2"], reserved_unfinished=200000)
sys.stdout.write(json.dumps(dict(died_at=None, seen=seen)) + "\n")
"""


def _child(run_dir: Path, fault_at: str, steps: int = 1) -> dict:
    env = dict(os.environ, PYTHONPATH=str(Path(__file__).resolve().parents[1]))
    done = subprocess.run([sys.executable, "-c", CHILD, str(run_dir), fault_at, str(steps)],
                          capture_output=True, text=True, env=env, timeout=60)
    lines = [ln for ln in done.stdout.splitlines() if ln.startswith("{")]
    assert lines, done.stderr
    return dict(json.loads(lines[-1]), returncode=done.returncode)


def _records(run_dir: Path):
    return sorted(p.name for p in (run_dir / "records").glob("*.json")) if (run_dir / "records").exists() else []


def test_a_crash_before_the_first_manifest_leaves_no_generation_and_orphans_are_not_acceptance(tmp_path):
    out = _child(tmp_path, "manifest-replace")
    assert out["died_at"] == "manifest-replace" and out["returncode"] == 7
    assert jobstore.read_manifest(tmp_path) is None
    assert _records(tmp_path), "the record bytes were written durably before the manifest"
    assert jobstore.read_budget(tmp_path) == (None, "budget.json: FileNotFoundError")


@pytest.mark.parametrize("fault_at, steps, committed", [
    ("record-write", 2, 1),      # the second generation's first record write
    ("records-fsync", 2, 1),     # after its records, before their directory fsync
    ("manifest-replace", 2, 1),  # before the manifest replace
    ("manifest-fsync", 2, 2),    # after the replace, before the directory fsync: an uncertain commit
])
def test_a_crash_at_each_durable_step_leaves_exactly_one_committed_generation(tmp_path, fault_at, steps, committed):
    out = _child(tmp_path, fault_at, steps)
    assert out["died_at"] == fault_at
    manifest = jobstore.read_manifest(tmp_path)
    assert manifest is not None and manifest.number == committed
    budget, problem = jobstore.read_budget(tmp_path)
    assert problem is None and budget["generation"] == committed
    if committed == 1:
        assert "checkpoint" not in manifest.records and manifest.clean
        with pytest.raises(checkpoint.CheckpointRefused, match="names none"):
            checkpoint.load(tmp_path)
    else:
        # The uncertain commit is a commit: the manifest on disk is what a
        # restart reads, with its started operation and unfinished reserve.
        assert manifest.unsettled == ("O-2",) and manifest.reserved_unfinished == 200000
        assert jobstore.unsettled_operations(tmp_path) == ("O-2",)


def test_the_committed_record_bytes_are_never_rewritten_and_a_corrupt_generation_is_refused(tmp_path):
    out = _child(tmp_path, "none")
    assert out["died_at"] is None
    manifest = jobstore.read_manifest(tmp_path)
    assert manifest.number == 2
    first = tmp_path / generations.record_path("budget", manifest.records["budget"])
    before = first.read_bytes()
    store = jobstore.JobStore(tmp_path)
    store.publish({"budget": {"reserved_attempts": 3, "generation": 3}})
    assert first.read_bytes() == before, "an older generation's record keeps its bytes"
    assert len(_records(tmp_path)) == 4
    # Corrupt the current generation's budget record: refused, never the
    # older generation's bytes and never the export.
    current = jobstore.read_manifest(tmp_path)
    (tmp_path / generations.record_path("budget", current.records["budget"])).write_text("{}")
    budget, problem = jobstore.read_budget(tmp_path)
    assert budget is None and "does not hash" in problem
    with pytest.raises(jobstore.GenerationCorrupt):
        jobstore.read_record(tmp_path, "budget")
    allowed, why = generations.fallback_allowed(current, manifest)
    assert not allowed


def test_a_failed_publication_latches_the_store_and_stops_the_budget_before_any_ticket(tmp_path):
    store = jobstore.JobStore(tmp_path)
    store.publish({"budget": {"reserved_attempts": 0}})
    blocked = tmp_path / "records"
    # Make the records directory unwritable for the next publication.
    blocked.chmod(0o500)
    try:
        budget = RunBudget(RunLimits(), path=tmp_path / "budget.json", store=store)
        if os.geteuid() == 0:
            pytest.skip("root ignores directory permissions; the refusal below is proven by the next test")
        with pytest.raises(RunBudgetExceeded, match="budget_state_unavailable"):
            budget.reserve()
        assert store.refused and budget.snapshot()["reserved_attempts"] == 0
        with pytest.raises(jobstore.StoreUnavailable):
            store.publish({"budget": {"reserved_attempts": 9}})
    finally:
        blocked.chmod(0o700)


def test_a_store_that_raises_stops_the_budget_before_the_call_and_the_export_is_not_trusted(tmp_path):
    class Fails(jobstore.JobStore):
        def publish(self, *a, **k):
            raise OSError("disk gone")
    store = Fails(tmp_path)
    budget = RunBudget(RunLimits(), path=tmp_path / "budget.json", store=store)
    # The construction-time publication failed: latched, so the first
    # reserve stops the run before any call (as a failed first write always did).
    assert budget.snapshot()["stop_reason"] == "budget_state_unavailable"
    with pytest.raises(RunBudgetExceeded, match="budget_state_unavailable"):
        budget.reserve()
    assert budget.snapshot()["reserved_attempts"] == 0 and budget.snapshot()["in_flight"] == 0
    assert not (tmp_path / "budget.json").exists(), "no export is written when the authority failed"


def test_the_manifest_outranks_a_stale_export_and_an_unsettled_operation_refuses_resume(tmp_path):
    store = jobstore.JobStore(tmp_path)
    budget = RunBudget(RunLimits(max_calls=5), path=tmp_path / "budget.json", store=store)
    ticket, _ = budget.reserve()
    snap = budget.snapshot()
    assert snap["operations"]["started"][0]["opid"] == f"O-{ticket}" and snap["in_flight"] == 1
    # A stale export says nothing is in flight; the manifest says one call
    # started and never settled. The manifest decides.
    (tmp_path / "budget.json").write_text(json.dumps(dict(snap, in_flight=0, operations={"started": [], "settled": []})))
    record, problem = jobstore.read_budget(tmp_path)
    assert problem is None and record["in_flight"] == 1 and record["operations"]["started"]
    assert jobstore.unsettled_operations(tmp_path) == (f"O-{ticket}",)
    body = dict(version=checkpoint.VERSION, sealed=False, resumable=True, consumed=checkpoint.consumed(snap),
                identities={}, state={})
    with pytest.raises(checkpoint.CheckpointRefused, match="never settled"):
        checkpoint.validate(tmp_path, body, {})
    budget.finish(ticket, {"input_tokens": 1, "output_tokens": 1}, reply="done")
    settled = budget.snapshot()["operations"]["settled"]
    assert settled[0]["status"] == "settled" and settled[0]["usage_known"] and len(settled[0]["result_digest"]) == 64
    assert jobstore.unsettled_operations(tmp_path) == ()


def test_checkpoint_round_trips_through_the_store_and_seals_there(tmp_path):
    store = jobstore.JobStore(tmp_path)
    body = dict(version=checkpoint.VERSION, lineage={}, sealed=False, resumable=True, uncarried=[], consumed=None,
                identities={}, state={}, written="now")
    checkpoint.write(tmp_path, body, store=store, budget_snapshot={"reserved_attempts": 0})
    manifest = jobstore.read_manifest(tmp_path)
    assert set(manifest.records) == {"checkpoint", "budget"}
    assert checkpoint.load(tmp_path)["resumable"] is True
    # The export is a copy; altering it changes nothing a reader acts on.
    (tmp_path / checkpoint.NAME).write_text("{}")
    assert checkpoint.load(tmp_path)["resumable"] is True
    checkpoint.seal(tmp_path, "completed", store=store)
    assert checkpoint.load(tmp_path)["sealed"] is True and jobstore.read_manifest(tmp_path).number == 2
    with pytest.raises(checkpoint.CheckpointRefused, match="sealed"):
        checkpoint.validate(tmp_path, checkpoint.load(tmp_path), {})


def test_a_checkpoint_written_before_the_typed_records_still_restores(tmp_path):
    """Historical format: the carried state lacks the A+B records; they
    start empty on the restored session, nothing is invented."""
    from quadratus.artifacts import ArtifactStore
    from quadratus.session import Session, SessionConfig
    session = Session("goal", ArtifactStore(tmp_path / "a"), lambda *a, **k: "", config=SessionConfig())
    state = checkpoint.capture_state(session)
    for name in checkpoint._ADDED_LATER:
        state.pop(name)
    fresh = Session("goal", ArtifactStore(tmp_path / "b"), lambda *a, **k: "", config=SessionConfig())
    checkpoint.restore_state(fresh, state)
    assert fresh.review_findings == [] and fresh.evidence_obligations == [] and fresh.operations == []
    assert fresh.plan_admissions == [] and fresh._requirement_revision_record == {}


def test_a_route_forfeit_is_carried_through_the_checkpoint_and_absent_from_older_ones(tmp_path):
    """A refused route forfeits the slots after it; continued from a
    quiescent checkpoint, the cap's stop still names the forfeit rather
    than the cap (admission/2)."""
    from quadratus.artifacts import ArtifactStore
    from quadratus.session import Session, SessionConfig
    session = Session("goal", ArtifactStore(tmp_path / "a"), lambda *a, **k: "", config=SessionConfig())
    forfeit = dict(task="t2", detail="t2: cannot pay its selected route", slots_unused=2, slots_used=1)
    session._route_forfeit = dict(forfeit)
    state = checkpoint.capture_state(session)
    assert state["_route_forfeit"] == forfeit
    fresh = Session("goal", ArtifactStore(tmp_path / "b"), lambda *a, **k: "", config=SessionConfig())
    checkpoint.restore_state(fresh, state)
    assert fresh._route_forfeit == forfeit
    older = Session("goal", ArtifactStore(tmp_path / "c"), lambda *a, **k: "", config=SessionConfig())
    state.pop("_route_forfeit")
    checkpoint.restore_state(older, state)
    assert older._route_forfeit is None


def test_a_terminal_commitment_survives_the_checkpoint_and_dispatches_no_ordinary_work(tmp_path, monkeypatch):
    """The job's commitment to end after one task (admission/3) is carried;
    a resumed episode reads it and goes to the cap's decision without
    asking for, or running, another task. Older checkpoints carry none."""
    from quadratus.artifacts import ArtifactStore
    from quadratus.session import Session, SessionConfig
    session = Session("goal", ArtifactStore(tmp_path / "a"), lambda *a, **k: "", config=SessionConfig())
    record = dict(task="t2", reason="its ordinary route could not be paid", selected="4 call(s)",
                  slots_used=2, relinquished=1, max_tasks=3)
    session._terminal_route = dict(record)
    session._slots_used = 2
    state = checkpoint.capture_state(session)
    assert state["_terminal_route"] == record
    fresh = Session("goal", ArtifactStore(tmp_path / "b"), lambda *a, **k: "", config=SessionConfig())
    checkpoint.restore_state(fresh, state)
    assert fresh._terminal_route == record and fresh._terminal_committed()
    decided = []
    monkeypatch.setattr(fresh, "next_task", lambda *a, **k: (_ for _ in ()).throw(AssertionError("planned")))
    monkeypatch.setattr(fresh, "_completion_decision", lambda site, n: decided.append((site, n)) or None)
    monkeypatch.setattr(fresh, "_apply_completion", lambda decision: None)
    monkeypatch.setattr(fresh, "_recheck_resolved_findings", lambda: None)
    fresh.run(max_tasks=3)
    assert decided == [("cap", 3)] and fresh._slots_used == 2, "straight to the cap's decision, slots as spent"
    older = Session("goal", ArtifactStore(tmp_path / "c"), lambda *a, **k: "", config=SessionConfig())
    state.pop("_terminal_route")
    checkpoint.restore_state(older, state)
    assert older._terminal_route is None and not older._terminal_committed()

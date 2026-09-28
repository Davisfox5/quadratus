"""Approved evidence a later task replaced cannot complete the run (map E2;
Codex 5862294492).

A design task's reviewer approves exact bytes, recorded as the task's
``delivery``. A later ordinary task can rewrite those renders, which are
excluded from source and so from its CHANGED line, and the run used to
complete over evidence nobody approved. At DONE (and at the cap's goal
confirmation) the guard now compares each approved delivery with the
project; a difference is an active ``unverified`` fact on the approved task
and the run stops ``CompletionUnproven``. It is not integrity: the J9b
settlement boundary is unchanged, and nothing is recaptured or reviewed
again. Whole-controller replays; the rewrite is the later lead's own write.
"""

import hashlib
import json
from pathlib import Path

from quadratus.session import Session
from tests.lifecycle import harness as H
from tests.lifecycle.test_lifecycle_matrix import (
    DECL_DESIGN,
    DECL_T2,
    Script,
    _design_files,
    _design_script,
)

SHOT = ".quadratus/design-evidence/t1/desktop/page.png"


def _task(replay, task_id):
    return next(t for t in replay.workflow["tasks"] if t["task_id"] == task_id)


def _sha(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _run(tmp_path, monkeypatch, *, rewrite, max_tasks=3, record_complete=False):
    """t1 (design, approved), then t2 (docs) which may rewrite t1's screenshot."""
    monkeypatch.setattr(Session, "_pick_lead", lambda self, spec: "claude:opus")
    script = _design_script("Renders refreshed.\nCHANGED: []")

    def orchestrator(call, replay):
        return {1: DECL_DESIGN, 2: DECL_T2}.get(len(replay.of("orchestrator")), "DONE")

    def lead(call, replay):
        if call.task == "t1":
            return _design_script("unused").overrides["lead"](call, replay)
        if rewrite:
            H.evidence(Path(call.cwd), "t1", age=H.FRESH, png={"desktop": 1300})
        return Script()._lead(call, replay)
    script.overrides.update(orchestrator=orchestrator, lead=lead)
    return H.run(tmp_path, monkeypatch, script, files=_design_files(), max_tasks=max_tasks,
                 record_complete=record_complete)


def test_approved_renders_replaced_by_a_later_task_do_not_complete(tmp_path, monkeypatch):
    replay = _run(tmp_path, monkeypatch, rewrite=True)
    t1 = _task(replay, "t1")
    assert t1["edges"]["reviewer"] is True and _sha(replay.project / SHOT) != t1["delivery"]["files"][SHOT]
    assert not replay.result.completed, "completed over evidence the reviewer never approved"
    assert replay.result.error.startswith("CompletionUnproven: DONE was accepted"), replay.result.error
    assert SHOT in replay.result.error
    fact = [f for f in t1["facts"] if f["stage"] == "delivery"]
    assert [(f["kind"], f["terminal"], f["recovered"]) for f in fact] == [("unverified", True, False)]
    stop = replay.workflow["run"]["facts"][-1]
    assert (stop["kind"], stop["legacy"]) == ("unverified", "CompletionUnproven"), "not integrity"
    assert len(replay.of("design-review")) == 1 and not replay.of("design-fix")[1:], "nothing re-run"


def test_the_same_run_without_the_rewrite_completes(tmp_path, monkeypatch):
    replay = _run(tmp_path, monkeypatch, rewrite=False, record_complete=True)
    t1 = _task(replay, "t1")
    assert _sha(replay.project / SHOT) == t1["delivery"]["files"][SHOT]
    assert replay.result.completed, replay.result.error
    assert not [f for f in t1["facts"] if f["stage"] == "delivery"]


def test_a_replacement_at_the_cap_is_refused_the_same_way(tmp_path, monkeypatch):
    replay = _run(tmp_path, monkeypatch, rewrite=True, max_tasks=2)
    assert not replay.result.completed
    assert replay.result.error.startswith("CompletionUnproven: the cap's goal confirmation was accepted"), \
        replay.result.error
    assert SHOT in json.dumps(_task(replay, "t1")["facts"])

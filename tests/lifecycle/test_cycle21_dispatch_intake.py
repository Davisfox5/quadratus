"""Cycle21 lifecycle (PR73 6751283026, reconciled by root): what the dispatch
would refuse is refused at intake, before any charge.

Run 20261109T195618Z (cycle20 terminal): t7 named ``readme.md`` while the
file is ``README.md`` and was refused at merge after the whole task; t8's
role packet (the saved P1-P9 SCOPE, ``cycle20_t8_scope.json``) was refused
for size only in ``_prepare_dispatch``, after slot 8 was charged, so the run
stopped with no correction, no admission and no lead.

Now both are sent back at intake through the shared bounded correction
allowance, before the slot, recovery or listed-task marks, the admission or
any call; a packet refusal that still reaches dispatch before admission is
recorded not started and uncharged, and one after admission is not.
"""
import json
from pathlib import Path

import pytest

from quadratus import session as S
from quadratus.policy import PacketTooLarge
from tests.lifecycle import harness as H
from tests.lifecycle.test_lifecycle_matrix import FILES, FIXED, Script

pytestmark = pytest.mark.requirements_ledger

REQS = "REQUIREMENTS:\nR1: add(a, b) returns the sum of two numbers\nR2: README documents add\n"
SAVED = json.loads((Path(__file__).parent / "cycle20_t8_scope.json").read_text())
CLEAN = "NO FINDINGS\nR1: MET - app.py\nR2: MET - README.md\nREVIEW: COMPLETE"


def _oversized(path="app.py", covers="R1"):
    """The saved t8 scenarios (P1-P9, verbatim operation, state and expect),
    mapped onto this fixture's requirement and test file."""
    proofs = [dict(p, requirements=[covers], location="tests/test_app.py") for p in SAVED["proofs"]]
    for p in proofs:
        p.pop("evidence", None)
    return dict(permitted_paths=[path, "tests/test_app.py"], intended_result=SAVED["intended_result"],
                acceptance=SAVED["acceptance"], max_lines=135, proofs=proofs)


def _narrow(path="app.py", covers="R1"):
    return dict(permitted_paths=[path, "tests/test_app.py"], intended_result="add returns the sum",
                acceptance=["add(1, 2) == 3"], max_lines=20,
                proofs=H.proofs_for(covers, "tests/test_app.py", operation="add(1, 2)", state="nothing stored",
                                    expect="3 is returned"))


def _task(scope, covers="R1", text="Implement add in app.py.", kind="backend simple"):
    return f"KIND: {kind}\nSCOPE: {json.dumps(scope)}\n{text}\nCOVERS: {covers}"


def _readme(path="README.md"):
    scope = dict(permitted_paths=[path], intended_result="README documents add", acceptance=["README names add"],
                 max_lines=10, proofs=H.proofs_for("R2", path, evidence="reading"))
    return f"KIND: docs rote\nSCOPE: {json.dumps(scope)}\nDocument add in {path}.\nCOVERS: R2"


def _script(*plans):
    replies = iter(plans)

    def lead(call, replay):
        if "Document add" in call.prompt:
            H.write(call, {"README.md": FILES["README.md"] + "add(a, b) returns a + b.\n"})
            return 'Documented add.\nCHANGED: ["README.md"]'
        H.write(call, {"app.py": FIXED})
        return 'Implemented add.\nCHANGED: ["app.py"]'
    return Script(orchestrator=lambda c, r: next(replies, "DONE"), lead=lead, collaborator=lambda c, r: CLEAN,
                  auditor=lambda c, r: "R1: MET - app.py\nR2: MET - README.md",
                  **{"requirements-review": lambda c, r: "COMPLETE"})


def _ledger(replay):
    return H.result_json(replay).get("slot_ledger") or []


def _events(replay, event):
    return [e for e in _ledger(replay) if e.get("event") == event]


def test_the_saved_oversized_scope_is_sent_back_measured_and_the_narrowed_task_runs(tmp_path, monkeypatch):
    first = REQS.split("R2:")[0] + _task(_oversized())
    replay = H.run(tmp_path, monkeypatch, _script(first, _task(_narrow())), files=FILES, max_tasks=2,
                   record_complete=False)
    planner = [c.prompt for c in replay.of("orchestrator")]
    sent = next(p for p in planner if "role packet for this task would be" in p)
    assert "the limit is 12,000" in sent and "proof scenarios" in sent and "every covered requirement" in sent
    record = H.result_json(replay)
    (refusal,) = record["packet_refusals"]
    assert refusal["needed"] > refusal["limit"] == 12_000 and refusal["components"]["proofs"] > 7_000
    assert refusal["components"]["scope"] + refusal["components"]["fixed"] == refusal["needed"]
    leads = replay.of("lead")
    assert leads and "Implement add" in leads[0].prompt, "the narrowed task reached its lead"
    assert all(_oversized()["proofs"][0]["expect"] not in c.prompt for c in leads), "the refused scope never ran"
    assert len(record.get("task_admissions") or []) == len({c.task for c in leads})
    assert "PolicyError" not in (replay.result.error or "")


def test_a_packet_refused_on_every_correction_stops_bounded_with_nothing_charged(tmp_path, monkeypatch):
    first = REQS.split("R2:")[0] + _task(_oversized())
    replay = H.run(tmp_path, monkeypatch, _script(*([first] + [_task(_oversized())] * 6)), files=FILES,
                   max_tasks=2, record_complete=False)
    assert replay.of("lead") == [], "no lead, collaborator or worker call"
    assert "kept naming tasks the dispatch would refuse" in (replay.result.error or ""), replay.result.error
    record = H.result_json(replay)
    assert not (record.get("task_admissions") or []), "no admission"
    assert _events(replay, "charged") == [] and len(_events(replay, "sent_back")) >= 4
    assert len(record["packet_refusals"]) == S.SessionConfig().max_requirement_reopens + 1


def test_a_parallel_batch_with_an_oversized_block_is_sent_back_before_any_child(tmp_path, monkeypatch):
    batch = REQS + "PARALLEL\n" + _task(_oversized()) + "\n---\n" + _readme()
    fixed = "PARALLEL\n" + _task(_narrow()) + "\n---\n" + _readme()
    replay = H.run(tmp_path, monkeypatch, _script(batch, fixed), files=FILES, max_tasks=3, record_complete=False)
    planner = [c.prompt for c in replay.of("orchestrator")]
    assert any("--- BATCH SENT BACK ---" in p and "role packet for this task would be" in p for p in planner)
    roles = [(c.role.split(":")[0], c.task) for c in replay.calls]
    first_lead = next(i for i, (r, _) in enumerate(roles) if r == "lead")
    refused_at = next(i for i, c in enumerate(replay.calls) if c.role.startswith("orchestrator")
                      and "BATCH SENT BACK" in c.prompt)
    assert refused_at < first_lead, "no child ran before the send-back"
    assert len({t for r, t in roles if r == "lead"}) >= 2, "the corrected batch ran both children"


def test_a_case_only_path_is_sent_back_naming_the_existing_spelling(tmp_path, monkeypatch):
    first = REQS + _readme("readme.md")
    replay = H.run(tmp_path, monkeypatch, _script(first, _readme("README.md"), _task(_narrow())), files=FILES,
                   max_tasks=3, record_complete=False)
    planner = [c.prompt for c in replay.of("orchestrator")]
    assert any("`readme.md` differs only in letter case from the existing `README.md`" in p for p in planner)
    leads = replay.of("lead")
    assert leads and all("readme.md" not in json.dumps(c.prompt).split("You may change only these paths")[-1][:80]
                         for c in leads)
    assert "out of scope" not in (replay.result.error or "")


def test_the_dispatch_backstop_before_admission_is_not_started_and_uncharged(tmp_path, monkeypatch):
    """Intake switched off so the dispatch builder refuses: the stop and its
    message are unchanged; the slot, admission and marks say not started."""
    monkeypatch.setattr(S.Session, "_packet_problem", lambda self, spec, *a, **k: "")
    first = REQS.split("R2:")[0] + _task(_oversized())
    replay = H.run(tmp_path, monkeypatch, _script(first), files=FILES, max_tasks=2, record_complete=False)
    assert "Required role packet exceeds 12000 bytes; narrow the task" in (replay.result.error or "")
    record = H.result_json(replay)
    assert not (record.get("task_admissions") or [])
    (not_started,) = _events(replay, "not_started")
    assert not_started["tasks"] == ["t1"] and not_started["slots_used"] == 0
    assert _events(replay, "charged") == [], _ledger(replay)
    assert [r["task"] for r in record["not_started"]] == ["t1"]
    assert replay.of("lead") == []


def test_a_packet_refusal_after_admission_stays_charged(tmp_path, monkeypatch):
    """Retained negative: work that began keeps its charge."""
    original = S.Session._admit_dispatch

    def admitted_then_refused(self, *args, **kwargs):
        original(self, *args, **kwargs)
        raise PacketTooLarge(13_000, 12_000, {}, role="reviewer")
    monkeypatch.setattr(S.Session, "_admit_dispatch", admitted_then_refused)
    replay = H.run(tmp_path, monkeypatch, _script(REQS.split("R2:")[0] + _task(_narrow())), files=FILES,
                   max_tasks=2, record_complete=False)
    assert "Required role packet exceeds 12000 bytes" in (replay.result.error or "")
    assert [e["tasks"] for e in _events(replay, "charged")] == [["t1"]], _ledger(replay)
    assert _events(replay, "not_started") == []

"""Deferred child evidence, declared not discovered (Davis, 2026-10-28). A
parallel child measures no execution, so its reviewer was told every
unexecuted row was an EVIDENCE gap and filed one finding per row for a
fact the harness held (controller-continuation-01 t4: seven findings,
seven hold records). The harness now marks the rows deferred to the merge
gate, declares the deferral per reviewer, runs the rows at the merge and
asks the same reviewer what the execution shows; a NOT SHOWN line is that
reviewer's EVIDENCE finding, SHOWN is a recorded reading that makes nothing
met, and a merge gate that does not pass leaves the rows missing and the
deferral not rechecked."""

import pytest

from tests.lifecycle import harness as H
from tests.lifecycle import replies
from tests.lifecycle.test_lifecycle_matrix import FILES, FIXED, Script
from tests.lifecycle.test_recovery_handoff import BATCH, README_DONE
from tests.lifecycle.test_step2_recovery import _plan

CASE = "tests/test_app.py::test_add"


def _script(*, reading="P1: SHOWN test_add ran and returned 3 for add(1, 2)", broken=False, proves=True,
            open_finding=True):
    def lead(call, replay):
        if call.task == "t1":
            H.write(call, {"app.py": FILES["app.py"] if broken else FIXED})
            bound = f"\nPROVES: P1 - {CASE}" if proves else ""
            return f'Implemented add.{bound}\nCHANGED: ["app.py"]'
        H.write(call, {"README.md": README_DONE})
        return 'Documented add.\nCHANGED: ["README.md"]'

    def collaborator(call, replay):
        if call.task == "t1":
            # A DEFERRED line keeps a finding open through the child, so the
            # reviewer's post-merge recheck call exists; the deferred rows
            # ride in it.
            return ("DEFERRED: t2 - README must state what add returns\nR1: MET - app.py adds\nREVIEW: COMPLETE"
                    if open_finding else "NO FINDINGS\nR1: MET - app.py adds\nREVIEW: COMPLETE")
        return "NO FINDINGS\nR2: MET - README.md states what add returns\nREVIEW: COMPLETE"

    def recheck(call, replay):
        if "Rows the harness deferred to the merge gate" in call.prompt:
            return replies.recheck_reply(call.prompt, default="RESOLVED the merged README states it") + "\n" + reading
        return replies.recheck_reply(call.prompt, default="RESOLVED shown")

    script = Script(orchestrator=_plan(BATCH), lead=lead, collaborator=collaborator,
                    auditor=lambda c, r: "R1: MET - app.py\nR2: MET - README.md",
                    **{"requirements-review": lambda c, r: "COMPLETE"})
    script.overrides["recheck"] = recheck
    script.overrides["gate-fix"] = lead
    return script


def _cli_vendor(peer: str) -> str:
    """The CLI vendor a roster peer is called through: the openai namespace
    is the codex CLI; claude and grok are their own (root, 2026-10-28). The
    peer key itself stays the reviewer's identity on the record."""
    vendor = peer.split(":")[0]
    return "codex" if vendor == "openai" else vendor


@pytest.mark.requirements_ledger
def test_the_child_reviewer_is_told_the_rows_are_deferred_and_reads_them_after_the_merge(tmp_path, monkeypatch):
    replay = H.run(tmp_path, monkeypatch, _script(), files=FILES, max_tasks=3, record_complete=False)
    review = [c for c in replay.of("collaborator") if c.task == "t1"][0].prompt
    assert "deferred to the merge gate (declared by the harness" in review
    assert "Do not report a deferred row's non-execution as an EVIDENCE finding" in review
    record = H.result_json(replay)
    reviewers = sorted({c.vendor for c in replay.of("collaborator") if c.task == "t1"})
    deferral = sorted((d for d in record["deferred_evidence"] if d["task"] == "t1"), key=lambda d: d["peer"])
    assert sorted(_cli_vendor(d["peer"]) for d in deferral) == reviewers and reviewers, "one declaration per reviewer"
    assert len({d["peer"] for d in deferral}) == len(deferral), "each declaration names its exact reviewer"
    for entry in deferral:
        assert entry["proof_ids"] == ["P1"] and entry["rechecked"] == "rechecked"
        assert entry["reading"]["P1"].startswith("SHOWN (the reviewer's reading")
    rechecks = [c for c in replay.of("recheck") if "Rows the harness deferred" in c.prompt]
    assert sorted(c.vendor for c in rechecks) == reviewers, "one recheck call per reviewer, the EVIDENCE findings' own"
    assert all(c.task == "t1" and "execution ran (receipt" in c.prompt for c in rechecks)
    rows = [r for r in record["parallel_batches"][0]["post_merge_rechecks"] if r["task"] == "t1"][0]["rechecked"]
    assert all(r["deferred"] == ["P1"] and r["verdict"] == "resolved" and r["new"] == [] for r in rows)
    assert not [r for r in record["review_findings"] if r["task"] == "t1" and r["claim"].startswith("EVIDENCE")], \
        "no EVIDENCE finding was filed for the fact"
    merged = [o for o in record["evidence_obligations"] if o["task"] == "t1" and o["kind"] == "acceptance"][-1]
    assert merged.get("deferred") is None and merged["execution"] == "ran"
    assert "Deferred evidence" not in replay.of("orchestrator")[1].prompt


@pytest.mark.requirements_ledger
def test_a_not_shown_line_is_the_reviewer_s_own_evidence_finding_held_open(tmp_path, monkeypatch):
    replay = H.run(tmp_path, monkeypatch, _script(reading="P1: NOT SHOWN the case does not exercise a non-number"),
                   files=FILES, max_tasks=3, record_complete=False)
    record = H.result_json(replay)
    deferral = {d["peer"]: d for d in record["deferred_evidence"] if d["task"] == "t1"}
    findings = [r for r in record["review_findings"] if r["task"] == "t1" and r["claim"].startswith("EVIDENCE")]
    assert len(findings) == len(deferral) >= 1, "one EVIDENCE finding per reviewer that said NOT SHOWN"
    for finding in findings:
        assert finding["claim"].startswith("EVIDENCE P1 - the case does not exercise") and not finding["closed"]
        assert finding["author"] in deferral
        assert deferral[finding["author"]]["reading"]["P1"].startswith(f"NOT SHOWN, recorded as {finding['fid']}")
        assert any(text.startswith(f"{finding['fid']}: UNRESOLVED EVIDENCE P1") for text in record["open_findings"])
        assert finding["fid"] in replay.of("orchestrator")[1].prompt


@pytest.mark.requirements_ledger
def test_a_shown_line_on_a_row_that_did_not_run_is_refused_and_the_gate_failure_leaves_the_deferral_unrechecked(tmp_path, monkeypatch):
    replay = H.run(tmp_path, monkeypatch, _script(broken=True), files=FILES, max_tasks=3, record_complete=False)
    record = H.result_json(replay)
    deferral = [d for d in record["deferred_evidence"] if d["task"] == "t1"][0]
    assert deferral["rechecked"] == "not rechecked: the merge gate did not pass" and deferral["reading"] is None
    assert not [c for c in replay.of("recheck") if "Rows the harness deferred" in c.prompt]
    row = [o for o in record["evidence_obligations"] if o["task"] == "t1" and o["kind"] == "acceptance"][-1]
    assert row["execution"] != "ran"
    assert (replay.result.error or "").startswith("FindingsOpen"), "the merge debt stops the run as before"


@pytest.mark.requirements_ledger
def test_a_shown_line_on_a_row_the_merge_gate_did_not_execute_is_refused(tmp_path, monkeypatch):
    """The gate passes but the row is unbound (no PROVES line), so nothing
    ran for it: the reviewer's SHOWN establishes nothing and says so."""
    replay = H.run(tmp_path, monkeypatch, _script(proves=False), files=FILES, max_tasks=3, record_complete=False)
    record = H.result_json(replay)
    for deferral in (d for d in record["deferred_evidence"] if d["task"] == "t1"):
        assert deferral["rechecked"] == "rechecked"
        assert deferral["reading"]["P1"].startswith("SHOWN refused: the row's execution is missing")
    row = [o for o in record["evidence_obligations"] if o["task"] == "t1" and o["kind"] == "acceptance"][-1]
    assert row["execution"] == "missing"
    assert not [r for r in record["review_findings"] if r["task"] == "t1" and r["claim"].startswith("EVIDENCE")]


@pytest.mark.requirements_ledger
def test_a_reviewer_that_left_nothing_open_is_not_called_for_its_deferred_rows(tmp_path, monkeypatch):
    """No new call: the deferral is recorded as not asked, the rows are the
    merge gate's execution facts, and the audit judges sufficiency."""
    replay = H.run(tmp_path, monkeypatch, _script(open_finding=False), files=FILES, max_tasks=3, record_complete=False)
    record = H.result_json(replay)
    deferral = [d for d in record["deferred_evidence"] if d["task"] == "t1"]
    assert deferral and all(d["rechecked"].startswith("not asked: the reviewer left no finding open") for d in deferral)
    assert not replay.of("recheck"), "no recheck call was made for the deferred rows alone"
    row = [o for o in record["evidence_obligations"] if o["task"] == "t1" and o["kind"] == "acceptance"][-1]
    assert row["execution"] == "ran"

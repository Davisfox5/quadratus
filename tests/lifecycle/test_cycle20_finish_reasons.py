"""Cycle20 lifecycle (PR73 6748219666, reconciled by root): what the planner,
the repair and the terminal summary are shown of an audit's NOT MET.

Cycle19 terminal (run 20261109T173424Z-d77c4002): the DONE send-back and
``finish_routing`` cut each reason at 300 characters in silence and named no
artifact, so the planner could not FETCH the rest; the terminal summary cut
the send-back at 400 characters inside a provenance label; the audit's NOT
MET form asked for one example, so each audit handed one example per
requirement to one repair. Separately, a straight apostrophe inside a word
was read as a quote mark, so a basis quoting the requirement could be
labelled "not found".

Now: a clipped reason says how much is left out and names the artifact that
holds the whole text, which the planner's ordinary FETCH serves; the terminal
summary cuts outside labels and keeps the requirement id; the audit may name
up to three distinct examples it found, in the same line; quotes pair at word
boundaries. No call, slot, budget or verdict changes.
"""
import json
import re

import pytest

from tests.lifecycle import harness as H
from tests.lifecycle.replies import judging_scenarios
from tests.lifecycle.test_lifecycle_matrix import FILES, FIXED
from tests.lifecycle.test_proof_binding import CASE, PROOF_R1, SHOWN, _at, _plan, _proofs, _scope
from tests.lifecycle.test_workflow_repair import MET, Seen, _script

pytestmark = pytest.mark.requirements_ledger

EXAMPLES = "add(-1, -2) returns 3 | add(-5, 0) returns 5 | add(-1, 1) returns 2"
EXPLANATION = "add drops the sign of negative operands on every path that sums them, " * 4
LONG = (f"R1: NOT MET - {EXPLANATION.strip()}; basis: the requirement's own words \"returns the sum\" are false "
        f"for negatives, and the goal's contract isn't met; evidence: source reading; at app.py add; "
        f"example: {EXAMPLES}")
ABSENT = (f"R1: NOT MET - {EXPLANATION.strip()}; basis: “Invalid inputs … return HTTP 400”; "
          f"evidence: source reading; at app.py add; example: {EXAMPLES}")
MARKER = re.compile(r"\[\.\.\. (\d+) more characters in audit reply (\w+); read it whole with FETCH: (\w+)\]")


def _run(tmp_path, monkeypatch, name, *, first, second, orchestrate, max_tasks=2):
    seen = Seen()
    audits = []
    repair = (f"KIND: backend simple\nSCOPE: {json.dumps(_scope(proofs=_proofs('R1')))}\n"
              "Repair add for negatives in app.py.\nCOVERS: R1")

    def orchestrator(call, replay):
        seen.keep("orchestrator", call)
        return orchestrate(call, replay, repair)

    def lead(call, replay):
        seen.keep(f"lead:{call.task}", call)
        body = FIXED if call.task == "t1" else "def add(a, b):\n    # negatives too\n    return a + b\n"
        H.write(call, {"app.py": body})
        return f'Implemented add.\nPROVES: P1 - {CASE}\nCHANGED: ["app.py"]'

    def verdict(call, replay):
        audits.append(call)
        return first if len(audits) == 1 else second
    script = _script(seen, orchestrator=orchestrator, lead=lead,
                     collaborator=lambda c, r: "NO FINDINGS" + MET + "\nREVIEW: COMPLETE",
                     recheck=lambda c, r: "RFT2-1: RESOLVED", auditor=judging_scenarios(verdict, PROOF_R1, basis=SHOWN))
    replay = H.run(_at(tmp_path, name), monkeypatch, script, files=FILES, max_tasks=max_tasks,
                   record_complete=False)
    return replay, seen, audits


def _fetching(call, replay, repair):
    """Plan t1, say DONE; on the send-back FETCH the artifact it names; with
    the whole reason in hand plan the repair; then DONE."""
    if not replay.of("lead"):
        return _plan(_scope(proofs=_proofs("R1")))
    tail = LONG.split(" - ", 1)[1][300:]
    if any(c.task == "t2" for c in replay.of("lead")):
        return "DONE"
    if tail in call.prompt:
        return repair
    marker = MARKER.search(call.prompt)
    if marker:
        return f"FETCH: {marker.group(2)}"
    return "DONE"


def test_a_clipped_reason_names_its_artifact_and_the_planners_fetch_reads_it_whole(tmp_path, monkeypatch):
    replay, seen, audits = _run(tmp_path, monkeypatch, "fetch", first=LONG, second=f"R1: MET - {CASE}",
                                orchestrate=_fetching)
    planner = [c.prompt for c in replay.of("orchestrator")]
    reason = LONG.split(" - ", 1)[1]
    sent = next(i for i, p in enumerate(planner) if MARKER.search(p))
    marker = MARKER.search(planner[sent])
    assert int(marker.group(1)) == len(reason) - 300
    assert marker.group(2) == marker.group(3)
    record = H.result_json(replay)
    first = record["requirements"]["audits"][0]
    assert marker.group(2) == first["reply_artifact"], "the reply that holds the words"
    assert reason[:300] in planner[sent] and reason[300:] not in planner[sent], "the payload bound is unchanged"
    assert reason[300:] in planner[sent + 1], "the planner's FETCH served the whole reply"
    fetches = [c for c in replay.of("orchestrator") if c.reply.strip().startswith("FETCH:")]
    assert len(fetches) <= 1
    (routed,) = [r for r in record["finish_routing"] if r["site"] == "done refused"]
    assert routed["why"] == reason[:300] and routed["why_omitted"] == len(reason) - 300
    assert routed["why_artifact"] == first["reply_artifact"] and routed["why_source"] == "audit reply"
    # A: the apostrophes beside the real quote no longer read as quote marks.
    assert first["bases"]["R1"]["quoted"] == ["returns the sum"] and first["bases"]["R1"]["quoted_in"] == ["R1"]
    assert "(its quoted basis was not found" not in planner[sent]
    # C: the audit is asked for up to three examples; all three reach the repair.
    assert "at most 3, separated by ' | '" in audits[0].prompt
    assert f"Example: {EXAMPLES}." in seen.prompts["lead:t2"][0]
    assert [c.task for c in replay.of("lead")] == ["t1", "t2"], "no extra task"
    assert len(audits) == 2, "no extra audit"
    assert replay.result.completed, replay.result.error


def _repeating(call, replay, repair):
    return _plan(_scope(proofs=_proofs("R1"))) if len(replay.of("orchestrator")) == 1 else "DONE"


def test_the_terminal_summary_keeps_the_requirement_and_never_cuts_a_label(tmp_path, monkeypatch):
    replay, seen, audits = _run(tmp_path, monkeypatch, "terminal", first=ABSENT, second=ABSENT,
                                orchestrate=_repeating, max_tasks=1)
    error = replay.result.error or ""
    assert not replay.result.completed and "R1:" in error, error
    assert "more characters" in error, error
    assert "(its quoted basis" not in error or "recorded only)" in error, "a label is whole or absent"
    record = H.result_json(replay)
    # The quote with a literal ellipsis is honestly absent: no fuzzy match.
    assert record["requirements"]["audits"][0]["bases"]["R1"]["quoted_in"] == []
    final = [r for r in record["finish_routing"] if r["site"] in ("done refused", "final challenge")]
    assert final and all(r["why_artifact"] for r in final), "every clipped reason names where it is whole"
    assert all(not r.get("met") for r in [record["requirements"]["audits"][-1]["verdicts"]["R1"]])

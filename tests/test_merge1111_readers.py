"""The development line's line readers, held to the one verdict reader.

Conformance audit of the merged tree, 2026-11-12 (findings readers 1 to 8,
truth 1, 2 and 6, leftovers 1). Before these fixes a recheck line, an
evidence row line, a reviewer's requirement line and an inspection line each
took any text after the accepting word as its reason, so "RESOLVED once
fixed", "SUFFICIENT? No" and "MET, but only partly" were acceptances and a
run reported complete over them. Every test here failed at 6d8e9b8.
Scripted replies only; no model is called.
"""
from __future__ import annotations

import types
from types import SimpleNamespace

import pytest

import quadratus.session as S
from quadratus import delivery, judgment, verdicts

CONDITIONED = [
    "once add() raises TypeError for a non-number; this revision changed nothing",
    ", but only partly: add('x', 1) still returns 'x1'",
    "? No. add('x', 1) still returns 'x1'.",
    "or UNRESOLVED",
    "partially; the negative case still returns the wrong sign",
    "- no, wait: UNRESOLVED, add still returns x1",
    "- but still unresolved in app.py",
    "- once the null check is added",
    "- only if tests/test_app.py::test_close is trusted",
]


# -- readers 1, truth 1: a recheck line accepts only when it says so plainly ----

@pytest.mark.parametrize("tail", CONDITIONED)
def test_a_conditioned_resolved_line_is_no_verdict_for_its_finding(tail):
    reading = judgment.recheck_verdicts(f"RFT1-1: RESOLVED {tail}", ["RFT1-1"])
    assert reading.dispositions["RFT1-1"].state == judgment.INVALID
    assert not reading.judged_any
    two = judgment.recheck_verdicts(f"RFT1-1: RESOLVED {tail}\nRFT1-2: RESOLVED", ["RFT1-1", "RFT1-2"])
    assert two.dispositions["RFT1-1"].state == judgment.INVALID
    assert two.dispositions["RFT1-2"].state == judgment.RESOLVED


@pytest.mark.parametrize("line, why", [
    ("RFT1-1: RESOLVED", ""),
    ("RFT1-1: RESOLVED.", ""),
    ("RFT1-1: RESOLVED - add() now raises TypeError", "add() now raises TypeError"),
    ("RFT1-1: RESOLVED: add() now raises TypeError", "add() now raises TypeError"),
    ("- **RFT1-1**: RESOLVED - no longer returns 'x1'", "no longer returns 'x1'"),
])
def test_a_plain_resolved_line_still_resolves(line, why):
    verdict = judgment.recheck_verdicts(line, ["RFT1-1"]).dispositions["RFT1-1"]
    assert (verdict.state, verdict.why) == (judgment.RESOLVED, why)


@pytest.mark.parametrize("line", [
    "RFT1-1: UNRESOLVED add('x', 1) still returns 'x1'",
    "RFT1-1: NOT RESOLVED once you look at app.py",
    "RFT1-1: UNRESOLVED? maybe, but RESOLVED is not earned",
])
def test_a_rejection_keeps_a_free_tail(line):
    assert judgment.recheck_verdicts(line, ["RFT1-1"]).dispositions["RFT1-1"].state == judgment.UNRESOLVED


@pytest.mark.parametrize("reply", [
    "RFT2-1: INVALIDATED? No: app.py still leaks the handle",
    "RFT2-1: INVALIDATED only if tests/test_app.py::test_close is trusted, and it never ran",
    "RFT2-1: INVALIDATED or UNRESOLVED per app.py",
])
def test_a_conditioned_invalidated_line_closes_no_carried_finding(reply):
    finding = delivery.finding_from_line("RFT2-1", "app.py leaks the file handle on error", author="claude:opus",
                                         role="collaborator", source_digest="src1", task="t2")
    reading = judgment.recheck_verdicts(reply, ["RFT2-1"])
    assert reading.dispositions["RFT2-1"].state == judgment.INVALID
    after = delivery.apply_reading((finding,), reading, author="openai:sol", role="audit", binding="b",
                                   judged_binding="b", source_digest="src1", resolve=lambda why: "app.py" in why)
    assert not after[0].closed


def test_a_plain_invalidated_line_keeps_its_quoted_command_as_evidence():
    verdict = judgment.recheck_verdicts("F1: INVALIDATED - `pytest -q` passes", ["F1"]).dispositions["F1"]
    assert verdict.state == judgment.INVALIDATED and "`pytest -q`" in verdict.why


# -- readers 2, truth 2: an evidence row line ------------------------------------

ROWS = [
    "O1: SUFFICIENT if a case also showed add() rejecting a non-number, which no executed case does",
    "O1: SUFFICIENT, but only for two positive integers",
    "O1: SUFFICIENT or INSUFFICIENT",
    "O1: SUFFICIENT? No: the case never checks a float.",
    "O1: SUFFICIENT only if the negative-number case is added",
    "O1: SUFFICIENT - only if the negative-number case is added",
    "O1: SUFFICIENT except the refusal path",
]


@pytest.mark.parametrize("line", ROWS)
def test_a_conditioned_sufficient_line_is_no_verdict_for_its_row(line):
    read, conflicts = judgment.sufficiency_reading(line, ["O1"])
    assert read == {} and "O1" in conflicts
    assert judgment.sufficiency_verdicts(line, ["O1"]) == {}


def test_plain_row_lines_are_read_and_a_rejection_keeps_its_tail():
    read, conflicts = judgment.sufficiency_reading(
        "O1: SUFFICIENT - the case adds two integers\n- **O2**: SUFFICIENT\nO3: INSUFFICIENT only the happy path ran",
        ["O1", "O2", "O3"])
    assert conflicts == {}
    assert read == {"O1": ("sufficient", "the case adds two integers"), "O2": ("sufficient", ""),
                    "O3": ("insufficient", "only the happy path ran")}


@pytest.mark.parametrize("line", ROWS[:4])
def test_a_conditioned_sufficient_keeps_the_audited_requirement_not_met(tmp_path, line):
    from tests.test_evidence_lifecycle_units import CASE, _executed_row, _gate, _project, _session
    root = _project(tmp_path)
    session = _session(tmp_path, project=root, gate=_gate(root), requirements=("R1", "R2"))
    _executed_row(session)
    found = {"R1": (True, CASE), "R2": (True, CASE)}
    session._judge_obligations("claude:opus", f"R1: MET - {CASE}\nR2: MET - {CASE}\n{line}", dict(found),
                               ids=["R1", "R2"])
    assert session.evidence_obligations[0]["sufficiency"] != "sufficient"
    assert session._apply_acceptance(found, ["R1", "R2"]), "the unjudged row lowers its requirements"


@pytest.mark.parametrize("line", [
    "O2: SUFFICIENT if the socket path is also exercised, which these cases do not do",
    "O2: SUFFICIENT? No, the case never opens the socket.",
])
def test_a_conditioned_sufficient_settles_no_restated_not_run_item(tmp_path, line):
    from tests.lifecycle.test_proof_binding import CASE, _session
    session = _session(tmp_path)
    session.project = tmp_path
    session._source_fingerprint = lambda: "S"
    entry = dict(task="t1", oid="O2", item="app.py", why="", cases=[CASE], binding=dict(kind="case", cases=[CASE]),
                 requirements=["R1"], after_check=0,
                 restatements=[dict(task="t1", author="claude:opus", cases=[CASE])])
    session.unexecuted_acceptance = [entry]
    session._judge_restated("openai:sol", line)
    assert not session._restated_judged(entry)
    assert S.Session._restatement_unread(line, ["O2"]) == ["O2"]


def test_a_design_approval_followed_by_a_questioned_row_is_no_approval():
    assert S._design_approved("APPROVED\nO1: SUFFICIENT - the render shows the empty state")
    assert S._design_approved("APPROVED\nO1: INSUFFICIENT the render misses it")       # integrator ruling 6
    assert not S._design_approved("APPROVED\nO1: SUFFICIENT? No: the render misses the empty state")


# -- readers 3: formatting hides no line, and a contradiction is not dropped -----

@pytest.mark.parametrize("extra", [
    "UNRESOLVED: add('x', 1) still returns 'x1'",
    "### UNRESOLVED: add('x', 1) still returns 'x1'",
    "`RFT1-1`: UNRESOLVED add('x', 1) still returns 'x1'",
    "### RFT1-1: UNRESOLVED add('x', 1) still returns 'x1'",
])
def test_a_contradicting_line_beside_resolved_keeps_the_finding_open(extra):
    reading = judgment.recheck_verdicts("RFT1-1: RESOLVED\n" + extra, ["RFT1-1"])
    assert reading.dispositions["RFT1-1"].state == judgment.UNRESOLVED
    assert "x1" in reading.dispositions["RFT1-1"].why


@pytest.mark.parametrize("extra", [
    "### BLOCKING: the revision broke add(1.5, 2), which now raises | basis: app.py | impact: correctness",
    "`NEW: the revision broke add(1.5, 2), which now raises | basis: app.py | impact: correctness`",
    "*NEW:* the revision broke add(1.5, 2), which now raises | basis: app.py | impact: correctness",
])
def test_a_new_finding_under_formatting_is_registered(extra):
    reading = judgment.recheck_verdicts("RFT1-1: RESOLVED\n" + extra, ["RFT1-1"])
    assert len(reading.new) == 1 and "add(1.5, 2)" in reading.new[0].line
    assert "| basis: app.py | impact: correctness" in reading.new[0].line


def test_a_bare_unresolved_line_beside_several_ids_is_a_new_finding():
    reading = judgment.recheck_verdicts("RFT1-1: RESOLVED\nRFT1-2: RESOLVED\nUNRESOLVED: the lock is never released",
                                        ["RFT1-1", "RFT1-2"])
    assert [n.line for n in reading.new] == ["the lock is never released"]
    assert judgment.recheck_verdicts("RFT1-1: RESOLVED\nUNRESOLVED: none", ["RFT1-1"]).dispositions[
        "RFT1-1"].state == judgment.RESOLVED


# -- readers 5: the bare one-finding form is read by verdicts.resolved_verdict ---

@pytest.mark.parametrize("reply", [
    "The lock was added but the race isn't\nresolved",
    "I cannot say this is\nresolved",
    "RESOLVED\n## BLOCKING: the lock is never released",
])
def test_the_bare_form_closes_nothing_on_a_wrapped_sentence_or_beside_a_blocker(reply):
    finding = delivery.finding_from_line("RFT2-1", "the lock races", author="claude:opus", role="collaborator",
                                         source_digest="src1", task="t2")
    reading = judgment.recheck_verdicts(reply, ["RFT2-1"])
    assert reading.dispositions["RFT2-1"].state != judgment.RESOLVED
    assert judgment.recheck_state(reply) != judgment.RESOLVED
    after = delivery.apply_reading((finding,), reading, author="claude:opus", role="reassessment", binding="b",
                                   judged_binding="b", source_digest="src1", resolve=lambda why: False)
    assert not after[0].closed
    if "BLOCKING" in reply:
        assert reading.dispositions["RFT2-1"].state == judgment.UNRESOLVED and len(reading.new) == 1


def test_the_bare_form_still_reads_a_plain_verdict():
    assert judgment.recheck_verdicts("**RESOLVED.**", ["F1"]).dispositions["F1"].state == judgment.RESOLVED
    assert judgment.recheck_verdicts("UNRESOLVED: still leaks", ["F1"]).dispositions["F1"] == judgment.Verdict(
        judgment.UNRESOLVED, "still leaks")
    assert judgment.recheck_verdicts("I looked at it.", ["F1"]).dispositions["F1"].state == judgment.INVALID


# -- readers 4, leftovers 1: a reviewer's requirement line -----------------------

HEDGED_REVIEWS = [
    "NO FINDINGS\nR9: MET, but only partly: subtract(a, b) is not documented at all",
    "NO FINDINGS\nR9: MET once the README gains the subtract section",
    "R9: MET except the return type, which the README states wrongly\nREVIEW: COMPLETE",
    "NO FINDINGS\nR9: MET or NOT MET",
    "NO FINDINGS\nR9: MET - but only partly; the refusal path is untested",
]


@pytest.mark.parametrize("note", HEDGED_REVIEWS)
def test_a_hedged_met_line_is_no_verdict_in_a_review(note):
    assert "R9" not in S._requirement_verdicts(note)
    assert not S._clean_goal_review(note, ["R9"])
    assert not S._approving_review(note, ["R9"])
    assert S._review_disposition(note, False, ["R9"]) != "reviewed: no findings"
    assert S._met_lines(note) == {}


def test_plain_requirement_lines_still_read_and_not_met_still_wins():
    note = "NO FINDINGS\n- **R1**: MET - app.py:3\nR2: MET\nREVIEW: COMPLETE"
    assert S._requirement_verdicts(note) == {"R1": True, "R2": True}
    assert S._clean_goal_review(note, ["R1", "R2"]) and S._approving_review(note, ["R1", "R2"])
    assert S._requirement_verdicts("R1: MET, but only partly\nR1: NOT MET - add() concatenates") == {"R1": False}
    assert S._requirement_verdicts("R1: MET - app.py\nR1: MET, but only partly") == {}


@pytest.mark.parametrize("note", HEDGED_REVIEWS[:4])
def test_a_hedged_met_line_leaves_the_review_unfinished_and_keeps_the_caveat(tmp_path, note):
    from tests.lifecycle.test_documentation_claims import REQS
    from tests.lifecycle.test_documentation_claims import _session as doc_session
    session = doc_session(tmp_path, REQS)
    session._covered_texts = lambda: [("R9", REQS["R9"])]
    session._current_covers = ["R9"]
    owed = []
    session._owe_review = lambda spec, peer, kind, why, **kw: owed.append(why)
    assert session._review_gaps(note), "the same seat is asked to complete its judgment"
    judged = session._judge_goal_review(SimpleNamespace(task_id="t5"), "openai:sol", note)
    assert owed and "R9" in owed[0] and session._goal_review_owed
    assert not S._approving_review(judged, ["R9"])
    assert any("R9" in n["text"] for n in session.review_notes), "the refused line is kept as a note for the audit"


# -- readers 6: one reader for the MET line and its citation ---------------------

@pytest.mark.parametrize("met_line", ["R7: MET - api.py:40", "- R7: MET - api.py:40", "**R7**: MET - api.py:40",
                                      "* R7: MET - api.py:40"])
def test_a_bulleted_or_bold_met_on_an_error_requirement_is_held_like_a_plain_one(tmp_path, met_line):
    from tests.lifecycle.test_documentation_claims import R23, REQS
    from tests.lifecycle.test_documentation_claims import _session as doc_session
    session = doc_session(tmp_path, REQS)
    session._covered_texts = lambda: [("R23", R23), ("R7", REQS["R7"])]
    session._executed_cases = lambda cited, rid=None: []
    session._last_scratch_proof = None
    note = f"NO FINDINGS\nR23: MET - README.md\n{met_line}\nREVIEW: COMPLETE"
    held = session._hold_unexercised_errors(SimpleNamespace(task_id="t5"), "openai:sol", note,
                                            S._requirement_verdicts(note))
    assert "R7: UNVERIFIED" in held and "R7: MET" not in held.replace("MET claimed", "")
    assert not S._clean_goal_review(held, ["R23", "R7"])
    assert [n["requirements"] for n in session.review_notes if n.get("risk") == "untested"] == [["R7"]]


# -- readers 8: a READ line attests only in its stated form ----------------------

def _attest(reply):
    gap = dict(id="IG-0123456789", task="t1", files=["a.py", "b.py"], item="Read a.py b.py",
               handed=[dict(task="t1", peer="p", stage="review", source="src", files={"a.py": "d1", "b.py": "d2"},
                            consumed=False, at=0)])
    fake = types.SimpleNamespace(inspection_gaps=[gap], _note=lambda m: None)
    S.Session._attest_inspections(fake, "t1", "p", "review", reply)
    return gap


@pytest.mark.parametrize("reply", [
    "READ IG-0123456789 not yet\nREVIEW: COMPLETE",
    "READ IG-0123456789: could not open either file\nREVIEW: COMPLETE",
    "READ IG-0123456789 - only tickets/store.py; the second file would not open\nREVIEW: COMPLETE",
    "READ IG-0123456789 - \nREVIEW: COMPLETE",
    "READ IG-0123456789 - both read\nREAD IG-0123456789 not really\nREVIEW: COMPLETE",
])
def test_a_read_line_that_is_not_the_stated_form_attests_nothing(reply):
    gap = _attest(reply)
    assert "attested" not in gap and not S._inspection_attested(gap)
    assert gap["refusals"] and gap["handed"][0]["consumed"]


@pytest.mark.parametrize("reply", ["READ IG-0123456789 - both files define the store's schema\nREVIEW: COMPLETE",
                                   "- **READ IG-0123456789** - both files define the store's schema"])
def test_a_read_line_in_the_stated_form_attests(reply):
    gap = _attest(reply)
    assert gap["attested"]["judgment"] == "both files define the store's schema"
    assert "attested" not in _attest("READ IG-0123456789 - shows x\nUNREAD IG-0123456789 - b.py would not open")


# -- whole runs ------------------------------------------------------------------

BLOCK = "BLOCKING: add('x', 1) returns a string instead of raising TypeError. | basis: R1 | impact: correctness"


@pytest.mark.requirements_ledger
@pytest.mark.parametrize("tail", ["RESOLVED once add() raises TypeError for a non-number; this revision changed nothing",
                                  "RESOLVED or UNRESOLVED"])
def test_a_run_does_not_complete_over_a_conditioned_recheck(tmp_path, monkeypatch, tail):
    from tests.lifecycle import harness as H
    from tests.lifecycle.replies import expected_finding_ids
    from tests.lifecycle.test_recovery_lifecycle import R1, T1, _finding, _run

    def recheck(call, replay):
        return "\n".join(f"{fid}: {tail}" for fid in expected_finding_ids(call.prompt))

    replay = _run(tmp_path, monkeypatch, R1 + T1, collaborator=lambda c, r: BLOCK, recheck=recheck)
    assert not replay.result.completed
    assert not _finding(H.result_json(replay), "RFT1-1")["closed"]


@pytest.mark.requirements_ledger
@pytest.mark.parametrize("extra", [
    "UNRESOLVED: add('x', 1) still returns 'x1'",
    "### BLOCKING: the revision broke add(1.5, 2), which now raises | basis: app.py | impact: correctness",
])
def test_a_run_does_not_complete_over_a_line_that_contradicts_resolved(tmp_path, monkeypatch, extra):
    from tests.lifecycle.replies import expected_finding_ids
    from tests.lifecycle.test_recovery_lifecycle import R1, T1, _run

    def recheck(call, replay):
        ids = expected_finding_ids(call.prompt)
        if len(replay.of("recheck")) > 1:
            return "\n".join(f"{fid}: UNRESOLVED still open" for fid in ids)
        return "\n".join(f"{fid}: RESOLVED" for fid in ids) + "\n" + extra

    replay = _run(tmp_path, monkeypatch, R1 + T1, collaborator=lambda c, r: BLOCK, recheck=recheck)
    assert not replay.result.completed


@pytest.mark.requirements_ledger
@pytest.mark.parametrize("row, completes", [
    ("SUFFICIENT? No: the case never checks a float.", False),
    ("SUFFICIENT only if the negative-number case is added", False),
    ("SUFFICIENT - the executed case adds two integers", True),
])
def test_a_run_completes_only_on_a_plain_sufficient_row(tmp_path, monkeypatch, row, completes):
    from tests.lifecycle import harness as H
    from tests.lifecycle.replies import requested_obligations
    from tests.lifecycle.test_lifecycle_matrix import FILES, Script
    from tests.lifecycle.test_recovery_lifecycle import CAPPED, R1, T1, clean, lead
    from tests.lifecycle.test_step2_recovery import _plan

    def auditor(call, replay):
        rows = [r for r in requested_obligations(call.prompt) if r["execution"] == "ran"]
        return "\n".join(["R1: MET - app.py"] + [f"{oid}: {row}" for oid in dict.fromkeys(r["oid"] for r in rows)])

    script = Script(orchestrator=_plan(R1 + T1), lead=lead, collaborator=lambda c, r: clean(c), auditor=auditor,
                    **{"requirements-review": lambda c, r: "COMPLETE"})
    replay = H.run(tmp_path, monkeypatch, script, files=FILES, max_tasks=3, record_complete=False, settings=CAPPED)
    assert replay.result.completed is completes, replay.result.error
    rows = H.result_json(replay).get("evidence_obligations") or []
    assert [o["sufficiency"] == "sufficient" for o in rows] == [completes] * len(rows) and rows


# -- readers 7: TEST INTACT beside a blocking line -------------------------------

@pytest.mark.requirements_ledger
def test_test_intact_beside_a_blocking_line_certifies_nothing(tmp_path, monkeypatch):
    from tests.lifecycle import harness as H
    from tests.lifecycle import test_bounded_repair as B

    def comparison(call, replay):
        assert "voids every TEST INTACT" in call.prompt
        return (f"TEST INTACT {B.FAILURES} - the same cases remain\n"
                "BLOCKING: the storage-failure case no longer asserts the rows are unchanged | basis: "
                f"{B.FAILURES} | impact: correctness")

    replay = B._run(tmp_path, monkeypatch, source=B.SOUND, failures=B.BROKEN, fixed_source=B.SOUND,
                    fixed_failures=B.CORRECTED, comparison=comparison)
    (entry,) = H.result_json(replay)["generated_test_repairs"]
    assert entry["verdict"] == "pending" and entry["judgment"] is None
    assert "BLOCKING" in entry["why"] and not S._test_repair_verified(entry)
    assert not replay.result.completed


# -- truth 6: a review held as unfinished is not recorded as complete ------------

@pytest.mark.requirements_ledger
def test_a_review_held_as_unfinished_is_not_a_complete_review_on_the_record(tmp_path, monkeypatch):
    from tests.lifecycle import harness as H
    from tests.lifecycle.replies import judging_scenarios
    from tests.lifecycle.test_check_debt import IMPLEMENT, MET, REQS
    from tests.lifecycle.test_lifecycle_matrix import ADD_PROOF, ADD_SHOWN, FILES, FIXED, Script

    def fix(call, replay):
        H.write(call, {"app.py": FIXED})
        return 'Fixed add.\nPROVES: P1 - tests/test_app.py::test_add\nCHANGED: ["app.py"]'

    def orchestrator(call, replay):
        return REQS + IMPLEMENT if len(replay.of("orchestrator")) == 1 else "DONE"

    script = Script(orchestrator=orchestrator, lead=fix,
                    collaborator=lambda call, replay: "No blocking findings.\nREVIEW: COMPLETE",
                    auditor=judging_scenarios(lambda call, replay: MET, ADD_PROOF, basis=ADD_SHOWN),
                    **{"requirements-review": lambda call, replay: "COMPLETE"})
    replay = H.run(tmp_path, monkeypatch, script, files=FILES, max_tasks=3, record_complete=False)
    record = H.result_json(replay)
    assert [(f["kind"], f["closed"]) for f in record["review_findings"]] == [("unreviewed", False)]
    assert record["workflow"]["tasks"][0]["edges"]["review"] is False
    assert [r["complete"] for r in record["reviewers"] if r["role"] == "collaborator"] == [False]


# -- the one reader ----------------------------------------------------------------

def test_the_line_readers_live_in_verdicts_and_judgment_calls_them():
    assert verdicts.plain_acceptance("", "MET") == (True, "")
    assert verdicts.plain_acceptance(" - app.py:3", "MET") == (True, "app.py:3")
    for rest in (" once fixed", ", but only partly", "? No", " or NOT MET", " - if the test ran", " - maybe",
                 " - fine. NOT MET for floats", " -"):
        assert verdicts.plain_acceptance(rest, "MET")[0] is False, rest
    assert verdicts.recheck_line("prose about F1: RESOLVED") is None
    assert verdicts.row_line("O1: INSUFFICIENT? yes") == ("O1", "insufficient", "? yes")
    assert verdicts.requirement_line("R1: the parser accepts empty input") is None
    source = open(judgment.__file__, encoding="utf-8").read()
    assert "verdicts.recheck_line(" in source and "verdicts.row_line(" in source
    assert "verdicts.resolved_verdict(" in source


# -- check of the readers fixes: the same holes in other wording -----------------


@pytest.mark.parametrize("second", ["RFT1-1: NOT YET RESOLVED", "RFT1-1: PARTIALLY RESOLVED - the race stays",
                                    "- **RFT1-1**: only partly resolved"])
def test_a_second_line_naming_the_id_and_the_word_leaves_no_verdict(second):
    reading = judgment.recheck_verdicts("RFT1-1: RESOLVED - the check was added\n" + second, ["RFT1-1"])
    assert reading.dispositions["RFT1-1"].state == judgment.INVALID
    alone = judgment.recheck_verdicts(second, ["RFT1-1"])
    assert alone.dispositions["RFT1-1"].state == judgment.INVALID


def test_an_id_line_without_the_word_does_not_void_a_plain_verdict():
    reading = judgment.recheck_verdicts("RFT1-1: the lock\nRFT1-1: RESOLVED - app.py takes it", ["RFT1-1"])
    assert reading.dispositions["RFT1-1"].state == judgment.RESOLVED
    assert judgment.recheck_verdicts("RFT1-1:\nRESOLVED", ["RFT1-1"]).dispositions["RFT1-1"].state == judgment.RESOLVED


def test_not_sufficient_is_a_rejection_and_wins_beside_sufficient():
    read, conflicts = judgment.sufficiency_reading(
        "O1: SUFFICIENT - the case shows it\nO1: NOT SUFFICIENT\nO2: NOT SUFFICIENT - no float case\n"
        "O3: SUFFICIENT - ok\nO3: well, barely sufficient", ["O1", "O2", "O3"])
    assert read == {"O2": ("insufficient", "no float case")}
    assert set(conflicts) == {"O1", "O3"}


@pytest.mark.parametrize("line", ["READ IG-0123456789 - could not open either file",
                                  "READ IG-0123456789 - unable to find tickets/store.py",
                                  "READ IG-0123456789 - none of them were in my copy"])
def test_a_read_line_whose_reason_says_nothing_was_read_attests_nothing(line):
    assert [kind for kind, _, _ in verdicts.inspection_lines(line)] == [""]
    kept = verdicts.inspection_lines("READ IG-0123456789 - store.py defines no schema")
    assert [kind for kind, _, _ in kept] == ["READ"]

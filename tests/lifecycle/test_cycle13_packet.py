"""Cycle12 terminal (root's reconciliation): run 20261108T231038Z-c546ec92
stopped on ``call_token_ceiling`` when the third audit reported 504,836
tokens against the 500,000 per-call ceiling. Its 114,325-character prompt
carried evidence obligations (47,266 characters) whose rows state each
generated scenario summary and then the same scenario in full, superseded
rows in full beside the row that holds their scenario now, and task-review
observations (44,619) with the same explanation repeated on every line.

Cycle13 states each of these once without losing a judgment, a binding or a
provenance: a behaviour that is exactly the generated summary of its own
scenario is not repeated; a superseded row whose one successor can be named
exactly refers to it; identical observation texts are listed once with every
review that raised them. A read-only offline tool compares the old and new
renderers on one saved state. No limit, call, model or judgment changes.

Real Session renderers on hand-set records, one lifecycle through the audit,
and the tool on a synthetic run folder."""
import hashlib
import json
from pathlib import Path

import pytest

from tests.lifecycle import harness as H
from tests.lifecycle.replies import requested_obligations

pytestmark = pytest.mark.requirements_ledger


def _session(tmp_path, requirements=None, *, project=None):
    from quadratus.artifacts import ArtifactStore
    from quadratus.session import Session, SessionConfig
    session = Session("goal", ArtifactStore(tmp_path / "a"), lambda *a, **k: "",
                      config=SessionConfig(project=project, requirements_ledger=True))
    session.memory.ledger.requirements.update(requirements or {"R1": "add returns the sum"})
    return session


def _scenario(pid="P1", op="add(1, 2)", state="nothing stored", expect="3 is returned", loc="tests/test_app.py"):
    return dict(proof=pid, operation=op, state=state, expect=expect, location=loc, evidence="test")


def _row(oid, task, *, scenario=None, behavior=None, superseded_by=None, cases=(), execution="ran",
         sufficiency="unjudged", requirements=(("R1", 1),), modality="test", **extra):
    scenario = _scenario() if scenario is None else scenario
    generated = (f"{scenario['proof']}: {scenario['operation']}; given {scenario['state']}; "
                 f"expect {scenario['expect']}; in {scenario['location']}")[:300] if scenario else None
    row = dict(oid=oid, task=task, kind="acceptance", requirements=[list(r) for r in requirements], gate="G",
               behavior=behavior if behavior is not None else generated, modality=modality, runner="gate",
               execution=execution, sufficiency=sufficiency, cases=list(cases), proof_id=(scenario or {}).get("proof"),
               **({"scenario": sorted(scenario.items())} if scenario else {}), **extra)
    if superseded_by:
        row.update(superseded_by=superseded_by, reason=f"superseded by task {superseded_by}'s declaration of the "
                                                         "same scenario")
    return row


def _render(session, rows, *, compact=True):
    session._compact_audit_packet = compact
    return session._obligations_block(rows)


# -- 1. a generated summary is stated once; any other behaviour stays whole ---------------------------

def test_a_generated_summary_is_stated_once_and_the_scenario_line_carries_it(tmp_path):
    session = _session(tmp_path)
    rows = [_row("O1", "t1", cases=["tests/test_app.py::test_add"])]
    old, new = _render(session, rows, compact=False), _render(session, rows)
    generated = rows[0]["behavior"]
    assert generated in old and generated not in new, "on 19a7894 the summary and the scenario both appear"
    assert "- O1 (t1; R1): scenario P1 (stated once below); evidence test; runner gate; execution ran" in new
    assert ("  scenario: proof: P1; operation: add(1, 2); state: nothing stored; expect: 3 is returned; "
            "location: tests/test_app.py; evidence: test; bound cases: tests/test_app.py::test_add") in new
    assert len(new) < len(old)
    (parsed,) = requested_obligations(new)
    assert parsed["behavior"] == generated and parsed["oid"] == "O1" and parsed["execution"] == "ran"


@pytest.mark.parametrize("name, row", [
    ("legacy string, no scenario", _row("O1", "t1", scenario={}, behavior="add works for negatives")),
    ("behaviour differs from its scenario", _row("O1", "t1", behavior="P1: add(1, 2); given nothing stored; "
                                                                       "expect 3; in tests/test_app.py")),
    ("scenario over the render bound", _row("O1", "t1", scenario=_scenario(op="x" * 4100))),
])
def test_any_other_behaviour_is_its_own_claim_and_stays_whole(tmp_path, name, row):
    session = _session(tmp_path)
    new = _render(session, [row])
    assert f"): {row['behavior']}; evidence" in new, name
    assert "(stated once below)" not in new
    if name == "scenario over the render bound":
        assert "over the 4,000-character bound for this prompt and not shown; leave this row unjudged" in new


# -- 2. a superseded row refers to its one exact successor ---------------------------------------------

def test_a_superseded_chain_refers_to_its_current_row_and_keeps_its_own_facts(tmp_path):
    session = _session(tmp_path)
    rows = [_row("O1", "t1", superseded_by="t2", execution="failed", cases=["tests/test_app.py::test_old"],
                 pre_supersession=dict(reason="tests/test_app.py::test_old failed on its source"),
                 sufficiency="insufficient", judged_by="openai:sol"),
            _row("O2", "t2", superseded_by="t3", scenario=_scenario(pid="P4")),
            _row("O3", "t3", scenario=_scenario(pid="P2"), cases=["tests/test_app.py::test_add"])]
    new = _render(session, rows)
    line = next(x for x in new.splitlines() if x.startswith("- O1 "))
    assert line.startswith("- O1 (t1; R1): superseded by task t2; its proof P1 is the same scenario as O3 (proof P2), "
                           "stated there; evidence test")
    assert "execution failed (before it was superseded: tests/test_app.py::test_old failed on its source)" in line
    assert "sufficiency insufficient by openai:sol" in line
    assert "its own bound cases: tests/test_app.py::test_old" in line
    assert next(x for x in new.splitlines() if x.startswith("- O2 ")).count("the same scenario as O3") == 1
    assert new.count("  scenario: proof:") == 1, "the shared scenario text is stated once, on O3"
    assert {r["oid"]: r["behavior"] for r in requested_obligations(new)}["O1"] == rows[2]["behavior"]


def test_a_superseded_row_with_a_distinct_claim_keeps_it_verbatim(tmp_path):
    """Root's repro on 10a9433: the compact line dropped the predecessor's own
    behaviour, so a distinct adverse claim disappeared."""
    session = _session(tmp_path)
    claim = "Distinct adverse claim: the parser still mutates on malformed input."
    rows = [_row("O1", "t1", superseded_by="t2", behavior=claim), _row("O2", "t2")]
    assert claim in _render(session, rows, compact=False)
    new = _render(session, rows)
    line = next(x for x in new.splitlines() if x.startswith("- O1 "))
    assert f"): {claim}; evidence test" in line and "the same scenario as" not in line


def test_a_superseded_row_whose_own_scenario_is_over_the_bound_is_not_shown_as_its_successors(tmp_path):
    """A successor with a shorter case list is shown; the predecessor's own
    scenario, with its long case list, is not, and keeps its refusal line."""
    session = _session(tmp_path)
    rows = [_row("O1", "t1", superseded_by="t2", cases=[f"tests/test_app.py::test_{'x' * 40}_{n}" for n in range(90)]),
            _row("O2", "t2", cases=["tests/test_app.py::test_add"])]
    new = _render(session, rows)
    o1 = new.split("- O1 ", 1)[1].split("\n- O", 1)[0]
    assert "the same scenario as" not in o1
    assert "over the 4,000-character bound for this prompt and not shown; leave this row unjudged" in o1
    assert "- O2 (t2; R1): scenario P1 (stated once below)" in new, "the successor itself is compact as before"


@pytest.mark.parametrize("name, rows", [
    ("no successor", [_row("O1", "t1", superseded_by="t9")]),
    ("two candidates", [_row("O1", "t1", superseded_by="t2"), _row("O2", "t2"), _row("O3", "t2", cases=["c"])]),
    ("a cycle", [_row("O1", "t1", superseded_by="t2"), _row("O2", "t2", superseded_by="t1")]),
    ("another revision", [_row("O1", "t1", superseded_by="t2"), _row("O2", "t2", requirements=(("R1", 2),))]),
    ("another modality", [_row("O1", "t1", superseded_by="t2"), _row("O2", "t2", modality="reading")]),
])
def test_an_unresolved_or_ambiguous_successor_keeps_the_full_row(tmp_path, name, rows):
    session = _session(tmp_path)
    new = _render(session, rows)
    line = next(x for x in new.splitlines() if x.startswith("- O1 "))
    assert "the same scenario as" not in line, name
    assert "superseded by task" in line, "its own reason stays"


def test_every_required_row_keeps_its_line_and_requirement_selection_is_unchanged(tmp_path):
    session = _session(tmp_path)
    rows = [_row("O1", "t1", superseded_by="t2"), _row("O2", "t2", scenario=_scenario(pid="P3")),
            _row("O3", "t2", scenario={}, behavior="legacy claim", modality="legacy", execution="missing")]
    required = session._audit_required_ids({"R1": "x"}, [], rows, None)
    new = _render(session, rows)
    for oid, kind in required:
        if kind == "row":
            assert f"- {oid} (" in new
    session._compact_audit_packet = False
    assert session._audit_required_ids({"R1": "x"}, [], rows, None) == required


# -- 3. review observations: one text once, every raiser kept, whitespace material -------------------

def _note(task, peer, text, *, requirements=("R1",), association="named", source="S1"):
    return dict(task=task, peer=peer, requirements=list(requirements), association=association, text=text,
                source=source)


def test_identical_observations_are_listed_once_with_every_review_that_raised_them(tmp_path):
    (tmp_path / "p").mkdir()
    session = _session(tmp_path, project=tmp_path / "p")
    session._source_fingerprint = lambda: "S2"
    warning = "the CLI exits 1 with a traceback when the file is missing"
    session.review_notes = [
        _note("t3", "openai:sol", warning, source="S1"),
        _note("t5", "anthropic:opus", warning, source="S2"),
        _note("t6", "openai:sol", warning, requirements=("R1", "R2"), association="unassigned", source="S2"),
        _note("t6", "openai:sol", "a  doubled space in `a  b` is quoted input"),
        _note("t7", "openai:sol", "a doubled space in `a b` is quoted input"),
    ]
    session._compact_audit_packet = False
    old = session._review_notes_block({"R1": "x"})
    session._compact_audit_packet = True
    new = session._review_notes_block({"R1": "x"})
    assert old.count(warning) == 3 and new.count(warning) == 1
    line = next(x for x in new.splitlines() if warning in x)
    assert ("t3 review by openai:sol, about R1, written about an earlier source (it may no longer hold)" in line
            and "t5 review by anthropic:opus, about R1, written about the current source" in line
            and "t6 review by openai:sol, naming no requirement (the task covered R1, R2; which one, if any, it "
                "concerns is uncertain), written about the current source" in line)
    assert "a  doubled space in `a  b` is quoted input" in new and "a doubled space in `a b` is quoted input" in new
    assert "None was marked blocking and none was rechecked." in new
    assert "not marked blocking and never rechecked" not in new, "the repeated explanation is in the header"
    assert len(new) < len(old)


# -- 4. the history names the rule to repair, for editing and reviewing seats only --------------------

def test_the_history_asks_for_the_goal_entitled_rule_only_where_work_is_done(tmp_path):
    session = _session(tmp_path)
    session.requirement_audits = [dict(auditor="openai:sol", source="S1", reply_artifact="a1", failed=False,
                                       verdicts={"R1": dict(met=False, why="w")}, stated=["R1"],
                                       stated_reasons={"R1": "fails; example: add(-1, 1)"}, revisions={}, bases={})]
    work = " ".join(session._audit_history(["R1"]).split())
    audit = " ".join(session._audit_history(["R1"], judge=True).split())
    rule = "identify the rule the goal entitles at that input layer"
    assert rule in work and "while every form the rule allows stays accepted" in work
    assert "it adds no requirement" in work
    assert rule not in audit


# -- 5. the audit still judges and routes as before ---------------------------------------------------

def test_the_audit_reads_the_compact_rows_and_an_adverse_verdict_still_routes(tmp_path, monkeypatch):
    from tests.lifecycle.test_cycle12_history import _repair
    replay, seen, audits = _repair(tmp_path, monkeypatch, "compact", second="R1: MET - tests/test_app.py::test_add")
    assert "(stated once below)" in audits[0].prompt
    assert replay.result.completed, replay.result.error
    replay, seen, audits = _repair(tmp_path, monkeypatch, "compact-adverse", max_tasks=3,
                                   second="R1: NOT MET - add(-1, -2) still returns 3 on the repaired source")
    assert H.result_json(replay)["requirements"]["status"]["R1"].startswith("NOT MET") and not replay.result.completed
    assert any("An independent audit found these requirements not met" in p for p in seen.prompts["orchestrator"])


# -- 6. the offline measurement tool ------------------------------------------------------------------

def _saved_run(tmp_path, *, checkpoint_source="S1", state=True):
    from quadratus import checkpoint
    session = _session(tmp_path)
    session.evidence_obligations = [_row("O1", "t1", superseded_by="t2"),
                                    _row("O2", "t2", scenario=_scenario(pid="P2"))]
    session.review_notes = [_note(f"t{n}", "openai:sol", "the same observation, raised again by every review")
                            for n in range(1, 7)]
    session.requirement_audits = [dict(auditor="openai:sol", source="S1", reply_artifact="a1", failed=False,
                                       verdicts={"R1": dict(met=False, why="w")}, stated=["R1"],
                                       stated_reasons={"R1": "w"}, revisions={"R1": 1}, bases={},
                                       required=["R1", "O2"])]
    run = tmp_path / "runs" / "20261109T000000Z-test"
    run.mkdir(parents=True)
    checkpoint.write(run, dict(version=checkpoint.VERSION, identities=dict(source=checkpoint_source),
                               state=checkpoint.capture_state(session) if state else {}, sealed=True))
    return run


def _digests(folder: Path) -> dict:
    return {str(p.relative_to(folder)): hashlib.sha256(p.read_bytes()).hexdigest()
            for p in sorted(folder.rglob("*")) if p.is_file()}


def test_the_tool_compares_both_renderers_on_one_saved_state_without_touching_the_run(tmp_path):
    from tools.audit_packet_size import measure
    run = _saved_run(tmp_path)
    before = _digests(run)
    report = measure(run, tmp_path / "out" / "report.json")
    assert _digests(run) == before, "the run folder is read only"
    saved = json.loads((tmp_path / "out" / "report.json").read_text())
    assert saved == report and report["units"].startswith("characters")
    assert report["sections"]["obligations"]["new_chars"] < report["sections"]["obligations"]["old_chars"]
    assert report["sections"]["review_observations"]["new_chars"] < report["sections"]["review_observations"]["old_chars"]
    assert report["required_obligation_ids"] == dict(checked=1, missing_old=[], missing_new=[])
    assert report["historical_prompt_reconstructed"] is False and report["state_match"]["exact"] is False
    assert "token" not in json.dumps(report["sections"])


def test_the_tool_reports_unlike_states_and_refuses_what_it_cannot_measure(tmp_path):
    from tools.audit_packet_size import measure
    run = _saved_run(tmp_path, checkpoint_source="S9")
    report = measure(run, tmp_path / "r.json")
    assert any("differs from the source the last audit judged" in m for m in report["state_match"]["mismatches"])
    with pytest.raises(SystemExit, match="outside the run folder"):
        measure(run, run / "report.json")
    with pytest.raises(SystemExit, match="carries no state"):
        measure(_saved_run(tmp_path / "empty", state=False), tmp_path / "e.json")

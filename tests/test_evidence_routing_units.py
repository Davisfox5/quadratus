"""Evidence route and modality, scenario correction atomicity and debt
association (Davis, 2026-11-06, batch 6674444836 items A, B and C), at the
units: no controller run.

turn-tracking-01: O10, a codex revision's "Read tickets/store.py
tests/test_dependencies_store.py", was NOT RUN because the editing prompt
named claude's reading tools; the harness held it as runner-less execution
debt on nine requirements. O11's correction changed the expectation and left
the state contradicting it. O34, an unexecuted interpreter fallback, held
R21, R22 and R24 though it served one of them.
"""
import inspect
import json

import pytest

from quadratus import checkpoint, runtime
from quadratus.artifacts import ArtifactStore
from quadratus.session import Session, SessionConfig, TaskSpec
from tools import truth_check

# -- A1. reading is named in the seat's own vendor's tools -------------------------------------------


def test_each_vendor_is_told_its_own_reading_tools_and_reading_is_never_not_run():
    sol, opus, grok = (runtime.inspection_tools(k) for k in ("openai:sol", "claude:opus", "grok:worker"))
    assert "shell commands (ls, cat, sed -n, head, rg, grep)" in sol and "Read, Grep and Glob" not in sol
    assert "Read, Grep and Glob" in opus
    assert "Read, Grep and Glob" not in grok and "your own file-reading and search tools" in grok
    for text in (sol, opus, grok):
        assert "never a NOT RUN item" in text
    source = inspect.getsource(runtime)
    assert "inspection_tools(model_key)" in source and '"Use Read, Grep and Glob for inspection. ' not in source
    assert "' | for: R<n>, R<m>'" in source, "the association grammar the parser reads is stated"


# -- A2. a reading NOT RUN is an inspection, attested by a review it was shown --------------------------

def _session(tmp_path):
    project = tmp_path / "project"
    (project / "tests").mkdir(parents=True)
    (project / "app.py").write_text("def add(a, b):\n    return a + b\n")
    (project / "tests" / "test_app.py").write_text("def test_add():\n    pass\n")
    session = Session("goal", ArtifactStore(tmp_path / "artifacts"), lambda *a, **k: "",
                      config=SessionConfig(project=str(project), allow_writes=True))
    spec = TaskSpec(task_id="t1", description="fix add")
    session._active_spec, session._current_covers = spec, ["R1", "R2"]
    return session, spec, project


def test_a_reading_not_run_is_an_inspection_gap_never_execution_debt(tmp_path):
    session, spec, project = _session(tmp_path)
    session._record_not_run("openai:sol", "revision",
                            "NOT RUN: Read app.py tests/test_app.py - Read/Grep/Glob unavailable\nCHANGED: []")
    assert session.unexecuted_acceptance == [] and not session._standing_not_run()
    (gap,) = session.inspection_gaps
    assert gap["files"] == ["app.py", "tests/test_app.py"] and gap["requirements"] == ["R1", "R2"]
    assert gap["attested"] is None and gap["author"] == "openai:sol"
    route = session._evidence_route("R1")
    assert route["kind"] == "inspection" and route["challengeable"] is False and "never a test run" in route["route"]
    assert "could not read app.py, tests/test_app.py" in session._not_run_block()
    # Reported twice before any review: one gap.
    session._record_not_run("openai:sol", "revision", "NOT RUN: Read app.py tests/test_app.py - again")
    assert len(session.inspection_gaps) == 1


@pytest.mark.parametrize("line", [
    "NOT RUN: Read missing.py - not there",            # names no file that exists: not a reading of the source
    "NOT RUN: python3 -m pytest tests/test_app.py - denied",
    "NOT RUN: tests/test_app.py::test_add - socket denied",
])
def test_anything_else_stays_execution_debt(tmp_path, line):
    session, _, _ = _session(tmp_path)
    session._record_not_run("openai:sol", "lead", line)
    assert session.inspection_gaps == [] and len(session.unexecuted_acceptance) == 1


def test_only_a_completed_review_shown_the_gap_attests_it_and_a_changed_file_reopens_it(tmp_path):
    session, spec, project = _session(tmp_path)
    session._record_not_run("openai:sol", "revision", "NOT RUN: Read app.py - unavailable")
    session._attest_inspections("t1", "claude:opus", "review")
    assert session._open_inspections(), "a review never shown the gap attests nothing"
    note = session._inspection_note(spec)
    assert "could not read these files" in note and "t1: app.py" in note
    session._attest_inspections("t1", "claude:opus", "review")
    assert session._open_inspections() == [] and session._inspection_note(spec) == ""
    attested = session.inspection_gaps[0]["attested"]
    assert attested["by"] == "claude:opus" and set(attested["files"]) == {"app.py"}
    (project / "app.py").write_text("def add(a, b):\n    return b + a\n")
    assert session._open_inspections(), "the attestation is bound to the bytes it read"
    assert session._evidence_route("R2")["kind"] == "inspection"


def test_a_review_only_task_is_shown_every_open_gap(tmp_path):
    session, spec, _ = _session(tmp_path)
    session._record_not_run("openai:sol", "revision", "NOT RUN: Read app.py - unavailable")
    from quadratus import session as S
    verify = TaskSpec(task_id="t2", description="verify")
    original = S.is_review_only
    try:
        S.is_review_only = lambda s: s is verify
        assert "t1: app.py" in session._inspection_note(verify)
    finally:
        S.is_review_only = original
    session._attest_inspections("t2", "openai:sol", "review")
    assert session._open_inspections() == []


def test_the_gaps_are_carried_and_judged_outside_the_engine(tmp_path):
    assert "inspection_gaps" in checkpoint._CARRIED
    session, spec, project = _session(tmp_path)
    session._record_not_run("openai:sol", "revision", "NOT RUN: Read app.py - unavailable")
    record = dict(inspection_gaps=json.loads(json.dumps(session.inspection_gaps)),
                  source_fingerprint=session._source_fingerprint())
    assert truth_check._inspection_problems(record) == ["task t1: reading of app.py is not attested by a completed review"]
    session._inspection_note(spec)
    session._attest_inspections("t1", "claude:opus", "review")
    record["inspection_gaps"] = json.loads(json.dumps(session.inspection_gaps))
    assert truth_check._inspection_problems(record) == []
    assert truth_check._inspection_problems(record, project) == []
    assert truth_check._inspection_problems(dict(record, source_fingerprint="other")) == [
        "task t1: reading of app.py was attested on another source than the final one"]
    (project / "app.py").write_text("changed\n")
    assert truth_check._inspection_problems(record, project) == [
        "task t1: reading of app.py: app.py is not the file the review attested"]


# -- C. a NOT RUN item holds the requirement it serves ---------------------------------------------------

def _rows(session):
    session.evidence_obligations = [
        dict(oid="O1", task="t1", kind="acceptance", proof_id="P1", requirements=[("R1", 1)],
             location="tests/test_a.py", cases=["tests/test_a.py::test_x"]),
        dict(oid="O2", task="t1", kind="acceptance", proof_id="P2", requirements=[("R2", 1)],
             location="README.md", cases=[]),
    ]


@pytest.mark.parametrize("entry, declared, held, how", [
    (dict(item="P1", binding=dict(kind="scenario", scenario="P1")), (), ["R1"], "bound: scenario P1"),
    (dict(item="tests/test_a.py::test_x", binding=dict(kind="case"), cases=["tests/test_a.py::test_x"]), (),
     ["R1"], "bound: the scenarios whose cases it binds"),
    (dict(item="python3.9 tests/test_a.py", binding=dict(kind="paths")), (), ["R1"],
     "bound: the scenarios whose location it names"),
    (dict(item="the python3.9 fallback in helper.py", binding=dict(kind="none")), ("R3",), ["R3"],
     "declared by openai:sol (R3)"),
    (dict(item="the python3.9 fallback in helper.py", binding=dict(kind="none")), ("R3", "R9"), ["R1", "R2", "R3"],
     "unknown"),
    (dict(item="the python3.9 fallback in helper.py", binding=dict(kind="none")), (), ["R1", "R2", "R3"], "unknown"),
], ids=["scenario", "case", "location", "declared", "declared-outside-covers", "unknown"])
def test_the_item_holds_what_it_is_bound_or_declared_to_serve_else_every_cover(tmp_path, entry, declared, held, how):
    session, _, _ = _session(tmp_path)
    _rows(session)
    entry = dict(entry, task="t1", author="openai:sol", requirements=["R1", "R2", "R3"])
    session._associate_not_run(entry, declared)
    assert entry["requirements"] == held and entry["requirements_covered"] == ["R1", "R2", "R3"]
    assert entry["association"].startswith(how), entry["association"]
    if how == "unknown":
        assert "every covered requirement stays held until it runs" in entry["association"]


def test_a_declared_association_is_parsed_off_the_reason_and_shown_to_the_audit(tmp_path):
    session, _, _ = _session(tmp_path)
    session._current_covers = ["R21", "R22", "R24"]
    session._record_not_run("openai:sol", "lead", "NOT RUN: the python3.9 fallback branch - no 3.9 here | for: R24")
    (entry,) = session.unexecuted_acceptance
    assert entry["why"] == "no 3.9 here" and entry["requirements"] == ["R24"]
    assert entry["requirements_covered"] == ["R21", "R22", "R24"]
    assert session._evidence_route("R21")["kind"] != "execution debt"
    assert session._evidence_route("R24")["kind"] == "execution debt"
    assert "(holds R24; declared by openai:sol (R24))" in session._not_run_block()


# -- B. a scenario correction is one revision of the whole scenario --------------------------------------

def _correcting(tmp_path, reply):
    from quadratus.scope import TaskScope, read_proofs
    session = Session("goal", ArtifactStore(tmp_path / "artifacts"), lambda *a, **k: reply,
                      config=SessionConfig())
    proofs = read_proofs([dict(id="P1", requirements=["R1"], operation="archive item 1",
                               state="item 1 has an open blocker", expect="the archive succeeds",
                               location="tests/test_app.py")])
    spec = TaskSpec(task_id="t1", description="work",
                    scope=TaskScope(permitted_paths=["app.py"], intended_result="x", acceptance=["x"],
                                    max_lines=20, proofs=proofs))
    record = dict(fid="RFT1-1", author="claude:opus", typed=dict(proof_id="P1", reason="state and expect disagree",
                                                                  field="expect", proposal="the archive is refused"))
    task = type("T", (), {"record": lambda *a, **k: None, "keep": lambda *a, **k: None})()
    session._scenario_corrected = set()
    out = session._correct_scenarios(spec, task, [record], {"claude:opus": "Reviewer A"})
    return session, dict(out.scope.proofs[0])


def test_several_fields_of_one_scenario_are_applied_together_with_their_lineage(tmp_path):
    session, proof = _correcting(tmp_path, "SCENARIO P1: state: item 1 has no open blocker | because: x\n"
                                           "SCENARIO P1: expect: the archive succeeds and is stored")
    assert proof["state"] == "item 1 has no open blocker" and proof["expect"] == "the archive succeeds and is stored"
    (entry,) = session.scenario_corrections
    assert entry["status"] == "corrected" and entry["field"] == "state, expect"
    assert entry["fields"]["state"] == dict(before="item 1 has an open blocker",
                                            after="item 1 has no open blocker")
    assert entry["fields"]["expect"]["before"] == "the archive succeeds"


@pytest.mark.parametrize("reply", [
    "SCENARIO P1: state: a\nSCENARIO P1: state: b",
    "SCENARIO P1: state: a\nSCENARIO P1: KEEP | because: fine",
])
def test_a_field_given_twice_or_beside_keep_is_ambiguous_and_nothing_moves(tmp_path, reply):
    session, proof = _correcting(tmp_path, reply)
    assert proof["state"] == "item 1 has an open blocker" and proof["expect"] == "the archive succeeds"
    assert session.scenario_corrections[0]["status"].startswith("refused: ambiguous reply")


def test_one_bad_field_refuses_the_whole_revision(tmp_path):
    session, proof = _correcting(tmp_path, "SCENARIO P1: state: item 1 has an open blocker\n"
                                           "SCENARIO P1: expect: the archive is refused")
    assert proof["expect"] == "the archive succeeds", "no partial correction"
    assert session.scenario_corrections[0]["status"].startswith("refused: state: ")

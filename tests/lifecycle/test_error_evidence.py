"""Error and refusal requirements are accepted only on executed evidence
(Davis, 2026-10-16).

R22 closed every task with its error contract judged MET by reading, and
the delivered CLI still let one rejected input leave as a traceback. Prompt
wording already asked for every error path to be traced
(``_ERROR_PATH_RULE``) and for a scratch demonstration (``_PROBE_RULE``);
nothing held a seat to it. Now a requirement whose own text is about errors,
refusals or invalid input is accepted only when a test case that exercises
it was executed and passed by a complete approved check on the current
source, or, in a task review, by that review's own scratch demonstration.
A MET by reading, a stale run, a skipped case or a citation of nothing is
held: NOT MET at the audit, UNVERIFIED in a review. Whether the executed
test establishes the clause stays the seat's own judgment.

Whole-run replays below the vendor CLI with the real pytest gate; the
project is a two-function app, no fixture of any live job.
"""

import json

import pytest

from quadratus.session import _error_clause
from tests.lifecycle import harness as H
from tests.lifecycle.test_lifecycle_matrix import Script
from tests.lifecycle.test_step2_recovery import _simple


def _scoped(paths, text, covers):
    scope = json.dumps({"permitted_paths": paths, "intended_result": "add refuses non-numbers",
                        "acceptance": ["add refuses non-numbers"], "max_lines": 30,
                        "proofs": H.proofs_for(covers, next((p for p in paths if "test" in p), paths[0]),
                                               operation="add('x', 1)", state="nothing stored",
                                               expect="TypeError is raised")})
    return f"KIND: backend simple\nSCOPE: {scope}\n{text}\nCOVERS: {covers}"

R1 = "add(a, b) raises TypeError when either argument is not a number"
R2 = "README documents add"
PLAN = ("REQUIREMENTS:\nR1: " + R1 + "\nR2: " + R2 + "\n"
        + _scoped(["app.py", "tests/test_app.py"], "Implement add in app.py with its refusal and a test.", "R1"))
PLAN_2 = _simple("README.md", "Document add in README.md.", "R2")

FAULTY = "def add(a, b):\n    return a + b\n"
SOUND = ("def add(a, b):\n"
         "    if not all(isinstance(v, (int, float)) and not isinstance(v, bool) for v in (a, b)):\n"
         "        raise TypeError('add takes numbers')\n"
         "    return a + b\n")
HAPPY = "from app import add\n\n\ndef test_add():\n    assert add(1, 2) == 3\n"
NEGATIVE = (HAPPY + "\n\ndef test_add_refuses_text():\n    import pytest\n"
            "    with pytest.raises(TypeError):\n        add('1', 2)\n")
SKIPPED = (HAPPY + "\n\nimport pytest\n\n\n@pytest.mark.skip(reason='later')\n"
           "def test_add_refuses_text():\n    with pytest.raises(TypeError):\n        add('1', 2)\n")
FILES = {"app.py": "def add(a, b):\n    return 0\n", "tests/test_app.py": HAPPY, "README.md": "# app\n"}
#: The approved check printing each case's file-qualified identity: only a
#: case record names an executed case exactly (root review of 17a96d3).
VERBOSE = H.GATE.replace(" -q ", " -v ")


def _run(tmp_path, monkeypatch, *, source, tests, audit, review="NO FINDINGS\nR1: MET - app.py",
         after=None, check=VERBOSE):
    def orchestrator(call, replay):
        n = len(replay.of("orchestrator"))
        return {1: PLAN, 2: PLAN_2}.get(n, "DONE")

    def lead(call, replay):
        if call.task == "t1":
            H.write(call, {"app.py": source, "tests/test_app.py": tests})
            changed = ["app.py"] + (["tests/test_app.py"] if tests != HAPPY else [])
            return "Implemented add with its refusal.\nCHANGED: " + json.dumps(changed)
        H.write(call, {"README.md": "# app\n\n`add(a, b)` returns the sum.\n", **(after or {})})
        return 'Documented add.\nCHANGED: ["README.md"' + "".join(f', "{p}"' for p in after or {}) + "]"

    def collaborator(call, replay):
        return review if call.task == "t1" else "NO FINDINGS\nR2: MET - README.md"

    script = Script(orchestrator=orchestrator, lead=lead, collaborator=collaborator,
                    auditor=lambda call, replay: audit,
                    **{"requirements-review": lambda call, replay: "COMPLETE",
                       "revision": lambda call, replay: "Nothing to change.\nCHANGED: []"})
    return H.run(tmp_path, monkeypatch, script, files=FILES, max_tasks=2, record_complete=False, check=check)


def _final(replay):
    audits = H.result_json(replay)["requirements"]["audits"]
    return audits[-1]


def test_the_classifier_reads_error_and_refusal_vocabulary_only():
    assert _error_clause(R1)
    assert _error_clause("Invalid inputs return HTTP 400; missing records HTTP 404")
    assert _error_clause("refusal is JSON stderr with a nonzero exit")
    assert _error_clause("All error responses are JSON with an error member")
    assert not _error_clause(R2) and not _error_clause("add(a, b) returns the sum")
    # The absence of runtime errors is not a refusal contract.
    assert not _error_clause("the list logs no console errors while typing")


@pytest.mark.requirements_ledger
def test_old_shape_a_met_by_reading_on_faulty_error_handling_is_not_met(tmp_path, monkeypatch):
    """Faulty: add concatenates strings and the happy-path test passes. The
    auditor reads app.py and calls R1 MET. Before this batch that stood; now
    no executed test exercises the refusal, so R1 is NOT MET and the run is
    not complete. R2, not an error clause, is untouched."""
    replay = _run(tmp_path, monkeypatch, source=FAULTY, tests=HAPPY,
                  audit="R1: MET - app.py raises TypeError for text\nR2: MET - README.md")
    audit = _final(replay)
    assert audit["verdicts"]["R1"]["met"] is False and "error or refusal requirement" in audit["verdicts"]["R1"]["why"]
    assert audit["verdicts"]["R2"]["met"] is True
    assert [entry["requirement"] for entry in audit["lowered"]] == ["R1"]
    assert not replay.result.completed


@pytest.mark.requirements_ledger
def test_a_sound_refusal_with_an_executed_negative_test_is_accepted(tmp_path, monkeypatch):
    replay = _run(tmp_path, monkeypatch, source=SOUND, tests=NEGATIVE,
                  review="NO FINDINGS\nR1: MET - tests/test_app.py::test_add_refuses_text",
                  audit="R1: MET - tests/test_app.py::test_add_refuses_text\nR2: MET - README.md")
    audit = _final(replay)
    assert audit["verdicts"]["R1"]["met"] is True and audit["lowered"] == []
    assert replay.result.completed, replay.result.error
    # The review cited the executed case: counted, no UNVERIFIED line, no revision.
    assert not replay.of("revision")


@pytest.mark.requirements_ledger
@pytest.mark.parametrize("case", ["missing", "unsupported", "skipped", "stale"])
def test_missing_unsupported_skipped_or_stale_evidence_stays_unverified(tmp_path, monkeypatch, case):
    """missing: no test cited; unsupported: a test name that does not exist;
    skipped: the negative case exists but the run skipped it (not complete);
    stale: the cited case ran, then the source changed with no check after
    it (a later task edits app.py outside any gate run on it)."""
    cite = {"missing": "app.py", "unsupported": "tests/test_app.py::test_nothing_like_this",
            "skipped": "tests/test_app.py::test_add_refuses_text",
            "stale": "tests/test_app.py::test_add_refuses_text"}[case]
    tests = SKIPPED if case == "skipped" else NEGATIVE
    replay = _run(tmp_path, monkeypatch, source=SOUND, tests=tests,
                  audit=f"R1: MET - {cite}\nR2: MET - README.md")
    if case == "stale":
        # Stale by construction: the cited run is on an earlier source.
        from quadratus.artifacts import ArtifactStore
        from quadratus.session import Session, SessionConfig
        session = Session("goal", ArtifactStore(tmp_path / "s"), lambda *a, **k: "",
                          config=SessionConfig(project=replay.project, requirements_ledger=True))
        result = H.result_json(replay)
        session.checks = [dict(c, source="an-earlier-source") for c in result["checks"]]
        assert session._executed_cases([cite]) == []
        session.checks = [dict(c, source=session._source_fingerprint()) for c in result["checks"]]
        assert session._executed_cases([cite]) == [cite], "the same run on the current source counts"
        return
    audit = _final(replay)
    assert audit["verdicts"]["R1"]["met"] is False, audit
    assert not replay.result.completed


@pytest.mark.requirements_ledger
def test_a_review_met_by_reading_is_unverified_and_answered_never_an_approval(tmp_path, monkeypatch):
    """The task review says MET by reading app.py. The lead sees an
    UNVERIFIED line in its place and answers it in a revision; the gap is a
    non-terminal fact and an observation the final audit sees. No BLOCKING
    finding is invented and no recheck is bought."""
    replay = _run(tmp_path, monkeypatch, source=SOUND, tests=NEGATIVE,
                  audit="R1: MET - tests/test_app.py::test_add_refuses_text\nR2: MET - README.md")
    revisions = [c for c in replay.of("revision") if c.task == "t1"]
    assert len(revisions) == 1 and "R1: UNVERIFIED - MET claimed without executed negative-path" in \
        revisions[0].prompt
    assert not [c for c in replay.of("recheck") if c.task == "t1"]
    auditor = replay.of("auditor")[-1]
    assert "marked R1 MET without executed negative-path evidence" in auditor.prompt
    facts = [f for t in replay.workflow["tasks"] if t["task_id"] == "t1" for f in t["facts"]]
    assert any("without executed negative-path evidence" in f["detail"] and not f["terminal"] for f in facts)
    # The audit's own rule decides acceptance: the executed case is cited there.
    assert _final(replay)["verdicts"]["R1"]["met"] is True


@pytest.mark.requirements_ledger
def test_no_command_or_private_path_reaches_a_seat_through_the_rule(tmp_path, monkeypatch):
    replay = _run(tmp_path, monkeypatch, source=FAULTY, tests=HAPPY,
                  audit="R1: MET - app.py\nR2: MET - README.md")
    # The auditor never sees a command (a review seat is shown the one
    # approved scratch command, by design); the held reasons name none.
    for call in replay.of("auditor"):
        assert "--quadratus-report" not in call.prompt and "-m pytest" not in call.prompt
    lowered = json.dumps(_final(replay)["lowered"])
    assert "pytest" not in lowered and str(replay.project) not in lowered


@pytest.mark.requirements_ledger
@pytest.mark.parametrize("declared", [True, False], ids=["own-report", "no-report"])
def test_a_quiet_run_credits_only_the_cases_its_own_report_names(tmp_path, monkeypatch, declared):
    """The same sound work and executed negative test under ``pytest -q``,
    which prints no case lines. With the harness report declared, the
    producer names each case it counted passed, so the cited case is shown
    exactly and R1 is MET (root review of 5ca254c: the production gates are
    quiet and declare the report). Without the report nothing names the
    case: unknown identity credits nothing and R1 stays NOT MET."""
    check = H.GATE if declared else H.GATE.replace(" --quadratus-report={report}", "")
    replay = _run(tmp_path, monkeypatch, source=SOUND, tests=NEGATIVE, check=check,
                  review="NO FINDINGS\nR1: MET - tests/test_app.py::test_add_refuses_text",
                  audit="R1: MET - tests/test_app.py::test_add_refuses_text\nR2: MET - README.md")
    assert _final(replay)["verdicts"]["R1"]["met"] is declared
    assert replay.result.completed is declared, replay.result.error

"""The merge of 2026-11-11, gate group: one rule from each line, held together.

The review batch's rule: the record of an observed check keeps counts and no
case names, so result.json stays inside the monitor's read bound. The
development line's rule: a proof row settles its bound case on the harness's
own run of the exact source a reviewer judges. The names the second rule
reads are kept in the session's memory beside the record and written
nowhere."""
import json
from contextlib import contextmanager
from types import SimpleNamespace

import pytest

from quadratus import session
from quadratus.integration import GateCommand, GateReceipt, GateResult, GateSuite
from tests.lifecycle import harness as H
from tests.lifecycle.test_lifecycle_matrix import FILES
from tests.lifecycle.test_proof_binding import CASE, _acceptance, _at
from tests.lifecycle.test_workflow_repair import Seen, _rows, _script


@pytest.mark.requirements_ledger
def test_a_proof_row_settles_on_an_observation_whose_record_names_no_case(tmp_path, monkeypatch):
    seen = Seen()
    replay = H.run(_at(tmp_path, "both"), monkeypatch, _script(seen), files=FILES, max_tasks=1,
                   record_complete=False)
    # The reviewer was shown the harness's run of these bytes as the row's receipt.
    assert f"binding bound: {CASE}; execution ran (receipt observed-1)" in _rows(seen.prompts["collaborator"][0])
    assert _acceptance(replay)[0]["receipts"] == ["observed-1"]
    # The record of that run kept its counts and not one case name.
    (observed,) = H.result_json(replay)["review_checks"]
    reports = [r["report"] for r in observed["receipts"]] or [observed["report"]]
    assert observed["passed"] and CASE not in json.dumps(observed)
    assert all("passed_cases" not in r and r["passed_cases_omitted"] == r["counts"]["passed"] >= 1 for r in reports)


def _report(names):
    return dict(state="parsed", producer="quadratus-pytest/5", exitstatus=0, collected=len(names), truncated=False,
                collect_errors=0, counts=dict(passed=len(names), failed=0, errors=0, skipped=0),
                passed_cases=list(names), passed_truncated=False)


def _observer(project, names, source):
    """A stand-in session for ``Session._observe_checks``: one gate whose run
    reports ``names`` as passed, and a source the test moves by hand."""
    gate = GateSuite([GateCommand(id="check", argv=("pytest", "-q"))], cwd=project)
    gate.run = lambda: GateResult(True, "gate suite", 0, "check: passed", (GateReceipt(
        id="check", status="passed", reason="exit 0", required=True, command="pytest -q", returncode=0,
        output=f"{len(names)} passed in 0.1s", tests=len(names), report=_report(names)),))

    @contextmanager
    def operation(*args, **kwargs):
        yield {}
    seqs = iter(range(1, 100))
    return SimpleNamespace(
        config=SimpleNamespace(integration_gate=gate, project_excludes=()), project=project, review_checks=[],
        checks=[], _observed_names=[], _required=lambda *a, **k: True, _current_gate=lambda: gate,
        _source_fingerprint=lambda: source[0], _verify_dependencies=lambda *a, **k: None,
        _activity=lambda *a, **k: None, _note=lambda *a, **k: None, _operation=operation,
        _note_check_wall=lambda *a, **k: None, _next_run_seq=lambda: next(seqs),
        _execution_identity=lambda gate: dict(gate=session._gate_identity(gate), source=source[0], requirements={},
                                              environment="env"),
        _consumed_identity=lambda frozen: dict(gate=frozen["gate"], source=frozen["source"], requirements={},
                                               environment="env"))


def test_the_names_are_given_only_to_the_record_this_session_made_of_the_current_bytes(tmp_path):
    project = tmp_path / "project"
    project.mkdir()
    (project / "app.py").write_text("X = 1\n")
    names = [f"tests/test_a.py::test_case[{n}]" for n in range(5)]
    source = ["first"]
    stub = _observer(project, names, source)
    session.Session._observe_checks(stub, SimpleNamespace(task_id="t1"), "review")
    (first,) = stub.review_checks
    assert names[3] not in json.dumps(stub.review_checks) and first["receipts"][0]["report"]["passed_cases_omitted"] == 5
    named = session.Session._observation_named(stub, first)
    assert named["receipts"][0]["report"]["passed_cases"] == names and named["source"] == "first"
    assert "passed_cases" not in first["receipts"][0]["report"], "the record itself is never given the names back"
    # What a resumed run holds is an equal record, not this one: it names nothing.
    restored = json.loads(json.dumps(first))
    assert restored == first and session.Session._observation_named(stub, restored) is restored
    # The bytes move and are observed again: the earlier run's names are let go.
    source[0] = "second"
    session.Session._observe_checks(stub, SimpleNamespace(task_id="t1"), "recheck")
    second = stub.review_checks[1]
    assert [kept[0] for kept in stub._observed_names] == [second]
    assert session.Session._observation_named(stub, first) is first
    assert session.Session._observation_named(stub, second)["receipts"][0]["report"]["passed_cases"] == names

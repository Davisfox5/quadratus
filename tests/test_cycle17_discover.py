"""Cycle17 (PR73 6740463929): an exact ``unittest discover`` command receipt
is a command-level citation, and nothing more.

cycle16 R21 named ``python3 -m unittest discover -s tests``; the frozen gate
ran exactly that and passed 66 cases; the citation refused it as "it ran a
selection, not the whole command" because ``runner_of`` names no runner for
``discover`` and ``config_selection`` records that as unknown selection. The
planner then asked Davis a configuration question the record answered.

Pinned here: a strict discover grammar; a command-level predicate that drops
only the harness's own unknown-runner diagnostic for that grammar and keeps
every other reason; ``filtered``, ``files`` and every file/case consumer
unchanged; ``_command_key`` keeps a discovery pattern ``-p`` (only pytest's
``-p no:cacheprovider`` is plumbing); the refusal says which fact holds."""
import pytest

from quadratus import session as S
from quadratus.integration import (
    RUNNER_UNKNOWN,
    config_selection,
    operand_paths,
    runner_of,
    unittest_discover_command,
)
from quadratus.session import Session
from tests.test_evidence_lifecycle_units import _gate, _project, _receipt_check, _session

DISCOVER = ["python3", "-m", "unittest", "discover", "-s", "tests"]
REQ = "`python3 -m unittest discover -s tests` passes"


# -- 1. the grammar ----------------------------------------------------------------------------

@pytest.mark.parametrize("argv", [
    DISCOVER,
    ["python3", "-m", "unittest", "discover"],
    ["python", "-m", "unittest", "discover", "-v", "--start-directory=tests", "-p", "test*.py"],
    ["/usr/bin/python3.11", "-m", "unittest", "discover", "--pattern", "test_*.py", "-s", "tests", "-b", "-f"],
    [*DISCOVER, "--quadratus-report={report}"],
    ["python3", "-m", "unittest", "discover", "-t", ".", "-s", "tests"],
], ids=["plain", "defaults", "long-equals", "interpreter-path", "report-token", "top-level"])
def test_the_strict_discover_shape(argv):
    assert unittest_discover_command(argv)


@pytest.mark.parametrize("argv", [
    [*DISCOVER, "-k", "add"],
    ["python3", "-m", "unittest", "discover", "tests"],
    ["python3", "-m", "unittest", "discover", "-stests"],
    ["python3", "-m", "unittest", "discover", "-s"],
    ["python3", "-m", "unittest", "discover", "-s", "-v"],
    ["python3", "-m", "unittest", "discover", "--pattern="],
    ["python3", "-m", "unittest", "discover", "-p=test*.py"],
    ["python3", "-m", "unittest", "discover", "-s", "a", "--start-directory", "b"],
    [*DISCOVER, "--locals"],
    ["bash", "-c", "python3 -m unittest discover -s tests"],
    ["pythonw", "-m", "unittest", "discover"],
    ["python3", "-c", "import unittest", "discover"],
    ["python3", "-m", "unittest", "tests.test_app"],
    ["python3", "-m", "pytest", "-q"],
], ids=["k", "positional", "attached-short", "missing-value", "option-as-value", "empty-equals", "short-equals",
        "repeated", "unknown-option", "wrapper", "interpreter-name", "code", "module-form", "pytest"])
def test_everything_else_is_not_the_shape(argv):
    assert not unittest_discover_command(argv)


def test_the_shape_grants_no_runner_file_or_case(tmp_path):
    """The plain discover form names no file at all."""
    root = _project(tmp_path)
    assert runner_of(DISCOVER, root, ".") is None and operand_paths(DISCOVER, root, ".") == []
    assert config_selection(DISCOVER, root, root) == RUNNER_UNKNOWN


# -- 2. exact identity: a discovery pattern is part of the command ------------------------------

def test_a_discovery_pattern_is_never_stripped_and_pytest_plumbing_still_is():
    key = Session._command_key
    plain = key(["python3", "-m", "unittest", "discover", "-s", "tests"])
    collision = key(["python3", "-m", "unittest", "discover", "-s", "tests", "-p", "no:cacheprovider"])
    attached = key(["python3", "-m", "unittest", "discover", "-s", "tests", "-pno:cacheprovider"])
    assert collision != plain and attached != plain, "a unittest -p is the discovery pattern"
    assert key([*DISCOVER, "-p", "test_a*.py"]) != key([*DISCOVER, "-p", "test_b*.py"])
    assert key(["python3", "-m", "pytest", "-q", "-p", "no:cacheprovider"]) == key(["python3", "-m", "pytest", "-q"])
    assert key(["pytest", "-q", "-pno:cacheprovider"]) == key(["python3", "-m", "pytest", "-q"])
    assert key(["python3", "-m", "pytest", "-q", "-p", "xdist"]) != key(["python3", "-m", "pytest", "-q"])
    assert key([*DISCOVER, "--quadratus-report={report}"]) == plain


# -- 3. the command-level predicate beside the file-level one ----------------------------------

def _run(tmp_path, command, selection="", output="1 passed in 0.1s"):
    root = _project(tmp_path)
    session = _session(tmp_path, project=root, gate=_gate(root))
    check = _receipt_check(session, command, selection=selection, output=output)
    check["receipts"][0]["output"] = output
    (run,) = S._check_runs(check, root)
    return run, root


@pytest.mark.parametrize("command, selection, output, reasons", [
    ("python3 -m unittest discover -s tests", RUNNER_UNKNOWN, "1 passed in 0.1s", []),
    ("python3 -m unittest discover -s tests", f"{RUNNER_UNKNOWN}; NODE_OPTIONS=--test-name-pattern=x",
     "1 passed in 0.1s", ["external"]),
    ("python3 -m unittest discover -s tests -k add", RUNNER_UNKNOWN, "1 passed in 0.1s", ["argv", "runner"]),
    ("python3 -m unittest discover -t . -s tests", RUNNER_UNKNOWN, "1 passed in 0.1s", ["argv"]),
    ("python3 -m unittest discover tests", RUNNER_UNKNOWN, "1 passed in 0.1s", ["runner"]),
    ("python3 -m unittest discover -s tests", RUNNER_UNKNOWN, "Ran 1 test, 1 deselected", ["deselected"]),
    ("bash run_tests.sh", RUNNER_UNKNOWN, "1 passed in 0.1s", ["runner"]),
], ids=["discover", "mixed-external", "k", "top-level-false-negative", "positional", "deselected", "wrapper"])
def test_only_the_unknown_runner_diagnostic_is_dropped_and_only_for_the_shape(tmp_path, command, selection, output,
                                                                            reasons):
    run, root = _run(tmp_path, command, selection, output)
    import shlex
    assert run["filtered"] is True, "the file-level reading is unchanged"
    assert run["files"] == set(operand_paths(shlex.split(command), root, ".")), "the same file reading as before"
    assert run["command_reasons"] == reasons and run["command_filtered"] == bool(reasons)


@pytest.mark.parametrize("command, selection", [
    ("python3 -m pytest -q tests/test_app.py", ""), ("python3 -m pytest -q tests/test_app.py", "-k add"),
    ("python3 -m unittest tests.test_app", ""),
])
def test_pytest_and_plain_module_rows_read_the_same_at_both_levels(tmp_path, command, selection):
    run, _root = _run(tmp_path, command, selection)
    assert run["command_filtered"] == run["filtered"]


# -- 4. the citation --------------------------------------------------------------------------

def _citation(tmp_path, command, *, requirement=REQ, legacy=False, monkeypatch=None, **kw):
    root = _project(tmp_path)
    session = _session(tmp_path, project=root, gate=_gate(root))
    session.memory.ledger.requirements["R1"] = requirement
    session.checks.append(_receipt_check(session, command, **kw))
    if legacy:
        original = S._check_runs
        monkeypatch.setattr(S, "_check_runs", lambda check, root=None: [
            {k: v for k, v in run.items() if k not in ("command_filtered", "command_reasons")}
            for run in original(check, root)])
    return session._command_receipt_citation("R1")


def test_the_exact_discover_receipt_is_cited_in_the_requirements_own_spelling(tmp_path):
    receipt, problem = _citation(tmp_path, "/private/runtime/bin/python3.11 -m unittest discover -s tests "
                                           "--quadratus-report={report}", selection=RUNNER_UNKNOWN)
    assert problem == "" and receipt["command"] == "python3 -m unittest discover -s tests"
    assert "/private" not in str(receipt)


@pytest.mark.parametrize("command, kw, expected", [
    ("python3 -m unittest discover -s tests", dict(selection=f"{RUNNER_UNKNOWN}; PYTEST_ADDOPTS=-k x"),
     "a test selection from the environment or configuration applies to it"),
    ("python3 -m unittest discover -s tests", dict(selection=RUNNER_UNKNOWN, status="failed"), "its run failed"),
    ("python3 -m unittest discover -s tests", dict(selection=RUNNER_UNKNOWN, source="0" * 24),
     "it ran on another source"),
    ("python3 -m unittest discover -s tests", dict(selection=RUNNER_UNKNOWN, skipped=1, output="1 passed, 1 skipped"),
     "it skipped 1 case(s)"),
    ("python3 -m unittest discover -s tests", dict(selection=RUNNER_UNKNOWN, required=False),
     "it is an optional command"),
    ("python3 -m unittest discover -s tests -k add", dict(selection=RUNNER_UNKNOWN), "its checks ran other commands"),
    ("python3 -m unittest discover -s tests -p no:cacheprovider", dict(selection=RUNNER_UNKNOWN),
     "its checks ran other commands"),
    ("python3 -m pytest -q tests", dict(), "its checks ran other commands"),
], ids=["mixed-external", "failed", "stale-source", "skipped", "optional", "k", "pattern-collision", "pytest"])
def test_every_other_reason_still_refuses_with_its_true_wording(tmp_path, command, kw, expected):
    receipt, problem = _citation(tmp_path, command, **kw)
    assert receipt is None and expected in problem, problem
    assert "/private" not in problem and "PYTEST_ADDOPTS" not in problem and "-k x" not in problem


def test_a_matched_receipt_names_its_id_and_the_public_command_only(tmp_path):
    receipt, problem = _citation(tmp_path, "python3 -m unittest discover -t . -s tests", selection=RUNNER_UNKNOWN,
                                 requirement="`python3 -m unittest discover -t . -s tests` passes")
    assert receipt is None
    assert problem.startswith("configured check check runs `python3 -m unittest discover -t . -s tests`; its "
                              "receipt does not count as evidence: ")
    assert "its arguments carry an option the harness reads as a test selection" in problem, "-t: the narrow false negative"


def test_a_record_without_the_command_reading_keeps_the_file_level_refusal(tmp_path, monkeypatch):
    receipt, problem = _citation(tmp_path, "python3 -m unittest discover -s tests", selection=RUNNER_UNKNOWN,
                                 legacy=True, monkeypatch=monkeypatch)
    assert receipt is None and "it ran a selection, not the whole command" in problem

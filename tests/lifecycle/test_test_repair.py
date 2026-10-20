"""An operator-authorized test repair, checked against its original cases.

Run 20261010T225947Z-fec6da52: the goal authorized repairing a broken
browser test, the repaired suite passed, and the original-test check then
restored the broken run-start version and timed out. A repair is now named
by the operator as an exact file; only that file stands as repaired in the
original-test copy, and only while every case its run-start version
declared still executes, unskipped. Every other original is restored as
before, the broken original is kept as evidence, and scripted replies drive
the real run with no provider call.
"""

import json
import shlex
import sys
from pathlib import Path

import pytest

from tests.lifecycle import harness as H
from tests.lifecycle.test_lifecycle_matrix import CLOSEOUT, FIXED, Script

pytestmark = pytest.mark.requirements_ledger

# Verbose, so each executed case is named in the check's own output.
CHECK = f"{shlex.quote(sys.executable)} -m pytest -v -p no:cacheprovider --quadratus-report={{report}}"
BROKEN = "def test_reset_keeps_order():\n    assert False, 'broken harness'\n"
REPAIRED = "def test_reset_keeps_order():\n    assert [1, 2] == sorted([2, 1])\n"
FILES = {"app.py": FIXED, "tests/test_app.py": "from app import add\n\n\ndef test_add():\n    assert add(1, 2) == 3\n",
         "tests/test_reset.py": BROKEN, "README.md": "# app\n"}
SCOPE = dict(permitted_paths=["app.py", "tests/test_reset.py", "tests/test_app.py"],
             intended_result="the reset test is repaired", acceptance=["tests pass"], max_lines=40,
             proofs=H.proofs_for("R1", "tests/test_reset.py"))
DECL = ("REQUIREMENTS:\nR1: tests/test_reset.py is repaired and passes\n"
        "KIND: testing standard\nSCOPE: " + json.dumps(SCOPE) + "\nRepair tests/test_reset.py.\nCOVERS: R1")


def _run(tmp_path, monkeypatch, writes, *, repair=("tests/test_reset.py",)):
    plan = [DECL]

    def lead(call, replay):
        H.write(call, writes)
        return "Repaired the test.\nCHANGED: " + json.dumps(sorted(writes))
    script = Script(orchestrator=lambda c, r: plan.pop(0) if plan else "DONE", lead=lead,
                    closeout=lambda c, r: CLOSEOUT,
                    **{"requirements-review": lambda c, r: "COMPLETE",
                       "auditor": lambda c, r: "R1: MET - tests/test_reset.py"})
    return H.run(tmp_path, monkeypatch, script, files=FILES, check=CHECK, max_tasks=2, repair_tests=repair)


def _record(replay):
    return json.loads((Path(replay.result.run_dir) / "result.json").read_text())


def test_an_authorized_repair_keeps_its_case_and_completes(tmp_path, monkeypatch):
    replay = _run(tmp_path, monkeypatch, {"tests/test_reset.py": REPAIRED})
    assert replay.result.completed, replay.result.error
    data = _record(replay)
    [repair] = data["test_repairs"]
    assert repair["path"] == "tests/test_reset.py" and repair["original_cases"] == ["test_reset_keeps_order"]
    assert repair["checks"] and all(c["passed"] for c in repair["checks"])
    kept = (Path(replay.result.run_dir) / "artifacts")
    assert any(BROKEN in p.read_text() for p in kept.glob("*.txt")), "the broken original is kept as evidence"
    assert not any(r["passed"] is False for r in data["original_test_runs"])


def test_without_the_authorization_the_broken_original_still_fails_the_run(tmp_path, monkeypatch):
    replay = _run(tmp_path, monkeypatch, {"tests/test_reset.py": REPAIRED}, repair=())
    assert not replay.result.completed
    assert any(r["passed"] is False for r in _record(replay)["original_test_runs"])


def test_a_repair_that_removes_the_original_case_is_refused(tmp_path, monkeypatch):
    weakened = "def test_something_else():\n    assert True\n"
    replay = _run(tmp_path, monkeypatch, {"tests/test_reset.py": weakened})
    assert not replay.result.completed
    [repair] = _record(replay)["test_repairs"]
    assert not repair["checks"][0]["passed"]
    assert any("was removed by the repair" in p for p in repair["checks"][0]["problems"])


def test_a_repair_that_skips_the_original_case_is_refused(tmp_path, monkeypatch):
    skipped = ("import pytest\n\n\n@pytest.mark.skip(reason='later')\n"
               "def test_reset_keeps_order():\n    assert True\n")
    replay = _run(tmp_path, monkeypatch, {"tests/test_reset.py": skipped})
    assert not replay.result.completed
    [repair] = _record(replay)["test_repairs"]
    assert any("was skipped" in p for p in repair["checks"][0]["problems"])


def test_other_originals_are_still_restored_beside_an_authorized_repair(tmp_path, monkeypatch):
    # The repair is fine, but the same task breaks add and weakens the
    # original add test to match: the restored original catches it.
    replay = _run(tmp_path, monkeypatch, {
        "tests/test_reset.py": REPAIRED, "app.py": "def add(a, b):\n    return 0\n",
        "tests/test_app.py": "from app import add\n\n\ndef test_add():\n    assert add(1, 2) == 0\n"})
    assert not replay.result.completed
    runs = _record(replay)["original_test_runs"]
    assert any(r["passed"] is False and "tests/test_app.py" in r["changed"]
               and "tests/test_reset.py" not in r["changed"] for r in runs)


@pytest.mark.parametrize("bad, why", [
    ("tests/*.py", "one exact file"), ("tests/", "one exact file"), ("../x.py", "one exact file"),
    ("tests/missing.py", "no existing file"), ("app.py", "not a test")])
def test_a_repair_authorization_names_one_existing_test_file(tmp_path, monkeypatch, bad, why):
    from quadratus.config import Settings
    from quadratus.project_run import run_project
    root = tmp_path / "p"
    root.mkdir()
    for name, text in FILES.items():
        (root / name).parent.mkdir(parents=True, exist_ok=True)
        (root / name).write_text(text)
    with pytest.raises(ValueError, match=why):
        run_project("x", root, Settings(backend="cli"), repair_tests=[bad])


def test_a_forbidden_file_cannot_be_authorized_for_repair(tmp_path):
    from quadratus.config import Settings
    from quadratus.project_run import run_project
    root = tmp_path / "p"
    (root / "tests").mkdir(parents=True)
    (root / "tests/test_reset.py").write_text(BROKEN)
    with pytest.raises(ValueError, match="forbade"):
        run_project("x", root, Settings(backend="cli"), forbid=["tests/**"], repair_tests=["tests/test_reset.py"])


def test_a_same_name_trivial_pass_is_never_reported_verified(tmp_path, monkeypatch):
    # Keeping the name and running the case is all the engine can check; a
    # repair that keeps both and asserts nothing must not read as verified.
    import importlib.util
    trivial = "def test_reset_keeps_order():\n    assert True\n"
    replay = _run(tmp_path, monkeypatch, {"tests/test_reset.py": trivial})
    data = _record(replay)
    [repair] = data["test_repairs"]
    assert repair["checks"][-1]["passed"] and repair["assertions_verified"] is False
    assert "assertions are not independently checked" in replay.result.report
    spec = importlib.util.spec_from_file_location(
        "truth_check", Path(__file__).resolve().parents[2] / "tools" / "truth_check.py")
    truth = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(truth)
    result = truth.judge(replay.result.run_dir, replay.project)
    assert result["verdict"] != truth.VERIFIED
    assert any("tests/test_reset.py" in r and "not independently checked" in r for r in result["unverified"])

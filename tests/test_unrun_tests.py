"""A test file a task adds or changes that no required check runs is an
unverified finding, never passed by the gate's silence (batch 2 gui-ui-v3 on
5d9f5ff: t2 added tests/ui/project_search.test.js; the frozen Node check
names five other files; the lead's `node --test tests/ui` was correctly
refused; the new tests never ran)."""

from types import SimpleNamespace

from quadratus.artifacts import ArtifactStore
from quadratus.integration import GateCommand, uncovered_tests
from quadratus.memory import TaskMemory
from quadratus.session import Session, SessionConfig

NODE = ("node", "--test", "--test-reporter-destination={report}", "tests/ui/bulk_coverage.test.js",
        "tests/ui/load_app.test.js")
PYTEST = ("python", "-m", "pytest", "-q", "-p", "no:cacheprovider", "--quadratus-report={report}")


def test_the_gui_ui_v3_case_is_named():
    changed = ["static/js/app.js", "tests/ui/project_search.test.js", "tests/ui/load_app.test.js"]
    assert uncovered_tests(changed, [PYTEST, NODE]) == ["tests/ui/project_search.test.js"]


def test_nothing_is_claimed_where_coverage_cannot_be_told():
    assert uncovered_tests(["tests/test_new.py"], [PYTEST, NODE]) == [], "pytest discovery covers it"
    assert uncovered_tests(["tests/other/new.test.js"], [NODE]) == [], "no check lists that folder"
    assert uncovered_tests(["tests/ui/new.spec.ts"], [NODE]) == [], "a different kind of test file"
    assert uncovered_tests(["tests/ui/new.test.js"], [("node", "--test", "tests/ui")]) == [], "folder operand"
    assert uncovered_tests(["tests/ui/new.test.js"], [NODE, ("node", "--test", "tests/ui/new.test.js")]) == []
    assert uncovered_tests(["app.py", "README.md"], [PYTEST, NODE]) == []


def test_an_explicit_pytest_file_list_does_not_cover_a_new_python_test():
    listed = ("python", "-m", "pytest", "-q", "tests/test_a.py")
    assert uncovered_tests(["tests/test_b.py"], [listed]) == ["tests/test_b.py"]
    assert uncovered_tests(["tests/test_a.py"], [listed]) == []


def test_the_session_names_the_file_without_claiming_it_did_not_run(tmp_path):
    """Codex review of 7515f28: a wrapper can run a file no check names, so
    the observation never becomes a finding that it did not run."""
    root = tmp_path / "project"
    (root / "tests" / "ui").mkdir(parents=True)
    (root / "tests" / "ui" / "load_app.test.js").write_text("// existing\n")
    session = Session("goal", ArtifactStore(tmp_path / "artifacts"), lambda *a, **k: "",
                      config=SessionConfig(project=root, allow_writes=True))
    session._task_before = session._capture_source()
    (root / "tests" / "ui" / "project_search.test.js").write_text("// new\n")
    gate = SimpleNamespace(commands=(GateCommand(id="check", argv=PYTEST), GateCommand(id="extra-1", argv=NODE)))
    task = TaskMemory("t2", "claude:opus", session.store)
    spec = SimpleNamespace(task_id="t2")
    session._note_unrun_tests(spec, task, gate)
    assert session.open_findings == [], "not named is not proof of not run"
    assert session.unnamed_test_files == ["tests/ui/project_search.test.js"]
    assert gate.commands[1].argv == NODE, "the configured commands are unchanged"


def test_the_lead_is_told_new_tests_belong_in_a_listed_file():
    import inspect

    from quadratus import runtime
    assert "never runs and its tests stay unverified" in inspect.getsource(runtime)


def test_a_new_test_file_a_run_test_invokes_is_not_claimed(tmp_path):
    """Batch 2 gui-ui-v3 t5: a pytest test ran node on the new search tests,
    inside the approved pytest check. A mention by name is evidence it may
    run, so nothing is claimed."""
    root = tmp_path / "project"
    (root / "tests" / "ui").mkdir(parents=True)
    session = Session("goal", ArtifactStore(tmp_path / "artifacts"), lambda *a, **k: "",
                      config=SessionConfig(project=root, allow_writes=True))
    session._task_before = session._capture_source()
    (root / "tests" / "ui" / "project_search.test.js").write_text("// new\n")
    (root / "tests" / "test_node_bridge.py").write_text(
        "import subprocess\ndef test_search():\n"
        "    subprocess.run(['node', '--test', 'tests/ui/project_search.test.js'], check=True)\n")
    gate = SimpleNamespace(commands=(GateCommand(id="check", argv=PYTEST), GateCommand(id="extra-1", argv=NODE)))
    session._note_unrun_tests(SimpleNamespace(task_id="t5"), TaskMemory("t5", "x", session.store), gate)
    assert session.open_findings == []

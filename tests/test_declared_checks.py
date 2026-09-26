"""Every check a project declares runs in the gate (Codex, Run 15).

The operator's check was pytest; the project also declared a package.json
test script, and its Node UI tests never ran in-run.
"""

import json
import shutil
import sys

import pytest

from quadratus.integration import GateCommand, GateSuite, _test_count
from quadratus.project_run import (
    _extra_gate_commands,
    _gate_plan,
    _merge_extras,
    _with_declared_checks,
)
from quadratus.repo_scan import scan_repo


def _project(tmp_path, *, package=None, python=True):
    if package is not None:
        (tmp_path / "package.json").write_text(json.dumps(package))
    if python:
        (tmp_path / "tests").mkdir()
        (tmp_path / "tests" / "test_x.py").write_text("def test_x():\n    pass\n")
    return scan_repo(tmp_path)


def test_both_a_node_script_and_a_python_suite_are_declared(tmp_path):
    scan = _project(tmp_path, package={"scripts": {"test": "node --test"}})
    assert scan.declared_checks[0] == ["npm", "test", "--silent"]
    assert scan.declared_checks[1][1:] == ["-m", "pytest", "-q"]
    assert scan.check_command == ["npm", "test", "--silent"], "the single-command choice is unchanged"


def test_an_operator_pytest_check_gains_only_the_node_script(tmp_path):
    scan = _project(tmp_path, package={"scripts": {"test": "node --test"}})
    gates = _with_declared_checks(["python", "-m", "pytest", "-q"], scan)
    assert [(g.id, g.argv, g.required) for g in gates] == [
        ("check", ("python", "-m", "pytest", "-q"), True),
        ("declared-npm", ("npm", "test", "--silent"), True)]


def test_nothing_extra_means_the_single_gate_stands(tmp_path):
    assert _with_declared_checks([sys.executable, "-m", "pytest", "-q"], _project(tmp_path)) is None
    node_only = tmp_path / "node-only"
    node_only.mkdir()
    scan = _project(node_only, package={"scripts": {"test": "node --test"}}, python=False)
    assert _with_declared_checks(["npm", "test", "--silent"], scan) is None
    assert _with_declared_checks(None, scan) is None


def test_a_package_without_a_test_script_declares_nothing(tmp_path):
    scan = _project(tmp_path, package={"scripts": {"build": "tsc"}}, python=False)
    assert scan.declared_checks == [] and scan.check_command is None
    malformed = tmp_path / "m"
    malformed.mkdir()
    (malformed / "package.json").write_text("[1, 2]")
    assert scan_repo(malformed).declared_checks == []


# -- Codex review of 3ef9962 -------------------------------------------------------------



def _tree(root, files):
    for name, text in files.items():
        (root / name).parent.mkdir(parents=True, exist_ok=True)
        (root / name).write_text(text)
    return scan_repo(root)


@pytest.mark.parametrize("files,expected", [
    ({"package.json": json.dumps({"scripts": {"test": "node --test tests/ui.test.js"}}),
      "tests/ui.test.js": "//"}, [["npm", "test", "--silent"]]),                        # JS-only
    ({"tests/test_x.py": "def test_x(): pass\n"}, ["pytest"]),                           # Python-only
    ({"package.json": json.dumps({"scripts": {"test": "node --test"}}),
      "tests/ui/a.test.js": "//", "tests/py/test_b.py": ""}, [["npm", "test", "--silent"], "pytest"]),  # mixed
    ({"package.json": json.dumps({"scripts": {"build": "tsc"}}), "tests/ui.test.js": "//"}, []),   # absent
    ({"pyproject.toml": "[project]\nname='x'\n", "tests/data.json": "{}"}, ["pytest"]),  # manifest signal
])
def test_a_python_gate_needs_python_evidence_not_just_a_tests_folder(tmp_path, files, expected):
    declared = _tree(tmp_path, files).declared_checks
    shape = ["pytest" if c[1:] == ["-m", "pytest", "-q"] else c for c in declared]
    assert shape == expected


@pytest.mark.parametrize("output,count", [
    ("ℹ tests 1\nℹ suites 0\nℹ pass 0\nℹ fail 0\nℹ cancelled 0\nℹ skipped 1\nℹ todo 0\n", 0),   # all skipped
    ("ℹ tests 3\nℹ pass 2\nℹ fail 1\nℹ skipped 0\n", 3),
    ("ℹ tests 2\nℹ pass 0\nℹ fail 0\nℹ cancelled 2\n", 0),
    ("TAP version 13\n# tests 4\n# pass 3\n# fail 0\n# todo 1\n", 3),
    ("# tests 4\n# skip 1\n", 3),
    ("ℹ tests 1\nℹ pass 1\nℹ fail 0\n", 1),        # a blank file is one file-level test to Node itself
    ("12 passed, 1 skipped in 0.3s", 12),
    ("compiled ok", None),
])
def test_runner_summaries_count_only_executed_cases(output, count):
    assert _test_count(output) == count


@pytest.mark.skipif(not shutil.which("node"), reason="needs node")
def test_an_all_skipped_node_suite_fails_and_a_syntax_check_needs_no_count(tmp_path):
    (tmp_path / "skip.test.js").write_text("require('node:test').test.skip('later', () => {});\n")
    (tmp_path / "app.js").write_text("const x = 1;\n")
    gates = _extra_gate_commands([["node", "--test", "skip.test.js"], ["node", "--check", "app.js"]])
    assert [g.minimum_tests for g in gates] == [1, None]
    result = GateSuite(gates, cwd=tmp_path).run()
    by_id = {r.id: r for r in result.receipts}
    assert not result.passed
    assert by_id["extra-1"].status == "failed" and by_id["extra-1"].reason == "zero tests executed"
    assert by_id["extra-2"].status == "passed" and by_id["extra-2"].tests is None
    assert by_id["extra-1"].source_hash == by_id["extra-2"].source_hash, "one unchanged tree for both"


def test_an_extra_may_state_its_own_minimum(tmp_path):
    (gate,) = _extra_gate_commands([{"argv": ["make", "check"], "minimum_tests": 5}])
    assert gate.minimum_tests == 5 and gate.argv == ("make", "check")


PY = [sys.executable, "-m", "pytest", "-q"]


@pytest.mark.parametrize("command,covers", [
    (PY, True),
    (["pytest", "-q"], True),
    (["python3", "-m", "pytest"], True),
    (["python", "-m", "pytest", "tests/test_one.py"], False),       # a subset
    (["python", "-m", "pytest", "-q", "-k", "fast"], False),        # a selection
    (["echo", "pytest"], False),                                    # merely mentions it
])
def test_only_an_equivalent_full_suite_covers_the_declared_pytest(tmp_path, command, covers):
    scan = _tree(tmp_path, {"tests/test_x.py": "def test_x(): pass\n"})
    gates = _gate_plan(command, [], scan)
    assert (gates is None) is covers
    if not covers:
        assert [g.id for g in gates] == ["check", "declared-" + scan.declared_checks[0][0].rsplit("/", 1)[-1]]
        assert gates[-1].minimum_tests == 1


def test_a_selected_node_file_does_not_cover_the_declared_npm_suite(tmp_path):
    scan = _tree(tmp_path, {"package.json": json.dumps({"scripts": {"test": "node --test"}}),
                            "tests/ui.test.js": "//"})
    gates = _gate_plan(["node", "--test", "tests/ui.test.js"], [], scan)
    assert [g.id for g in gates] == ["check", "declared-npm"]


def test_programmatic_gates_keep_the_extras_and_refuse_a_clash():
    mine = [GateCommand(id="mine", argv=("true",))]
    extras = _extra_gate_commands([["node", "--check", "app.js"]])
    assert [g.id for g in _merge_extras(mine, extras)] == ["mine", "extra-1"]
    assert _merge_extras(mine, []) == mine
    with pytest.raises(ValueError, match="clash with configured gate ids: extra-1"):
        _merge_extras([GateCommand(id="extra-1", argv=("true",))], extras)


def test_policy_task_gates_keep_the_operator_extras(tmp_path):
    from quadratus.policy import load_policy, task_gate
    from tests.test_policy import document, write_policy
    doc = document()
    doc["gates"].append({"id": "lint", "runner": "command", "argv": ["true"], "required": True})
    doc["defaults"]["required_gates"] = ["scope", "lint"]
    write_policy(tmp_path, doc)
    policy = load_policy(tmp_path)
    existing = GateSuite([GateCommand(id="check", argv=("true",)), *_extra_gate_commands([["true"]])],
                         cwd=tmp_path)
    gate = task_gate(policy, policy.resolve(["a.py"]), existing)
    ids = [c.id for c in gate.commands]
    assert "lint" in ids and "check" in ids and "extra-1" in ids


# -- the Python-file probe consumes a bounded number of entries (Codex review of 8a71d25) --

class _Entry:
    def __init__(self, name, *, directory=False, path=""):
        self.name, self.path, self._dir = name, path or name, directory

    def is_symlink(self):
        return False

    def is_dir(self, follow_symlinks=True):
        return self._dir


class _Listing:
    """A scandir stand-in that fails the test if read past the budget."""

    consumed = 0

    def __init__(self, entries, budget):
        self.entries, self.budget = entries, budget

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def __iter__(self):
        for entry in self.entries:
            _Listing.consumed += 1
            if _Listing.consumed > self.budget + 1:
                raise AssertionError("read past the budget")
            yield entry


@pytest.mark.parametrize("layout,found", [
    ("flat", False),              # 100,000 non-Python entries: stops at the budget
    ("at-limit", True),           # the Python file is entry number `limit`
    ("past-limit", False),        # one entry further: not proven
    ("deep", False),              # a chain of directories deeper than the budget
])
def test_the_python_probe_stops_at_its_budget(monkeypatch, tmp_path, layout, found):
    from quadratus import repo_scan
    limit = 50
    _Listing.consumed = 0

    def scandir(path):
        path = str(path)
        if layout == "flat":
            return _Listing((_Entry(f"f{i}.js") for i in range(100_000)), limit)
        if layout == "at-limit":
            return _Listing([*(_Entry(f"f{i}.js") for i in range(limit - 1)), _Entry("test_x.py")], limit)
        if layout == "past-limit":
            return _Listing([*(_Entry(f"f{i}.js") for i in range(limit)), _Entry("test_x.py")], limit)
        return _Listing([_Entry("d", directory=True, path=path + "/d")], limit)       # deep
    monkeypatch.setattr(repo_scan.os, "scandir", scandir)
    assert repo_scan._has_python_tests(tmp_path, limit=limit) is found
    assert _Listing.consumed <= limit + 1


def test_the_python_probe_skips_hidden_cache_and_linked_directories(tmp_path):
    from quadratus.repo_scan import _has_python_tests
    tests = tmp_path / "tests"
    for hidden in (".cache", "__pycache__", "node_modules"):
        (tests / hidden).mkdir(parents=True)
        (tests / hidden / "test_x.py").write_text("")
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    (elsewhere / "test_y.py").write_text("")
    (tests / "linked").symlink_to(elsewhere, target_is_directory=True)
    (tests / "ui.test.js").write_text("//")
    assert _has_python_tests(tests) is False
    (tests / "unit").mkdir()
    (tests / "unit" / "test_z.py").write_text("")
    assert _has_python_tests(tests) is True

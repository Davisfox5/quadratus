"""Impact selection and shard coverage on small, real Git inventories."""

from __future__ import annotations

import subprocess
from collections import Counter

import pytest

from tools.ci import plan

SMOKE = {"tests/test_run_budget.py", "tests/test_prompt_rule_parity.py"}


def _repo(tmp_path, files):
    files = {**{p: "def test_smoke(): pass\n" for p in SMOKE}, **files}
    for name, source in files.items():
        path = tmp_path / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(source)
    for args in (
        ["init", "-q"],
        ["add", "-f", "--", *files],
        ["-c", "user.name=CI fixture", "-c", "user.email=ci@example.test",
         "-c", "core.hooksPath=/dev/null", "commit", "--no-gpg-sign", "-qm", "fixture"],
    ):
        subprocess.run(["git", *args], cwd=tmp_path, check=True, capture_output=True, timeout=10)
    return tmp_path


def test_imports_reach_tests_through_production_modules_and_relative_test_helpers(tmp_path):
    root = _repo(tmp_path, {
        "quadratus/leaf.py": "raise RuntimeError('the planner must never import source')\n",
        "quadratus/service.py": "from .leaf import value\n",
        "tests/helpers.py": "from quadratus import service\n",
        "tests/test_indirect.py": "def test_it():\n    from . import helpers\n",
        "tests/test_other.py": "def test_other(): pass\n",
    })
    selected, _, _ = plan.select(root, ["quadratus/leaf.py"])
    assert set(selected) == SMOKE | {"tests/test_indirect.py"}


def test_a_changed_test_helper_selects_its_consumers(tmp_path):
    root = _repo(tmp_path, {
        "tests/helpers.py": "value = 1\n",
        "tests/test_using_helper.py": "from .helpers import value\n",
        "tests/test_other.py": "def test_other(): pass\n",
    })
    selected, _, _ = plan.select(root, ["tests/helpers.py"])
    assert set(selected) == SMOKE | {"tests/test_using_helper.py"}


@pytest.mark.parametrize("reference", [
    "source = 'quadratus/leaf.py'\n",
    "target = 'quadratus.leaf.value'\n",
    "import importlib\nmodule = importlib.import_module('quadratus.leaf')\n",
])
def test_literal_source_paths_and_dynamic_module_names_are_dependencies(tmp_path, reference):
    root = _repo(tmp_path, {
        "quadratus/leaf.py": "value = 1\n",
        "tests/test_read_source.py": reference,
        "tests/test_other.py": "def test_other(): pass\n",
    })
    selected, _, _ = plan.select(root, ["quadratus/leaf.py"])
    assert set(selected) == SMOKE | {"tests/test_read_source.py"}


def test_segmented_source_paths_are_kept_when_another_consumer_is_mapped(tmp_path):
    root = _repo(tmp_path, {
        "tools/acceptance/preflight.py": "value = 1\n",
        "tests/test_literal.py": "source = 'tools/acceptance/preflight.py'\n",
        "tests/test_segmented.py": (
            "from pathlib import Path\n"
            "source = (Path(__file__).parents[1] / 'tools' / 'acceptance' / 'preflight.py').read_text()\n"
        ),
        "tests/test_other.py": "def test_other(): pass\n",
    })
    selected, _, _ = plan.select(root, ["tools/acceptance/preflight.py"])
    assert set(selected) == SMOKE | {"tests/test_literal.py", "tests/test_segmented.py"}


def test_unreferenced_docs_and_dev_record_do_not_schedule_tests(tmp_path):
    root = _repo(tmp_path, {"docs/guide.md": "Operator prose.\n", "docs/dev-record.json": "{}\n"})
    selected, all_tests, reasons = plan.select(root, ["docs/guide.md", "docs/dev-record.json"])
    assert selected == [] and set(all_tests) == SMOKE
    assert reasons


def test_docs_read_by_a_test_are_contract_inputs(tmp_path):
    root = _repo(tmp_path, {
        "docs/rules.md": "A contract.\n",
        "tests/test_rules.py": "source = 'docs/rules.md'\n",
    })
    selected, all_tests, _ = plan.select(root, ["docs/rules.md"])
    assert selected == all_tests


@pytest.mark.parametrize("changed", [
    "quadratus/deleted.py", "contracts/data.json", "quadratus/unmapped.py",
    "requirements-dev.txt", "tests/conftest.py", "tests/lifecycle/conftest.py",
    ".github/workflows/ci.yml", "tools/ci/plan.py",
])
def test_deleted_unknown_unmapped_and_global_inputs_fall_back_to_all_tests(tmp_path, changed):
    root = _repo(tmp_path, {
        "quadratus/unmapped.py": "value = 1\n",
        "contracts/data.json": "{}\n",
        "tests/test_other.py": "def test_other(): pass\n",
    })
    selected, all_tests, reasons = plan.select(root, [changed])
    assert selected == all_tests and reasons


def test_a_tested_change_cannot_hide_another_change_without_reachable_tests(tmp_path):
    root = _repo(tmp_path, {
        "quadratus/covered.py": "value = 1\n",
        "quadratus/uncovered.py": "value = 1\n",
        "quadratus/consumer.py": "from .uncovered import value\n",
        "tests/test_covered.py": "from quadratus.covered import value\n",
        "tests/test_other.py": "def test_other(): pass\n",
    })
    selected, all_tests, _ = plan.select(root, ["quadratus/covered.py", "quadratus/uncovered.py"])
    assert selected == all_tests


def test_an_unstaged_test_deletion_plans_all_remaining_tests(tmp_path):
    removed = "tests/test_removed.py"
    root = _repo(tmp_path, {
        removed: "def test_removed(): pass\n",
        "tests/test_survives.py": "def test_survives(): pass\n",
    })
    (root / removed).unlink()
    result = plan.build(root, [removed])
    assert set(result["selected"]) == SMOKE | {"tests/test_survives.py"}
    assert result["coverage"] == "full" and result["omitted"] == []
    assert Counter(p for shard in result["shards"] for p in shard["files"]) == Counter(result["selected"])


def test_full_plan_assigns_every_test_once_and_balances_measured_work(tmp_path):
    files = {f"tests/test_case_{i}.py": "def test_case(): pass\n" for i in range(4)}
    root = _repo(tmp_path, files)
    durations = dict(zip(sorted(files), [9, 8, 2, 1], strict=True))
    durations.update({p: 1 for p in SMOKE})
    result = plan.build(root, [], full=True, shards=2, durations=durations)
    assigned = [p for shard in result["shards"] for p in shard["files"]]
    assert Counter(assigned) == Counter({p: 1 for p in files.keys() | SMOKE})
    assert result["omitted"] == [] and result["coverage"] == "full"
    assert [s["weight"] for s in result["shards"]] == [11, 11]
    assert result["shards"] == plan.build(root, [], full=True, shards=2, durations=durations)["shards"]


def test_empty_plan_has_no_shard_that_could_trigger_pytest_default_discovery(tmp_path):
    root = _repo(tmp_path, {"docs/guide.md": "Prose.\n"})
    result = plan.build(root, ["docs/guide.md"])
    assert result["selected"] == [] and result["shards"] == []
    assert set(result["omitted"]) == SMOKE


@pytest.mark.parametrize("missing", ["validated_run", "head", "source_sha256"])
def test_previous_baseline_requires_success_identity_and_source_inputs(missing):
    previous = {"validated_run": "12345", "head": "previous", "source_sha256": {"app.py": "old"}}
    del previous[missing]
    with pytest.raises(ValueError):
        plan.changes_since(previous, {"app.py": "new"})


def test_previous_baseline_detects_added_removed_and_modified_inputs():
    previous = {
        "validated_run": "12345", "head": "previous",
        "source_sha256": {"same.py": "same", "changed.py": "old", "removed.py": "gone"},
    }
    current = {"same.py": "same", "changed.py": "new", "added.py": "new"}
    assert plan.changes_since(previous, current) == ["added.py", "changed.py", "removed.py"]


def test_new_untracked_python_test_is_included_in_local_plan(tmp_path):
    root = _repo(tmp_path, {"tests/test_old.py": "def test_old(): pass\n"})
    (root / "tests/test_new.py").write_text("def test_new(): pass\n")
    selected, _, _ = plan.select(root, ["tests/test_new.py"])
    assert set(selected) == SMOKE | {"tests/test_new.py"}

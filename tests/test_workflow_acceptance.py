"""The inventory must not turn absent proof into a passing journey."""

import importlib.util
import json
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location("workflow_acceptance", ROOT / "tools/workflow_acceptance.py")
assert SPEC and SPEC.loader
wa = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(wa)


def test_manifest_matches_map_and_selectors_exist():
    manifest = json.loads((ROOT / "docs/workflow-acceptance-manifest.json").read_text())
    assert wa.validate(manifest, ROOT) == []
    assert len(manifest["journeys"]) == 39


def test_missing_proof_stays_unproven_after_mapped_test_passes():
    manifest = {"journeys": [{"id": "J1", "tests": ["tests/test_x.py::test_x"],
                "required_obligations": ["calls", "outcome"],
                "coverage": {"outcome": ["tests/test_x.py::test_x"]}}]}
    result = wa.assess(manifest, {"tests/test_x.py::test_x": "PASS"})
    assert result["journeys"][0]["status"] == "UNPROVEN"
    assert result["journeys"][0]["gaps"] == ["calls"]


def test_failure_and_skip_are_distinct_from_unproven():
    selector = "tests/test_x.py::test_x"
    manifest = {"journeys": [{"id": "J1", "tests": [selector],
                "required_obligations": ["calls"], "coverage": {"calls": [selector]}}]}
    assert wa.assess(manifest, {selector: "PASS"})["status"] == "PASS"
    assert wa.assess(manifest, {selector: "FAIL"})["journeys"][0]["status"] == "FAIL"
    assert wa.assess(manifest, {selector: "SKIP"})["journeys"][0]["status"] == "SKIP"
    assert wa.assess(manifest, {})["journeys"][0]["status"] == "UNPROVEN"


def test_a_known_failure_takes_precedence_over_a_missing_cited_test():
    failed, absent = "tests/test_x.py::test_failed", "tests/test_x.py::test_absent"
    manifest = {"journeys": [{"id": "J1", "tests": [failed, absent],
                "required_obligations": ["calls"], "coverage": {"calls": [failed, absent]}}]}
    row = wa.assess(manifest, {failed: "FAIL"})["journeys"][0]
    assert row["tests"][absent] == "UNPROVEN"
    assert row["obligations"]["calls"] == "FAIL"
    assert row["status"] == "FAIL"


def test_junit_folds_parameterized_cases_and_preserves_failures(tmp_path):
    xml = tmp_path / "results.xml"
    xml.write_text('''<testsuite>
      <testcase classname="tests.lifecycle.test_example" name="test_route[a]"/>
      <testcase classname="tests.lifecycle.test_example" name="test_route[b]"><failure/></testcase>
      <testcase classname="tests.lifecycle.test_example" name="test_optional"><skipped/></testcase>
    </testsuite>''')
    assert wa.read_junit(xml) == {
        "tests/lifecycle/test_example.py::test_route": "FAIL",
        "tests/lifecycle/test_example.py::test_optional": "SKIP",
    }


def test_duplicate_or_missing_journey_cannot_be_reported_as_coverage():
    manifest = json.loads((ROOT / "docs/workflow-acceptance-manifest.json").read_text())
    manifest["journeys"].append(manifest["journeys"][0])
    assert any("duplicate journey" in error for error in wa.validate(manifest, ROOT))
    manifest["journeys"].pop()
    manifest["journeys"].pop()
    assert any("journey ids differ" in error for error in wa.validate(manifest, ROOT))

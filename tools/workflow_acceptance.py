"""Offline acceptance inventory for the workflow-map whole journeys.

This runs mapped scripted controller tests and reports obligation coverage. A
passing test never substitutes for an unmapped assertion or a live run.
"""

from __future__ import annotations

import argparse
import ast
import json
import re
import subprocess
import sys
import tempfile
import xml.etree.ElementTree as ET
from pathlib import Path

OBLIGATIONS = {"calls", "outcome", "report", "evidence_identity", "partial_work"}
STATUSES = {"PASS", "FAIL", "SKIP", "UNPROVEN"}
ROOT = Path(__file__).resolve().parents[1]


def map_ids(text: str) -> set[str]:
    section = text.split("## 7. Whole-journey acceptance matrix", 1)[1].split("## 8.", 1)[0]
    return set(re.findall(r"^\| (J\d+[ab]?) \|", section, flags=re.MULTILINE))


def validate(manifest: dict, root: Path) -> list[str]:
    errors = []
    if manifest.get("version") != 1:
        errors.append("unsupported manifest version")
    source = manifest.get("source_map")
    if source != "docs/workflow-map.md":
        errors.append("source_map must be docs/workflow-map.md")
        return errors
    try:
        expected = map_ids((root / source).read_text())
    except (OSError, IndexError) as exc:
        return errors + [f"cannot read journey matrix: {exc}"]
    journeys = manifest.get("journeys")
    if not isinstance(journeys, list):
        return errors + ["journeys must be a list"]
    actual = [item.get("id") for item in journeys if isinstance(item, dict)]
    if len(actual) != len(set(actual)):
        errors.append("duplicate journey id")
    if set(actual) != expected:
        errors.append(f"journey ids differ from map: missing={sorted(expected - set(actual))}, extra={sorted(set(actual) - expected)}")
    for item in journeys:
        if not isinstance(item, dict):
            errors.append("journey entry is not an object")
            continue
        jid = item.get("id", "?")
        tests = item.get("tests", [])
        required = item.get("required_obligations", [])
        coverage = item.get("coverage", {})
        if not isinstance(tests, list) or not isinstance(required, list) or not isinstance(coverage, dict):
            errors.append(f"{jid}: malformed tests, obligations or coverage")
            continue
        if not set(required) <= OBLIGATIONS or not {"calls", "outcome", "report", "partial_work"} <= set(required):
            errors.append(f"{jid}: invalid or missing standard obligations")
        if not set(coverage) <= set(required):
            errors.append(f"{jid}: coverage outside required obligations")
        for obligation, selectors in coverage.items():
            if not isinstance(selectors, list) or not selectors or not set(selectors) <= set(tests):
                errors.append(f"{jid}: {obligation} must cite mapped tests")
        for selector in tests:
            if not isinstance(selector, str) or not re.fullmatch(r"tests/(?:[\w-]+/)*test_[\w-]+\.py::test_[\w-]+", selector):
                errors.append(f"{jid}: invalid selector {selector!r}")
                continue
            file_name, function_name = selector.split("::")
            try:
                tree = ast.parse((root / file_name).read_text())
            except (OSError, SyntaxError) as exc:
                errors.append(f"{jid}: cannot inspect {file_name}: {exc}")
                continue
            if function_name not in {node.name for node in tree.body if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))}:
                errors.append(f"{jid}: selector missing in source: {selector}")
    return errors


def read_junit(path: Path) -> dict[str, str]:
    """Normalize parameterized JUnit test cases to their function selector."""
    cases: dict[str, list[str]] = {}
    for case in ET.parse(path).iter("testcase"):
        klass, name = case.attrib.get("classname", ""), case.attrib.get("name", "")
        selector = klass.replace(".", "/") + ".py::" + name.split("[", 1)[0]
        if not selector.startswith("tests/"):
            selector = "tests/" + selector
        status = "FAIL" if case.find("failure") is not None or case.find("error") is not None else (
            "SKIP" if case.find("skipped") is not None else "PASS"
        )
        cases.setdefault(selector, []).append(status)
    return {selector: "FAIL" if "FAIL" in values else "SKIP" if "SKIP" in values else "PASS"
            for selector, values in cases.items()}


def assess(manifest: dict, cases: dict[str, str]) -> dict:
    rows = []
    for item in manifest["journeys"]:
        selectors = item["tests"]
        test_statuses = {selector: cases.get(selector, "UNPROVEN") for selector in selectors}
        obligation_statuses = {}
        for obligation in item["required_obligations"]:
            proof = item["coverage"].get(obligation, [])
            values = [test_statuses[selector] for selector in proof]
            obligation_statuses[obligation] = (
                "UNPROVEN" if not proof or "UNPROVEN" in values else
                "FAIL" if "FAIL" in values else "SKIP" if "SKIP" in values else "PASS"
            )
        values = list(obligation_statuses.values()) + list(test_statuses.values())
        status = "FAIL" if "FAIL" in values else "UNPROVEN" if "UNPROVEN" in values else (
            "SKIP" if "SKIP" in values else "PASS"
        )
        rows.append({"id": item["id"], "status": status, "tests": test_statuses,
                     "obligations": obligation_statuses,
                     "gaps": [key for key, value in obligation_statuses.items() if value != "PASS"]})
    counts = {status: sum(row["status"] == status for row in rows) for status in sorted(STATUSES)}
    return {"status": "PASS" if counts["PASS"] == len(rows) else "FAIL" if counts["FAIL"] else "UNPROVEN",
            "counts": counts, "journeys": rows,
            "limitations": manifest.get("limitations", [])}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--manifest", type=Path, default=ROOT / "docs/workflow-acceptance-manifest.json")
    parser.add_argument("--report", type=Path, help="Write a machine-readable assessment")
    parser.add_argument("--no-run", action="store_true", help="Only inspect manifest and coverage; tests become UNPROVEN")
    args = parser.parse_args(argv)
    manifest = json.loads(args.manifest.read_text())
    errors = validate(manifest, ROOT)
    if errors:
        print("Invalid acceptance manifest:\n" + "\n".join(errors), file=sys.stderr)
        return 2
    cases: dict[str, str] = {}
    runner_exit = None
    if not args.no_run:
        selectors = sorted({selector for item in manifest["journeys"] for selector in item["tests"]})
        if selectors:
            with tempfile.TemporaryDirectory(prefix="quadratus-acceptance-") as directory:
                junit = Path(directory) / "results.xml"
                process = subprocess.run([sys.executable, "-m", "pytest", "-q", "--junitxml", str(junit), *selectors],
                                         cwd=ROOT, text=True, stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
                runner_exit = process.returncode
                if junit.exists():
                    try:
                        cases = read_junit(junit)
                    except ET.ParseError:
                        pass
                print(process.stdout[-12000:], file=sys.stderr)
    result = assess(manifest, cases)
    result["runner_exit"] = runner_exit
    result["baseline_commit"] = manifest["baseline_commit"]
    if runner_exit not in (None, 0) and result["counts"]["FAIL"] == 0:
        result["runner_error"] = "test collection or execution did not complete normally"
        if result["status"] == "PASS":
            result["status"] = "UNPROVEN"
    if args.report:
        args.report.write_text(json.dumps(result, indent=2) + "\n")
    print(json.dumps({"status": result["status"], "counts": result["counts"], "runner_exit": runner_exit}))
    return 0 if result["status"] == "PASS" and runner_exit == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())

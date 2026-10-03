"""Helpers shared by the frozen graders."""
from __future__ import annotations

import csv
import io
import shutil
import subprocess
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
BASELINE_TESTS = HERE / "baseline" / "tests"

PY_BASELINE = ("tests/test_basic.py", "tests/test_import_preview.py", "tests/test_bulk_edit.py")


def run_baseline_regressions(project_root: Path, tmp: Path) -> list:
    """Run the baseline (1cd9264) tests, frozen here, against the cell's code.

    The cell's ``app.py``, ``templates/`` and ``static/`` are copied into a
    temp tree beside the frozen tests, so the builders' own edits to
    ``tests/`` never decide this grade. Returns the failures as strings."""
    tree = tmp / "regression"
    tree.mkdir()
    for name in ("app.py", "requirements.txt"):
        if (project_root / name).is_file():
            shutil.copy2(project_root / name, tree / name)
    for folder in ("templates", "static"):
        if (project_root / folder).is_dir():
            shutil.copytree(project_root / folder, tree / folder)
    shutil.copytree(BASELINE_TESTS, tree / "tests")
    failures = []
    proc = subprocess.run([sys.executable, "-m", "pytest", "-q", "-p", "no:cacheprovider", *PY_BASELINE],
                          cwd=tree, capture_output=True, text=True, timeout=600)
    if proc.returncode != 0:
        failures.append(f"pytest baseline exit {proc.returncode}:\n{(proc.stdout + proc.stderr)[-3000:]}")
    node = shutil.which("node")
    if not node:
        failures.append("node is not on PATH; the baseline UI tests could not run")
        return failures
    for test in sorted((tree / "tests" / "ui").glob("*.test.js")):
        proc = subprocess.run([node, str(test)], cwd=tree, capture_output=True, text=True, timeout=300)
        if proc.returncode != 0:
            failures.append(f"{test.name} exit {proc.returncode}:\n{(proc.stdout + proc.stderr)[-2000:]}")
    return failures


def parse_csv(text: str) -> list:
    return list(csv.reader(io.StringIO(text)))


def wait_until(page, predicate: str, timeout_ms: int = 5000):
    """Poll a JS predicate in the page until true (or raise)."""
    page.wait_for_function(predicate, timeout=timeout_ms)

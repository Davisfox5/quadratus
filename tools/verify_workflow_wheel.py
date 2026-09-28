"""Build an offline wheel and exercise its installed gate producer outside the source tree."""

from __future__ import annotations

import argparse
import json
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path


PROBE = r'''
import hashlib
import json
import sys
from pathlib import Path

installed, project = map(Path, sys.argv[1:3])
sys.path.insert(0, str(installed))
import quadratus
from quadratus import integration

assert Path(quadratus.__file__).resolve().is_relative_to(installed.resolve())
assert Path(integration.__file__).resolve().is_relative_to(installed.resolve())
producer = integration._PRODUCER_FILE
assert producer.is_file() and producer.resolve().is_relative_to(installed.resolve())

project.mkdir()
(project / "test_app.py").write_text("def test_one():\n    assert False, 'product assertion'\n")
(project / "quadratus_gate_report.py").write_text("raise RuntimeError('project shadow loaded')\n")
argv = [sys.executable, "-m", "pytest", "-q", "-p", "no:cacheprovider",
        "--quadratus-report=" + integration.REPORT_TOKEN]
assertion = integration.IntegrationGate(argv, cwd=project).run()
assert assertion.report["state"] == "parsed", assertion.report
assert assertion.report["failures"][0]["assertion"] is True
assert integration.attribute(assertion) == {"product": True, "reasons": []}

(project / "test_app.py").write_text("def test_one():\n    raise RuntimeError('runner fault')\n")
runner_fault = integration.IntegrationGate(argv, cwd=project).run()
assert runner_fault.report["state"] == "parsed", runner_fault.report
assert runner_fault.report["failures"][0]["assertion"] is False
assert integration.attribute(runner_fault)["product"] is False

(project / "test_app.py").write_text("import pytest\n@pytest.fixture\ndef ready():\n    raise RuntimeError('setup fault')\ndef test_one(ready):\n    pass\n")
setup_fault = integration.IntegrationGate(argv, cwd=project).run()
assert setup_fault.report["state"] == "parsed", setup_fault.report
assert setup_fault.report["counts"]["errors"] == 1
assert integration.attribute(setup_fault)["product"] is False

print(json.dumps({"package": str(Path(quadratus.__file__).resolve()),
                  "producer": str(producer.resolve()),
                  "producer_sha256": hashlib.sha256(producer.read_bytes()).hexdigest(),
                  "assertion_product": integration.attribute(assertion)["product"],
                  "runner_fault_product": integration.attribute(runner_fault)["product"],
                  "setup_fault_product": integration.attribute(setup_fault)["product"],
                  "shadow_ignored": True}))
'''


def verify(source: Path, builder: str, runner: str) -> dict:
    source = source.resolve()
    with tempfile.TemporaryDirectory(prefix="quadratus-wheel-") as root_text:
        root = Path(root_text)
        build_source = root / "source"
        build_source.mkdir()
        shutil.copytree(source / "quadratus", build_source / "quadratus",
                        ignore=shutil.ignore_patterns("__pycache__", "*.pyc"))
        for name in ("pyproject.toml", "README.md", "multi_model_workflow.py", "chat_gui.py"):
            shutil.copy2(source / name, build_source / name)
        wheels = root / "wheels"
        subprocess.run([builder, "-m", "pip", "wheel", "--no-deps", "--no-build-isolation",
                        "--no-index", "--wheel-dir", str(wheels), str(build_source)],
                       check=True, capture_output=True, text=True)
        wheel, = wheels.glob("quadratus*.whl")
        installed = root / "installed"
        subprocess.run([builder, "-m", "pip", "install", "--no-deps", "--no-index",
                        "--target", str(installed), str(wheel)],
                       check=True, capture_output=True, text=True)
        shutil.rmtree(build_source)
        result = subprocess.run([runner, "-I", "-c", PROBE, str(installed), str(root / "project")],
                                cwd=root, check=True, capture_output=True, text=True)
        return json.loads(result.stdout)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source", type=Path, default=Path(__file__).resolve().parents[1])
    parser.add_argument("--builder-python", default=sys.executable)
    parser.add_argument("--runner-python", default=sys.executable)
    args = parser.parse_args()
    try:
        result = verify(args.source, args.builder_python, args.runner_python)
    except subprocess.CalledProcessError as exc:
        print(exc.stdout or "", file=sys.stderr)
        print(exc.stderr or "", file=sys.stderr)
        raise
    print(json.dumps(result, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

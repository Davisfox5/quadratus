"""The shipped wheel must preserve structured check attribution outside the checkout."""

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest


def test_installed_wheel_gate_producer(tmp_path):
    builder = os.environ.get("QUADRATUS_WHEEL_BUILDER")
    if not builder:
        pytest.skip("set QUADRATUS_WHEEL_BUILDER to an offline Python with setuptools and wheel")
    root = Path(__file__).resolve().parents[1]
    command = [sys.executable, str(root / "tools" / "verify_workflow_wheel.py"),
               "--source", str(root), "--builder-python", builder,
               "--runner-python", sys.executable]
    result = subprocess.run(command, cwd=tmp_path, check=True, capture_output=True, text=True)
    evidence = json.loads(result.stdout)
    assert evidence["assertion_product"] is True
    assert evidence["runner_fault_product"] is False
    assert evidence["setup_fault_product"] is False
    assert evidence["shadow_ignored"] is True
    assert len(evidence["producer_sha256"]) == 64
    assert "/installed/quadratus/" in evidence["package"]
    assert "/installed/quadratus/_gate_producer/" in evidence["producer"]

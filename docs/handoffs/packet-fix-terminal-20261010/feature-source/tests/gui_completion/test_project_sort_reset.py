import os
import re
import shutil
import subprocess

import pytest


def test_project_sort_reset_ui_suite():
    node = shutil.which("node")
    if node is None:
        pytest.skip("node not installed")

    root = os.path.dirname(os.path.dirname(os.path.dirname(__file__)))
    result = subprocess.run(
        [node, "--test", "--test-reporter=tap", "tests/gui_completion/project_sort_reset.test.js"],
        cwd=root,
        capture_output=True,
        text=True,
        timeout=60,
    )
    out = result.stdout + result.stderr
    assert result.returncode == 0, out
    assert re.search(r"^# pass 6$", out, re.M), out
    assert re.search(r"^# fail 0$", out, re.M), out

"""The gate never runs stale bytecode (Codex confirmation on #25).

CPython trusts a cached ``.pyc`` whose recorded source size and mtime match.
A same-size edit landing in the same second as the previous import reused the
old bytecode, and the gate passed broken source. Each execution now gets a
fresh private ``PYTHONPYCACHEPREFIX``. These run real interpreters and real
pytest through both seams, ``IntegrationGate`` and ``GateSuite``.
"""

import os
import sys
import tempfile
from pathlib import Path

import pytest

from quadratus import integration
from quadratus.integration import GateCommand, GateSuite, IntegrationGate

GOOD = "def add(a, b):\n    return a + b\n"
BAD = "def add(a, b):\n    return a - b\n"   # same length as GOOD
TEST = "from app import add\n\n\ndef test_add():\n    assert add(1, 2) == 3\n"
IMPORT = [sys.executable, "-c", "import app; assert app.add(1, 2) == 3; print('1 passed')"]
PYTEST = [sys.executable, "-m", "pytest", "-q", "-p", "no:cacheprovider", "tests/test_app.py"]


def _project(tmp_path):
    root = tmp_path / "project"
    (root / "tests").mkdir(parents=True)
    (root / "app.py").write_text(GOOD)
    (root / "tests" / "test_app.py").write_text(TEST)
    (root / "tests" / "conftest.py").write_text("import sys, pathlib\nsys.path.insert(0, str(pathlib.Path(__file__).parent.parent))\n")
    return root


def _stale_edit(root, text):
    """Rewrite app.py at the same size and the same mtime, as a fast edit can."""
    path = root / "app.py"
    stamp = path.stat()
    assert len(text) == stamp.st_size
    path.write_text(text)
    os.utime(path, ns=(stamp.st_atime_ns, stamp.st_mtime_ns))


def _warm_project_cache(root, argv):
    """Leave a valid cache of the good source in the project, as a prior
    ordinary run (not the gate) would."""
    import subprocess
    env = {k: v for k, v in os.environ.items() if k not in ("PYTHONPYCACHEPREFIX", "PYTHONDONTWRITEBYTECODE")}
    subprocess.run(argv, cwd=root, env=env, check=True, capture_output=True)
    assert list(root.rglob("*.pyc")), "the project now has a cache a stale read would use"


def _gate(kind, root, argv):
    if kind == "gate":
        return IntegrationGate(argv, cwd=root).run().passed
    suite = GateSuite([GateCommand("check", tuple(argv))], cwd=root, exclude=[root / "__pycache__",
                                                                                root / "tests" / "__pycache__"])
    return suite.run().passed


@pytest.mark.parametrize("kind", ["gate", "suite"])
@pytest.mark.parametrize("argv", [IMPORT, PYTEST], ids=["import", "pytest"])
def test_a_same_size_same_mtime_edit_is_seen_by_the_gate(tmp_path, kind, argv):
    root = _project(tmp_path)
    _warm_project_cache(root, argv)
    before = sorted(p.read_bytes() for p in root.rglob("*.pyc"))
    _stale_edit(root, BAD)
    assert _gate(kind, root, argv) is False, "broken source must fail, whatever the cache says"
    _stale_edit(root, GOOD)
    assert _gate(kind, root, argv) is True, "good source still passes"
    assert sorted(p.read_bytes() for p in root.rglob("*.pyc")) == before, "the project's own cache is untouched"


def test_an_inherited_prefix_is_overridden_and_left_alone(tmp_path, monkeypatch):
    root = _project(tmp_path)
    inherited = tmp_path / "inherited-prefix"
    inherited.mkdir()
    monkeypatch.setenv("PYTHONPYCACHEPREFIX", str(inherited))
    import subprocess
    subprocess.run(IMPORT, cwd=root, check=True, capture_output=True)   # warms the inherited prefix
    warmed = sorted(str(p) for p in inherited.rglob("*"))
    assert warmed
    _stale_edit(root, BAD)
    assert IntegrationGate(IMPORT, cwd=root).run().passed is False
    assert sorted(str(p) for p in inherited.rglob("*")) == warmed, "nothing read into or deleted from it"


def test_each_execution_gets_its_own_prefix_and_removes_only_that(tmp_path, monkeypatch):
    root = _project(tmp_path)
    made = []
    real = tempfile.mkdtemp

    def recording(*args, **kwargs):
        made.append(real(*args, **kwargs))
        return made[-1]
    monkeypatch.setattr(integration.tempfile, "mkdtemp", recording)
    IntegrationGate(IMPORT, cwd=root).run()
    GateSuite([GateCommand("a", tuple(IMPORT)), GateCommand("b", tuple(IMPORT))], cwd=root).run()
    assert len(made) == 3 and len(set(made)) == 3
    assert not any(Path(p).exists() for p in made)
    assert (root / "app.py").read_text() == GOOD


@pytest.mark.parametrize("argv, timeout", [
    ([sys.executable, "-c", "import time; time.sleep(5)"], 0.5),
    (["quadratus-no-such-command"], 5),
    ([sys.executable, "-c", "raise SystemExit(3)"], 5),
])
def test_the_prefix_is_removed_on_timeout_missing_command_and_failure(tmp_path, monkeypatch, argv, timeout):
    made = []
    real = tempfile.mkdtemp
    monkeypatch.setattr(integration.tempfile, "mkdtemp", lambda *a, **k: made.append(real(*a, **k)) or made[-1])
    assert IntegrationGate(argv, cwd=tmp_path, timeout=timeout).run().passed is False
    suite = GateSuite([GateCommand("x", tuple(argv), timeout=timeout)], cwd=tmp_path).run()
    assert suite.passed is False
    assert made and not any(Path(p).exists() for p in made)


def test_a_non_python_check_still_runs_with_the_private_prefix_set(tmp_path):
    result = IntegrationGate(["sh", "-c", "test -n \"$PYTHONPYCACHEPREFIX\" && echo ok"], cwd=tmp_path).run()
    assert result.passed and "ok" in result.output

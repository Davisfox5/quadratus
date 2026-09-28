"""A rewrite inside one timestamp tick is still seen (#25, deptree).

The guard caches a file's hash keyed by its lstat. On a filesystem whose
timestamps are coarser than a rewrite (a container mount with whole-second
times), a same-size rewrite in the same tick keeps that lstat, and the
cached hash hid the change: ``test_an_edited_venv_file_is_a_change`` failed
intermittently in the pinned container (Codex, 5863853232). A hash is now
cached only once the file's times are older than its read by more than
``_RACY_NS`` (git's "racily clean" rule). Coarse timestamps are emulated
inside the guard only; the rewrite is real.
"""

import time
import types

import pytest

from quadratus import deptree
from quadratus.deptree import DependencyGuard, DependencyTreeChanged
from tests.test_deptree import _watch

TICK = 10**9  # whole seconds


class _Coarse:
    """A stat result whose times only move in whole ticks."""

    def __init__(self, st):
        self._st = st

    def __getattr__(self, name):
        value = getattr(self._st, name)
        return value // TICK * TICK if name in ("st_mtime_ns", "st_ctime_ns") else value


@pytest.fixture
def coarse(monkeypatch):
    real = deptree.os.fstat
    proxy = types.SimpleNamespace(**{k: getattr(deptree.os, k) for k in dir(deptree.os) if not k.startswith("__")})
    proxy.fstat = lambda fd: _Coarse(real(fd))
    monkeypatch.setattr(deptree, "os", proxy)
    lstat = DependencyGuard._lstat
    monkeypatch.setattr(DependencyGuard, "_lstat", lambda self, entry, rel: _Coarse(lstat(self, entry, rel)))


def _venv_file(tmp_path):
    root = tmp_path / "project"
    site = root / ".venv" / "lib" / "python3.11" / "site-packages"
    site.mkdir(parents=True)
    (site / "x.py").write_text("VALUE = 1\n")
    return root, site / "x.py"


def test_a_same_size_rewrite_within_one_tick_is_a_change(tmp_path, coarse):
    root, path = _venv_file(tmp_path)
    watch = _watch(root)
    path.write_text("VALUE = 2\n")
    with pytest.raises(DependencyTreeChanged):
        watch.verify("during lead (t1)")


def test_a_racily_clean_file_is_read_again(tmp_path, coarse):
    root, _ = _venv_file(tmp_path)
    guard = DependencyGuard(root)
    first, second = guard.identity(), guard.identity()
    assert first.read_bytes > 0 and second.read_bytes == first.read_bytes, "not cached while racy"


def test_an_old_enough_file_is_still_cached(tmp_path, coarse):
    root, _ = _venv_file(tmp_path)
    guard = DependencyGuard(root, wall=lambda: time.time_ns() + 10 * TICK)
    first, second = guard.identity(), guard.identity()
    assert first.read_bytes > 0 and second.read_bytes == 0 and first.digest == second.digest

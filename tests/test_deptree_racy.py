"""A rewrite inside one timestamp tick is still seen (#25, deptree).

The guard caches a file's hash keyed by its lstat. On a filesystem whose
timestamps are coarser than a rewrite (a container mount with whole-second
times), a same-size rewrite in the same tick keeps that lstat, and the
cached hash hid the change: ``test_an_edited_venv_file_is_a_change`` failed
intermittently in the pinned container (Codex, 5863853232). An age rule on
the wall clock was not enough either: a skewed clock let a fresh file be
cached (5864022673). No hash is carried across passes now, so every
verification reads the content. Coarse timestamps and a skewed wall clock
are emulated; the rewrite is real.
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


def _wall(monkeypatch, clock):
    """Make ``clock`` the guard's wall clock: the module's, and any the guard
    itself holds (the age rule at f9573e8 read ``self.wall``)."""
    monkeypatch.setattr(deptree.time, "time_ns", clock)
    init = DependencyGuard.__init__

    def skewed(self, *args, **kw):
        init(self, *args, **kw)
        if hasattr(self, "wall"):
            self.wall = clock
    monkeypatch.setattr(DependencyGuard, "__init__", skewed)


@pytest.mark.parametrize("skew_ns", [10 * TICK, -10 * TICK])
def test_a_skewed_wall_clock_does_not_hide_a_rewrite(tmp_path, coarse, monkeypatch, skew_ns):
    """The wall clock ahead of, or behind, the file clock (5864022673)."""
    real = time.time_ns
    _wall(monkeypatch, lambda: real() + skew_ns)
    root, path = _venv_file(tmp_path)
    watch = _watch(root)
    path.write_text("VALUE = 2\n")
    with pytest.raises(DependencyTreeChanged):
        watch.verify("during lead (t1)")


def test_a_clock_stepped_back_between_passes_does_not_hide_a_rewrite(tmp_path, coarse, monkeypatch):
    now = [time.time_ns() + 10 * TICK]
    _wall(monkeypatch, lambda: now[0])
    root, path = _venv_file(tmp_path)
    watch = _watch(root)
    now[0] -= 20 * TICK
    path.write_text("VALUE = 2\n")
    with pytest.raises(DependencyTreeChanged):
        watch.verify("during lead (t1)")


def test_an_unchanged_file_verifies_on_a_coarse_filesystem(tmp_path, coarse):
    root, _ = _venv_file(tmp_path)
    watch = _watch(root)
    watch.verify("during lead (t1)")

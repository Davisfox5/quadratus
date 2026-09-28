"""Runtime-dependency tree identity (contract v2 D on #25), unit cases.

Real files on a real filesystem; only the clock is injected, for the
deadline. The whole-controller cases are in
tests/lifecycle/test_dependency_identity.py.
"""

import os
import time

import pytest

from quadratus.deptree import (
    DependencyGuard,
    DependencyIdentityUnavailable,
    DependencyTreeChanged,
    DependencyWatch,
    check_exemptions,
)


def _project(tmp_path):
    root = tmp_path / "project"
    (root / "node_modules" / "pkg").mkdir(parents=True)
    (root / "node_modules" / "pkg" / "index.js").write_text("module.exports = 'real';\n")
    (root / "app.py").write_text("x = 1\n")
    return root


def _watch(root, **kw):
    watch = DependencyWatch(DependencyGuard(root, **kw))
    watch.start()
    return watch


def test_an_unchanged_tree_verifies(tmp_path):
    root = _project(tmp_path)
    watch = _watch(root)
    (root / "app.py").write_text("x = 2\n")     # source is not the tree
    watch.verify("after a source-only edit")
    assert watch.record["status"] == "unchanged" and watch.record["roots"] == ["node_modules"]


def test_no_trees_at_all_is_an_identity_and_a_new_one_is_a_change(tmp_path):
    root = tmp_path / "project"
    root.mkdir()
    (root / "app.py").write_text("x = 1\n")
    watch = _watch(root)
    watch.verify("unchanged")
    (root / "node_modules" / "playwright").mkdir(parents=True)
    (root / "node_modules" / "playwright" / "index.js").write_text("shim\n")
    with pytest.raises(DependencyTreeChanged) as caught:
        watch.verify("during lead (t1)")
    assert "node_modules/playwright/index.js" in caught.value.changed
    assert "during lead (t1)" in str(caught.value)


def test_a_same_length_rewrite_with_mtime_restored_is_rehashed_and_caught(tmp_path):
    root = _project(tmp_path)
    target = root / "node_modules" / "pkg" / "index.js"
    before = target.stat()
    watch = _watch(root)
    time.sleep(0.01)
    target.write_text("module.exports = 'shim';\n")
    assert target.stat().st_size == before.st_size
    os.utime(target, ns=(before.st_atime_ns, before.st_mtime_ns))
    assert target.stat().st_mtime_ns == before.st_mtime_ns
    with pytest.raises(DependencyTreeChanged) as caught:
        watch.verify("during check")
    assert caught.value.changed == ["node_modules/pkg/index.js"]


def test_the_cache_skips_rereading_an_identical_file(tmp_path):
    root = _project(tmp_path)
    # Files written well before they are read: not racily clean.
    guard = DependencyGuard(root, wall=lambda: time.time_ns() + 10_000_000_000)
    first = guard.identity()
    second = guard.identity()
    assert first.read_bytes > 0 and second.read_bytes == 0 and first.digest == second.digest


@pytest.mark.parametrize("change", ["added", "retargeted"])
def test_a_symlink_inside_a_tree_is_recorded_and_never_followed(tmp_path, change):
    root = _project(tmp_path)
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "big.js").write_text("x" * 1000)
    link = root / "node_modules" / "linked"
    if change == "retargeted":
        link.symlink_to(outside)
    watch = _watch(root)
    assert watch.baseline.read_bytes == len("module.exports = 'real';\n"), "the link was not followed"
    if change == "retargeted":
        link.unlink()
        link.symlink_to(tmp_path)
    else:
        link.symlink_to(outside)
    with pytest.raises(DependencyTreeChanged) as caught:
        watch.verify("during lead (t1)")
    assert caught.value.changed == ["node_modules/linked"]


def test_a_symlinked_directory_a_tree_could_hide_behind_is_a_boundary(tmp_path):
    root = _project(tmp_path)
    elsewhere = tmp_path / "elsewhere"
    (elsewhere / "node_modules").mkdir(parents=True)
    watch = _watch(root)
    (root / "pkg").symlink_to(elsewhere)
    with pytest.raises(DependencyTreeChanged) as caught:
        watch.verify("during lead (t1)")
    assert caught.value.changed == ["pkg"]


def test_a_symlink_to_a_file_outside_any_tree_is_source_not_a_boundary(tmp_path):
    root = _project(tmp_path)
    watch = _watch(root)
    (root / "linked.py").symlink_to(root / "app.py")
    watch.verify("after a source symlink")


def test_a_new_nested_tree_and_a_deleted_tree_are_both_changes(tmp_path):
    root = _project(tmp_path)
    watch = _watch(root)
    (root / "pkg" / "node_modules").mkdir(parents=True)
    with pytest.raises(DependencyTreeChanged) as nested:
        watch.verify("nested")
    assert nested.value.changed == ["pkg/node_modules"]

    root2 = _project(tmp_path / "second")
    watch2 = _watch(root2)
    import shutil
    shutil.rmtree(root2 / "node_modules")
    with pytest.raises(DependencyTreeChanged) as deleted:
        watch2.verify("deleted")
    assert "node_modules" in deleted.value.changed


def test_an_edited_venv_file_is_a_change(tmp_path):
    root = tmp_path / "project"
    site = root / ".venv" / "lib" / "python3.11" / "site-packages"
    site.mkdir(parents=True)
    (site / "x.py").write_text("VALUE = 1\n")
    watch = _watch(root)
    (site / "x.py").write_text("VALUE = 2\n")
    with pytest.raises(DependencyTreeChanged):
        watch.verify("during lead (t1)")


def test_after_one_stop_every_later_verify_stops(tmp_path):
    root = _project(tmp_path)
    watch = _watch(root)
    (root / "node_modules" / "new.js").write_text("1")
    with pytest.raises(DependencyTreeChanged):
        watch.verify("first")
    (root / "node_modules" / "new.js").unlink()     # restoring does not un-stop
    with pytest.raises(DependencyTreeChanged) as again:
        watch.verify("at DONE")
    assert "already stopped" in str(again.value)


def test_a_failed_call_is_rechecked_without_raising_and_poisons_the_run(tmp_path):
    root = _project(tmp_path)
    watch = _watch(root)
    (root / "node_modules" / "new.js").write_text("1")
    watch.after_failure("during gate-fix (t1)", RuntimeError("the call failed"))
    assert watch.record["status"] == "changed"
    with pytest.raises(DependencyTreeChanged):
        watch.verify("before check (t1)")


def test_an_interrupt_is_recorded_unverified_without_reading(tmp_path):
    root = _project(tmp_path)
    watch = _watch(root)
    watch.after_failure("during lead (t1)", KeyboardInterrupt())
    assert watch.record["status"] == "unverified"


# -- bounds, fail closed -------------------------------------------------------------

def test_over_the_entry_bound_is_a_named_stop(tmp_path):
    root = _project(tmp_path)
    for i in range(20):
        (root / "node_modules" / f"f{i}.js").write_text("1")
    watch = DependencyWatch(DependencyGuard(root, max_entries=10))
    with pytest.raises(DependencyIdentityUnavailable, match="more than 10 entries"):
        watch.start()
    assert watch.record["status"] == "unverified"


def test_over_the_byte_bound_is_a_named_stop(tmp_path):
    root = _project(tmp_path)
    (root / "node_modules" / "big.bin").write_bytes(b"x" * 4096)
    with pytest.raises(DependencyIdentityUnavailable, match="more than 1000 bytes"):
        DependencyGuard(root, max_bytes=1000).identity()


def test_over_the_deadline_is_a_named_stop(tmp_path):
    root = _project(tmp_path)
    ticks = iter(range(0, 10_000, 50))
    with pytest.raises(DependencyIdentityUnavailable, match="longer than 120s"):
        DependencyGuard(root, clock=lambda: next(ticks)).identity()


@pytest.mark.skipif(os.geteuid() == 0, reason="root reads unreadable files")
def test_an_unreadable_file_is_a_named_stop(tmp_path):
    root = _project(tmp_path)
    target = root / "node_modules" / "pkg" / "index.js"
    target.chmod(0)
    try:
        with pytest.raises(DependencyIdentityUnavailable, match="could not read"):
            DependencyGuard(root).identity()
    finally:
        target.chmod(0o644)


def test_a_file_that_cannot_be_opened_is_a_named_stop(tmp_path, monkeypatch):
    """The same case without depending on the test's uid."""
    root = _project(tmp_path)
    real = os.open

    def refusing(path, flags, *args, **kwargs):
        if path == "index.js":
            raise PermissionError("denied")
        return real(path, flags, *args, **kwargs)
    monkeypatch.setattr(os, "open", refusing)
    with pytest.raises(DependencyIdentityUnavailable, match="could not read node_modules/pkg/index.js"):
        DependencyGuard(root).identity()


def test_an_unlistable_directory_is_a_named_stop(tmp_path, monkeypatch):
    root = _project(tmp_path)
    real = os.scandir

    def refusing(fd):
        raise PermissionError("denied")
    monkeypatch.setattr(os, "scandir", refusing)
    with pytest.raises(DependencyIdentityUnavailable, match="could not list"):
        DependencyGuard(root).identity()
    monkeypatch.setattr(os, "scandir", real)


def test_a_fifo_is_recorded_and_never_opened(tmp_path):
    root = _project(tmp_path)
    os.mkfifo(root / "node_modules" / "pipe")
    identity = DependencyGuard(root).identity()      # would block if opened
    assert identity.entries["node_modules/pipe"][0] == "other"


def test_git_and_state_directories_are_not_walked(tmp_path):
    root = _project(tmp_path)
    (root / ".git" / "node_modules").mkdir(parents=True)
    (root / ".quadratus" / "runs" / "node_modules").mkdir(parents=True)
    identity = DependencyGuard(root).identity()
    assert not any(p.startswith((".git", ".quadratus")) for p in identity.entries)


# -- operator cache exemptions -------------------------------------------------------

def test_an_exempt_cache_change_is_recorded_not_stopped(tmp_path):
    root = _project(tmp_path)
    watch = _watch(root, exempt=["node_modules/.cache"])
    (root / "node_modules" / ".cache").mkdir()
    (root / "node_modules" / ".cache" / "babel.json").write_text("{}")
    watch.verify("during check")
    assert watch.record["events"][0]["exempt_changed"] == ["node_modules/.cache",
                                                          "node_modules/.cache/babel.json"]
    assert watch.record["exemptions"] == ["node_modules/.cache"]


@pytest.mark.parametrize("path", ["node_modules", "node_modules/react", "node_modules/.bin", "/abs/node_modules/.c",
                                  "../node_modules/.cache", "src/.cache", "node_modules/./.cache", ""])
def test_an_exemption_must_be_a_hidden_cache_inside_a_tree(tmp_path, path):
    with pytest.raises(ValueError):
        check_exemptions(tmp_path, [path])


def test_an_exemption_with_a_symlinked_component_is_refused(tmp_path):
    (tmp_path / "real").mkdir()
    (tmp_path / "node_modules").mkdir()
    (tmp_path / "node_modules" / ".cache").symlink_to(tmp_path / "real")
    with pytest.raises(ValueError, match="symlinked"):
        check_exemptions(tmp_path, ["node_modules/.cache"])

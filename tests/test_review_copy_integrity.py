"""A copy-bound call's answer is accepted only for the bytes it was handed.

Codex review of 2ffa7f6 on #52: with the disposable copy writable (codex
workspace-write, so a reviewer's pytest can run), a reviewer could rewrite a
source file or a render in its copy and approve what it rewrote, while the
session records the approval against the hashes it took before the call.
Fleet now hashes the copy before the call and refuses the answer if any
handed file differs or is gone afterwards. Files the call creates are its
own scratch and are not held against it.
"""

from types import SimpleNamespace

import pytest

from quadratus.config import Settings
from quadratus.project import Project
from quadratus.runtime import Fleet, ReviewCopyAltered, _copy_altered, _copy_digests


@pytest.fixture(autouse=True)
def cli_environment(monkeypatch):
    monkeypatch.setattr("shutil.which", lambda _: "/unused/cli")
    for vendor in ("OPENAI", "CLAUDE", "GROK"):
        monkeypatch.delenv(f"QUADRATUS_CLI_ARGS_{vendor}", raising=False)


def _fleet(tmp_path, monkeypatch, during_call):
    root = tmp_path / "project"
    (root / "app").mkdir(parents=True)
    (root / "app" / "main.py").write_text("def add(a, b):\n    return a + b\n")
    fleet = Fleet(Settings(backend="cli"), project=Project(root, exclude={root / ".quadratus"}), allow_writes=True)
    seen = {}

    def in_directory(directory, *, allow_writes=False, disposable=False):
        seen.update(directory=directory, allow_writes=allow_writes, disposable=disposable)
        return SimpleNamespace()
    provider = SimpleNamespace(restricted=False, in_directory=in_directory)
    monkeypatch.setattr(fleet, "provider_for", lambda key: provider)

    def generate(model_key, view, prompt, role):
        during_call(seen["directory"])
        return "APPROVED"
    monkeypatch.setattr(fleet, "_generate", generate)
    return fleet, root, seen


def test_a_reviewer_that_rewrites_a_handed_file_is_refused(tmp_path, monkeypatch):
    def rewrite(directory):
        (directory / "app" / "main.py").write_text("def add(a, b):\n    return a - b\n")
    fleet, root, seen = _fleet(tmp_path, monkeypatch, rewrite)
    try:
        with pytest.raises(ReviewCopyAltered, match="app/main.py"):
            fleet.invoke("claude:opus", "Review this.")
    finally:
        fleet.close()
    assert seen["disposable"] is True and seen["allow_writes"] is False
    assert (root / "app" / "main.py").read_text().endswith("a + b\n"), "the project itself was never touched"


def test_a_reviewer_that_deletes_a_handed_file_is_refused(tmp_path, monkeypatch):
    fleet, root, seen = _fleet(tmp_path, monkeypatch, lambda d: (d / "app" / "main.py").unlink())
    try:
        with pytest.raises(ReviewCopyAltered, match="app/main.py"):
            fleet.invoke("claude:opus", "Review this.")
    finally:
        fleet.close()


def test_scratch_a_reviewer_creates_in_its_copy_is_not_held_against_it(tmp_path, monkeypatch):
    """pytest writes caches and the provider writes .quadratus-tmp; new files
    are the copy's purpose, and only handed files are compared."""
    def scratch(directory):
        (directory / ".quadratus-tmp").mkdir()
        (directory / ".quadratus-tmp" / "x").write_text("tmp")
        (directory / "app" / "__pycache__").mkdir()
        (directory / "app" / "__pycache__" / "main.cpython-312.pyc").write_bytes(b"\x00")
        (directory / ".pytest_cache").mkdir()
    fleet, root, seen = _fleet(tmp_path, monkeypatch, scratch)
    try:
        assert fleet.invoke("claude:opus", "Review this.") == "APPROVED"
    finally:
        fleet.close()


def test_copy_digests_and_altered_agree_on_the_handed_set(tmp_path):
    (tmp_path / "a.txt").write_text("a")
    (tmp_path / "sub").mkdir()
    (tmp_path / "sub" / "b.txt").write_text("b")
    handed = _copy_digests(tmp_path)
    assert set(handed) == {"a.txt", "sub/b.txt"}
    assert _copy_altered(tmp_path, handed) == []
    (tmp_path / "sub" / "b.txt").write_text("B")
    (tmp_path / "new.txt").write_text("new")
    assert _copy_altered(tmp_path, handed) == ["sub/b.txt"]

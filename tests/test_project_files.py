"""The harness-built file context: what goes in, what never does, and its bounds.

Codex, Run 14: each lead re-read the files its task was scoped to. These
tests pin what the harness hands over instead. They prove the prompt's
contents; whether a model then reads less is a live-run question.
"""

import hashlib

import pytest

from quadratus import project_files as pf
from quadratus.project_files import context_pack, path_refusal, render_pack


def _root(tmp_path):
    root = tmp_path / "My Project"
    (root / "src").mkdir(parents=True)
    (root / "src" / "app.py").write_text("def add(a, b):\n    return a + b\n")
    return root


@pytest.mark.parametrize("path,reason", [
    ("src/*.py", "a pattern"), ("src/[ab].py", "a pattern"), ("../outside.py", "outside the project"),
    ("/etc/passwd", "outside the project"), (".env", "hidden"), (".codex/auth.json", "hidden"),
    ("src/.cache/x.py", "hidden"), ("keys/id_ed25519", "credential"), ("config/api_token.txt", "credential"),
    ("deploy/server.pem", "credential"), ("src/missing.py", "does not exist yet"), ("src", "not a regular file"),
    ("link.py", "symlink"), ("via/app.py", "symlink"),
])
def test_what_is_never_shown(tmp_path, path, reason):
    root = _root(tmp_path)
    for name in ("keys/id_ed25519", "config/api_token.txt", "deploy/server.pem", ".env", ".codex/auth.json",
                 "src/.cache/x.py"):
        (root / name).parent.mkdir(parents=True, exist_ok=True)
        (root / name).write_text("dummy, not a real secret")
    (root / "link.py").symlink_to(root / "src" / "app.py")
    (root / "via").symlink_to(root / "src", target_is_directory=True)
    assert reason in path_refusal(root, path)
    included, refused = context_pack(root, [path])
    assert included == [] and refused == [dict(path=path, reason=path_refusal(root, path))]
    assert "dummy" not in render_pack(included, refused)


def test_an_excluded_path_is_refused(tmp_path):
    root = _root(tmp_path)
    (root / "build").mkdir()
    (root / "build" / "out.py").write_text("x = 1\n")
    assert path_refusal(root, "build/out.py", exclude=[root / "build"]) == "excluded from the project"


def test_a_binary_file_is_refused_by_content(tmp_path):
    root = _root(tmp_path)
    (root / "src" / "blob.dat").write_bytes(b"GIF89a\0\0\0")
    assert context_pack(root, ["src/blob.dat"]) == ([], [dict(path="src/blob.dat", reason="binary")])


def test_an_included_file_is_current_with_its_hash(tmp_path):
    root = _root(tmp_path)
    before, _ = context_pack(root, ["src/app.py", "./src/app.py"])
    assert len(before) == 1, "deduplicated"
    (root / "src" / "app.py").write_text("def add(a, b):\n    return b + a\n")
    after, _ = context_pack(root, ["src/app.py"])
    data = (root / "src" / "app.py").read_bytes()
    assert after[0]["sha"] == hashlib.sha256(data).hexdigest()[:12] != before[0]["sha"]
    assert "return b + a" in render_pack(after, [])
    assert after[0]["lines"] == 2 and not after[0]["truncated"]


def test_the_pack_is_bounded(tmp_path, monkeypatch):
    root = _root(tmp_path)
    monkeypatch.setattr(pf, "MAX_FILE_BYTES", 40)
    monkeypatch.setattr(pf, "MAX_TOTAL_BYTES", 70)
    monkeypatch.setattr(pf, "MAX_FILES", 3)
    for i in range(5):
        (root / "src" / f"m{i}.py").write_text("".join(f"x{j} = {j}\n" for j in range(10)))
    included, refused = context_pack(root, [f"src/m{i}.py" for i in range(5)])
    assert [e["path"] for e in included] == ["src/m0.py", "src/m1.py"]
    assert all(e["truncated"] and len(e["text"].encode()) <= 40 and e["text"].endswith("\n") for e in included)
    assert sum(len(e["text"].encode()) for e in included) <= 70
    reasons = {r["path"]: r["reason"] for r in refused}
    assert reasons["src/m2.py"] == "over the context budget for this prompt"
    assert reasons["src/m3.py"] == reasons["src/m4.py"] == reasons["src/m2.py"]
    text = render_pack(included, refused)
    assert "cut at 5 of 10 lines" in text


def test_the_pack_holds_at_most_max_files(tmp_path, monkeypatch):
    root = _root(tmp_path)
    monkeypatch.setattr(pf, "MAX_FILES", 2)
    for i in range(3):
        (root / "src" / f"m{i}.py").write_text("x = 1\n")
    included, refused = context_pack(root, [f"src/m{i}.py" for i in range(3)])
    assert [e["path"] for e in included] == ["src/m0.py", "src/m1.py"]
    assert refused == [dict(path="src/m2.py", reason="over the 2-file limit for this prompt")]


def test_nothing_to_show_renders_nothing(tmp_path):
    assert render_pack(*context_pack(_root(tmp_path), [])) == ""

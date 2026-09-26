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


@pytest.mark.parametrize("data,reason", [
    (b"GIF89a\0\0\0", "binary (contains NUL bytes)"),
    (b"x = 1\n" * 2000 + b"\0", "binary (contains NUL bytes)"),     # past the first 8,192 bytes
    (b"\xff" * 10_000, "not UTF-8 text"),                             # Codex review of 895cf67
    (b"ok = 1\n" + b"\xc3\x28\n", "not UTF-8 text"),
])
def test_non_text_contents_are_refused_whole(tmp_path, data, reason):
    root = _root(tmp_path)
    (root / "src" / "blob.dat").write_bytes(data)
    assert context_pack(root, ["src/blob.dat"]) == ([], [dict(path="src/blob.dat", reason=reason)])


class _Opens:
    """Counts every file the pack opens, by name."""

    def __init__(self, monkeypatch):
        self.names = []
        real = pf.Path.open

        def counting(path, *a, **k):
            self.names.append(path.name)
            return real(path, *a, **k)
        monkeypatch.setattr(pf.Path, "open", counting)


def test_candidates_past_the_budget_or_refused_by_name_are_never_opened(tmp_path, monkeypatch):
    root = _root(tmp_path)
    monkeypatch.setattr(pf, "MAX_TOTAL_BYTES", 60)
    (root / "src" / "big.py").write_text("x = 1\n" * 40)
    (root / "src" / "after.py").write_text("y = 2\n")
    (root / "api_token.txt").write_text("dummy")
    opens = _Opens(monkeypatch)
    included, refused = context_pack(root, ["api_token.txt", "src/big.py", "src/after.py"])
    assert [e["path"] for e in included] == ["src/big.py"]
    assert opens.names == ["big.py"], "the credential name and the over-budget file were never opened"
    assert dict(path="src/after.py", reason="over the context budget for this prompt") in refused


def test_a_file_past_the_inspection_bound_is_refused_unread(tmp_path, monkeypatch):
    root = _root(tmp_path)
    with (root / "src" / "huge.py").open("wb") as handle:
        handle.truncate(pf.MAX_INSPECT_BYTES + 1)            # sparse; never read
    opens = _Opens(monkeypatch)
    included, refused = context_pack(root, ["src/huge.py"])
    assert included == [] and opens.names == []
    assert refused == [dict(path="src/huge.py", reason="larger than the 512,000-byte inspection bound")]


def test_a_file_at_the_inspection_bound_is_read_whole_for_honest_metadata(tmp_path, monkeypatch):
    root = _root(tmp_path)
    monkeypatch.setattr(pf, "MAX_INSPECT_BYTES", 4_000)
    data = b"z = 0\n" * 600                                   # 3,600 bytes, over the 1 KB show limit
    monkeypatch.setattr(pf, "MAX_FILE_BYTES", 1_000)
    (root / "src" / "mid.py").write_bytes(data)
    (entry,), _ = context_pack(root, ["src/mid.py"])
    assert entry["lines"] == 600 and entry["sha"] == hashlib.sha256(data).hexdigest()[:12]
    assert entry["truncated"] and len(entry["text"].encode()) <= 1_000


@pytest.mark.parametrize("failure", [PermissionError, FileNotFoundError])
def test_an_unreadable_or_vanished_file_is_a_refusal_not_a_crash(tmp_path, monkeypatch, failure):
    root = _root(tmp_path)
    (root / "src" / "other.py").write_text("z = 3\n")

    def broken(path, *a, **k):
        if path.name == "app.py":
            raise failure("denied")
        return open(path, *a, **k)
    monkeypatch.setattr(pf.Path, "open", broken)
    included, refused = context_pack(root, ["src/app.py", "src/other.py"])
    assert refused == [dict(path="src/app.py", reason=f"could not be read ({failure.__name__})")]
    assert [e["path"] for e in included] == ["src/other.py"]


def test_a_metadata_failure_is_a_refusal_not_a_crash(tmp_path, monkeypatch):
    root = _root(tmp_path)
    real = pf.Path.is_symlink

    def broken(path):
        if path.name == "app.py":
            raise PermissionError("denied")
        return real(path)
    monkeypatch.setattr(pf.Path, "is_symlink", broken)
    assert path_refusal(root, "src/app.py") == "could not be inspected (PermissionError)"


def test_changed_windows_show_what_differs_not_the_prefix(tmp_path, monkeypatch):
    root = _root(tmp_path)
    monkeypatch.setattr(pf, "MAX_FILE_BYTES", 400)
    before = "".join(f"v{i} = {i}\n" for i in range(1, 201))
    after = before.replace("v150 = 150", "v150 = 'changed'").replace("v20 = 20\n", "")
    (root / "src" / "long.py").write_text(after)
    (entry,), _ = context_pack(root, ["src/long.py"], baselines={"src/long.py": before.encode()})
    assert entry["windows"] == [(14, 26), (143, 155)]   # a deletion shows the line it left behind
    assert "v150 = 'changed'" in entry["text"] and "v1 = 1\n" not in entry["text"]
    assert "showing only lines 14-26, 143-155" in render_pack([entry], [])


def test_windows_fall_back_to_a_marked_prefix_when_nothing_differs(tmp_path, monkeypatch):
    root = _root(tmp_path)
    monkeypatch.setattr(pf, "MAX_FILE_BYTES", 100)
    text = "".join(f"v{i} = {i}\n" for i in range(1, 101))
    (root / "src" / "same.py").write_text(text)
    (entry,), _ = context_pack(root, ["src/same.py"], baselines={"src/same.py": text.encode()})
    assert entry["windows"] is None and entry["truncated"] and entry["text"].startswith("v1 = 1")


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


# -- the window comparison is bounded (Codex review of 95b2cd7) -------------------------

REPEATED = ["x"] * 20_000


def _text(lines):
    return "".join(line + "\n" for line in lines)


def _with(lines, index, value):
    out = list(lines)
    out[index] = value
    return out


@pytest.mark.parametrize("old,new,expected", [
    (REPEATED, _with(REPEATED, 0, "y"), [(1, 1)]),                         # edit at the beginning
    (REPEATED, _with(REPEATED, 9_999, "y"), [(10_000, 10_000)]),           # in the middle
    (REPEATED, _with(REPEATED, 19_999, "y"), [(20_000, 20_000)]),          # at the end (Codex's probe)
    (REPEATED, REPEATED[:10_000] + ["y"] + REPEATED[10_000:], [(10_001, 10_001)]),   # an insert
    (_with(REPEATED, 9_999, "y"), REPEATED[:9_999] + REPEATED[10_000:], [(10_000, 10_000)]),  # a delete
])
def test_repeated_lines_are_located_exactly_with_bounded_work(monkeypatch, old, new, expected):
    compared = []
    real = pf.difflib.SequenceMatcher

    def recording(junk, a, b, **kw):
        compared.append(len(a) + len(b))
        return real(junk, a, b, **kw)
    monkeypatch.setattr(pf.difflib, "SequenceMatcher", recording)
    ranges, coarse = pf.changed_ranges(_text(old).encode(), _text(new))
    assert ranges == expected and coarse is False
    assert all(n <= pf.MAX_DIFF_LINES for n in compared), compared


def test_a_large_dissimilar_middle_is_one_labelled_coarse_region(monkeypatch):
    def never(*a, **k):
        raise AssertionError("no line-by-line comparison past the bound")
    monkeypatch.setattr(pf.difflib, "SequenceMatcher", never)
    old = [f"same {i}" for i in range(10)] + [f"a{i}" for i in range(2_000)] + [f"end {i}" for i in range(10)]
    new = [f"same {i}" for i in range(10)] + [f"b{i}" for i in range(2_000)] + [f"end {i}" for i in range(10)]
    ranges, coarse = pf.changed_ranges(_text(old).encode(), _text(new))
    assert ranges == [(11, 2_010)] and coarse is True


def test_a_coarse_window_says_so(tmp_path, monkeypatch):
    root = _root(tmp_path)
    monkeypatch.setattr(pf, "MAX_FILE_BYTES", 300)
    monkeypatch.setattr(pf, "MAX_DIFF_LINES", 10)
    old = [f"a{i}" for i in range(100)]
    new = [f"b{i}" for i in range(100)]
    (root / "src" / "long.py").write_text(_text(new))
    (entry,), _ = context_pack(root, ["src/long.py"], baselines={"src/long.py": _text(old).encode()})
    assert entry["coarse"] and entry["windows"][0][0] == 1
    assert "too large to compare line by line" in render_pack([entry], [])


def test_twenty_thousand_repeated_lines_finish_promptly_under_an_outside_guard(tmp_path):
    """Codex's probe did not finish in 3 s before the bound. Run in a child
    process so a regression is killed by the timeout, not left running."""
    import subprocess
    import sys
    script = (
        "import sys, time\n"
        "from pathlib import Path\n"
        "from quadratus.project_files import context_pack\n"
        "root = Path(sys.argv[1])\n"
        "(root / 'big.txt').write_text('x\\n' * 19999 + 'y\\n')\n"
        "start = time.monotonic()\n"
        "(entry,), _ = context_pack(root, ['big.txt'], baselines={'big.txt': b'x\\n' * 20000})\n"
        "assert entry['windows'] == [(19994, 20000)] and 'y' in entry['text'], entry['windows']\n"
        "print(round(time.monotonic() - start, 3))\n"
    )
    done = subprocess.run([sys.executable, "-c", script, str(tmp_path)], capture_output=True, text=True,
                          timeout=10, cwd=str(__import__("pathlib").Path(pf.__file__).parent.parent))
    assert done.returncode == 0, done.stderr
    assert float(done.stdout) < 1.0, done.stdout

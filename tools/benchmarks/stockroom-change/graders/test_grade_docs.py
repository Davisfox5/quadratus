import os
import pathlib
import subprocess
import sys

ROOT = pathlib.Path(os.getcwd())
HERE = pathlib.Path(__file__).resolve().parent


def rows():
    text = (ROOT / "docs" / "errors.md").read_text(encoding="utf-8")
    return [[cell.strip() for cell in line.strip().strip("|").split("|")]
            for line in text.splitlines() if line.strip().startswith("|")]


def test_errors_has_invalid_state():
    found = [row for row in rows() if row[0].strip("`") == "invalid_state"]
    assert len(found) == 1 and len(found[0]) == 3
    assert found[0][1].strip("`") == "400" and found[0][2]


def test_errors_keeps_the_old_rows():
    codes = {row[0].strip("`") for row in rows()}
    assert {"unknown_sku", "invalid_quantity", "insufficient_stock", "invalid_holder", "invalid_ttl",
            "unknown_reservation", "already_committed", "reservation_released", "reservation_expired",
            "invalid_json", "invalid_body", "missing_field", "unknown_field", "invalid_length",
            "invalid_id", "not_found", "method_not_allowed", "body_too_large", "usage"} <= codes


def test_readme_documents_the_store_methods():
    text = (ROOT / "README.md").read_text(encoding="utf-8")
    for word in ("extend", "adjust", "list_reservations", "invalid_state"):
        assert word in text, word
    assert "partial" in text.lower()


def test_readme_documents_the_routes():
    text = (ROOT / "README.md").read_text(encoding="utf-8")
    for word in ("/extend", "/adjust", "/reservations", "delta", "ttl_seconds"):
        assert word in text, word
    assert "/items/" in text and "holder" in text and "state" in text


def test_readme_documents_the_commands():
    text = (ROOT / "README.md").read_text(encoding="utf-8")
    for word in ("--quantity", "--holder", "--state", "extend", "adjust", "reservations"):
        assert word in text, word


def test_the_original_tests_still_pass(tmp_path):
    # The original tests find the project from their own location, so they
    # run from a folder that holds them beside links to the project's files.
    tests = tmp_path / "tests"
    tests.mkdir()
    for source in (HERE / "original").glob("orig_*.py"):
        (tests / ("test_" + source.name[5:])).write_bytes(source.read_bytes())
    (tests / "conftest.py").write_text(
        "import os, sys\nsys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))\n")
    for entry in ROOT.iterdir():
        if entry.name not in ("tests", ".quadratus", ".git", ".pytest_cache"):
            (tmp_path / entry.name).symlink_to(entry, target_is_directory=entry.is_dir())
    env = {k: v for k, v in os.environ.items() if not k.lower().endswith("_proxy")}
    done = subprocess.run(
        [sys.executable, "-m", "pytest", "tests", "-q", "-p", "no:cacheprovider", "-c", os.devnull,
         "--rootdir", str(tmp_path)],
        cwd=tmp_path, env=env, capture_output=True, text=True, timeout=600)
    assert done.returncode == 0, done.stdout[-3000:]


def test_the_project_suite_passes():
    env = {k: v for k, v in os.environ.items() if not k.lower().endswith("_proxy")}
    done = subprocess.run([sys.executable, "-m", "pytest", "-q", "-p", "no:cacheprovider"],
                          cwd=ROOT, env=env, capture_output=True, text=True, timeout=900)
    assert done.returncode == 0, done.stdout[-3000:]

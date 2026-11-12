import json
import pathlib

import pytest
from stockroom.cli import main
from stockroom.store import Store


@pytest.fixture
def db(tmp_path):
    path = str(tmp_path / "s.db")
    Store(path).add_item("A-1", "Anvil", 10)
    return path


def ok(capsys, db, *argv):
    capsys.readouterr()
    assert main(["--db", db, *argv]) == 0
    out = capsys.readouterr()
    assert out.err == "" and out.out.endswith("\n") and out.out.count("\n") == 1
    return json.loads(out.out)


def bad(capsys, db, *argv):
    capsys.readouterr()
    status = main(["--db", db, *argv])
    out = capsys.readouterr()
    assert out.out == "" and out.err.endswith("\n") and out.err.count("\n") == 1, out
    payload = json.loads(out.err)
    assert set(payload) == {"error", "message"} and payload["message"].strip()
    return status, payload["error"]


def test_reserve_show_release(capsys, db):
    r = ok(capsys, db, "reserve", "A-1", "2", "ann", "--ttl", "60")
    assert r["state"] == "active" and r["quantity"] == 2 and r["holder"] == "ann"
    assert ok(capsys, db, "show", str(r["id"])) == r
    assert ok(capsys, db, "availability", "A-1") == {"sku": "A-1", "on_hand": 10, "reserved": 2, "available": 8}
    assert ok(capsys, db, "release", str(r["id"]))["state"] == "released"


def test_commit_and_default_ttl(capsys, db):
    import time
    r = ok(capsys, db, "reserve", "A-1", "2", "ann")
    assert 800 < r["expires_at"] - time.time() <= 900
    assert ok(capsys, db, "commit", str(r["id"]))["state"] == "committed"
    assert Store(db).get_item("A-1")["quantity"] == 8


def test_store_refusals_exit_1(capsys, db):
    assert bad(capsys, db, "reserve", "nope", "1", "ann") == (1, "unknown_sku")
    assert bad(capsys, db, "reserve", "A-1", "11", "ann") == (1, "insufficient_stock")
    assert bad(capsys, db, "reserve", "A-1", "0", "ann") == (1, "invalid_quantity")
    assert bad(capsys, db, "reserve", "A-1", "1", "ann", "--ttl", "0") == (1, "invalid_ttl")
    assert bad(capsys, db, "reserve", "A-1", "1", "  ") == (1, "invalid_holder")
    assert bad(capsys, db, "show", "999") == (1, "unknown_reservation")
    assert bad(capsys, db, "release", "999") == (1, "unknown_reservation")
    assert bad(capsys, db, "commit", "999") == (1, "unknown_reservation")
    assert bad(capsys, db, "availability", "nope") == (1, "unknown_sku")
    r = ok(capsys, db, "reserve", "A-1", "1", "ann")
    ok(capsys, db, "commit", str(r["id"]))
    assert bad(capsys, db, "commit", str(r["id"])) == (1, "already_committed")


@pytest.mark.parametrize("argv", [
    ["reserve", "A-1", "two", "ann"],
    ["reserve", "A-1", "2.0", "ann"],
    ["reserve", "A-1", "2", "ann", "--ttl", "soon"],
    ["reserve", "A-1", "2"],
    ["reserve", "A-1", "2", "ann", "extra"],
    ["reserve"],
    ["release", "abc"],
    ["release"],
    ["release", "1", "2"],
    ["commit", "1.5"],
    ["commit"],
    ["show", "x"],
    ["show"],
    ["availability"],
    ["availability", "A-1", "extra"],
])
def test_usage_errors_are_json_exit_2(capsys, db, argv):
    assert bad(capsys, db, *argv) == (2, "usage")
    assert Store(db).availability("A-1")["reserved"] == 0


def test_seed_commands_unchanged(capsys, db):
    assert ok(capsys, db, "list") == {"items": [{"sku": "A-1", "name": "Anvil", "quantity": 10}]}
    assert ok(capsys, db, "add", "B-2", "Bucket", "1") == {"sku": "B-2", "name": "Bucket", "quantity": 1}
    capsys.readouterr()
    assert main(["--db", db, "add", "B-2", "Bucket", "1"]) == 1
    assert json.loads(capsys.readouterr().err)["error"] == "duplicate_sku"
    with pytest.raises(SystemExit) as stop:
        main(["--db", db, "add", "C-3", "Crate", "many"])
    assert stop.value.code == 2
    assert not capsys.readouterr().err.lstrip().startswith("{")


CODES = {
    "unknown_sku": "404", "invalid_quantity": "400", "insufficient_stock": "409", "invalid_holder": "400",
    "invalid_ttl": "400", "unknown_reservation": "404", "already_committed": "409",
    "reservation_released": "409", "reservation_expired": "409", "invalid_json": "400",
    "invalid_body": "400", "missing_field": "400", "unknown_field": "400", "invalid_length": "400",
    "invalid_id": "400", "not_found": "404", "method_not_allowed": "405", "body_too_large": "413",
}


def test_errors_table_lists_every_code_with_its_status():
    text = pathlib.Path("docs/errors.md").read_text()
    rows = {}
    for line in text.splitlines():
        cells = [c.strip().strip("`") for c in line.strip().strip("|").split("|")]
        if line.strip().startswith("|") and len(cells) >= 3:
            rows[cells[0]] = cells
    missing = [c for c in list(CODES) + ["usage"] if c not in rows]
    assert not missing, missing
    wrong = [c for c, status in CODES.items() if status not in rows[c][1]]
    assert not wrong, wrong
    assert not any(ch.isdigit() for ch in rows["usage"][1])


def test_readme_documents_the_new_surface():
    text = pathlib.Path("README.md").read_text()
    for word in ("reserve", "release", "commit", "get_reservation", "availability", "schema_version",
                 "/reservations", "/commit", "/availability", "show", "--ttl", "expired", "released",
                 "committed", "active"):
        assert word in text, word


def test_the_projects_own_suite_passes_and_grew():
    import subprocess
    import sys
    done = subprocess.run([sys.executable, "-m", "pytest", "-q", "-p", "no:cacheprovider"],
                          capture_output=True, text=True, timeout=600)
    assert done.returncode == 0, done.stdout[-800:]
    import re
    passed = int(re.search(r"(\d+) passed", done.stdout).group(1))
    assert passed >= 40, passed

import json
import time

import pytest
from stockroom.cli import main
from stockroom.store import Store


@pytest.fixture
def db(tmp_path):
    path = str(tmp_path / "s.db")
    store = Store(path)
    store.add_item("A-1", "Anvil", 10)
    store.add_item("B-2", "Bolt", 4)
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


def test_bug_reserve_default_ttl_is_900(capsys, db):
    r = ok(capsys, db, "reserve", "A-1", "2", "ann")
    assert 890 < r["expires_at"] - time.time() <= 900


def test_bug_release_of_expired_prints_it(capsys, db):
    r = Store(db).reserve("A-1", 2, "ann", 1, now=100)
    assert ok(capsys, db, "release", str(r["id"]))["state"] == "expired"


def test_bug_holder_of_64(capsys, db):
    assert ok(capsys, db, "reserve", "A-1", "1", "h" * 64)["holder"] == "h" * 64


def test_commit_quantity(capsys, db):
    r = ok(capsys, db, "reserve", "A-1", "5", "ann")
    out = ok(capsys, db, "commit", str(r["id"]), "--quantity", "2")
    assert (out["state"], out["quantity"]) == ("active", 3)
    assert ok(capsys, db, "availability", "A-1") == {"sku": "A-1", "on_hand": 8, "reserved": 3, "available": 5}
    assert ok(capsys, db, "commit", str(r["id"]))["state"] == "committed"
    assert ok(capsys, db, "availability", "A-1")["on_hand"] == 5


def test_commit_quantity_before_the_id(capsys, db):
    r = ok(capsys, db, "reserve", "A-1", "5", "ann")
    assert ok(capsys, db, "commit", "--quantity", "5", str(r["id"]))["state"] == "committed"


@pytest.mark.parametrize("value,expected", [
    ("0", (1, "invalid_quantity")),
    ("6", (1, "invalid_quantity")),
    ("x", (2, "usage")),
    ("1.5", (2, "usage")),
    ("-1", (2, "usage")),
])
def test_commit_quantity_refusals(capsys, db, value, expected):
    r = ok(capsys, db, "reserve", "A-1", "5", "ann")
    assert bad(capsys, db, "commit", str(r["id"]), "--quantity", value) == expected
    assert ok(capsys, db, "show", str(r["id"])) == r


def test_commit_quantity_without_a_value(capsys, db):
    assert bad(capsys, db, "commit", "1", "--quantity") == (2, "usage")


def test_extend(capsys, db):
    r = ok(capsys, db, "reserve", "A-1", "2", "ann", "--ttl", "60")
    out = ok(capsys, db, "extend", str(r["id"]), "5000")
    assert out["state"] == "active" and 4900 < out["expires_at"] - r["expires_at"] < 5000
    assert ok(capsys, db, "show", str(r["id"])) == out


@pytest.mark.parametrize("argv,expected", [
    (("extend", "ID", "0"), (1, "invalid_ttl")),
    (("extend", "ID", "86401"), (1, "invalid_ttl")),
    (("extend", "999", "60"), (1, "unknown_reservation")),
    (("extend", "ID", "abc"), (2, "usage")),
    (("extend", "ID", "6.0"), (2, "usage")),
    (("extend", "ID"), (2, "usage")),
    (("extend",), (2, "usage")),
    (("extend", "ID", "60", "70"), (2, "usage")),
    (("extend", "x", "60"), (2, "usage")),
])
def test_extend_refusals(capsys, db, argv, expected):
    r = ok(capsys, db, "reserve", "A-1", "2", "ann")
    argv = [str(r["id"]) if word == "ID" else word for word in argv]
    assert bad(capsys, db, *argv) == expected
    assert ok(capsys, db, "show", str(r["id"])) == r


def test_extend_refused_by_state(capsys, db):
    r = ok(capsys, db, "reserve", "A-1", "2", "ann")
    ok(capsys, db, "release", str(r["id"]))
    assert bad(capsys, db, "extend", str(r["id"]), "60") == (1, "reservation_released")


def test_adjust(capsys, db):
    assert ok(capsys, db, "adjust", "A-1", "5") == {"sku": "A-1", "on_hand": 15, "reserved": 0, "available": 15}
    assert ok(capsys, db, "adjust", "A-1", "-3") == {"sku": "A-1", "on_hand": 12, "reserved": 0, "available": 12}
    assert ok(capsys, db, "list")["items"][0]["quantity"] == 12


@pytest.mark.parametrize("argv,expected", [
    (("adjust", "A-1", "0"), (1, "invalid_quantity")),
    (("adjust", "A-1", "-11"), (1, "insufficient_stock")),
    (("adjust", "nope", "1"), (1, "unknown_sku")),
    (("adjust", "A-1", "1.5"), (2, "usage")),
    (("adjust", "A-1", "x"), (2, "usage")),
    (("adjust", "A-1", "--3"), (2, "usage")),
    (("adjust", "A-1"), (2, "usage")),
    (("adjust", "A-1", "1", "2"), (2, "usage")),
])
def test_adjust_refusals(capsys, db, argv, expected):
    assert bad(capsys, db, *argv) == expected
    assert ok(capsys, db, "availability", "A-1")["on_hand"] == 10


def test_reservations(capsys, db):
    a = ok(capsys, db, "reserve", "A-1", "1", "ann")
    b = ok(capsys, db, "reserve", "A-1", "1", "bob")
    ok(capsys, db, "reserve", "B-2", "1", "ann")
    ok(capsys, db, "release", str(b["id"]))
    out = ok(capsys, db, "reservations", "A-1")
    assert set(out) == {"reservations"}
    assert [(r["id"], r["state"]) for r in out["reservations"]] == [(a["id"], "active"), (b["id"], "released")]
    assert [r["id"] for r in ok(capsys, db, "reservations", "A-1", "--holder", "ann")["reservations"]] == [a["id"]]
    assert [r["id"] for r in ok(capsys, db, "reservations", "A-1", "--state", "released")["reservations"]] == [b["id"]]
    assert ok(capsys, db, "reservations", "A-1", "--state", "active", "--holder", "bob") == {"reservations": []}


@pytest.mark.parametrize("argv,expected", [
    (("reservations", "A-1", "--state", "gone"), (1, "invalid_state")),
    (("reservations", "nope"), (1, "unknown_sku")),
    (("reservations", "A-1", "--holder", ""), (1, "invalid_holder")),
    (("reservations",), (2, "usage")),
    (("reservations", "A-1", "B-2"), (2, "usage")),
    (("reservations", "A-1", "--state"), (2, "usage")),
    (("reservations", "A-1", "--limit", "5"), (2, "usage")),
])
def test_reservations_refusals(capsys, db, argv, expected):
    assert bad(capsys, db, *argv) == expected


def test_old_commands_unchanged(capsys, db):
    assert ok(capsys, db, "add", "C-3", "Cog", "7") == {"sku": "C-3", "name": "Cog", "quantity": 7}
    with pytest.raises(SystemExit):
        main(["--db", db, "add", "C-4", "Cog", "x"])
    capsys.readouterr()
    assert bad(capsys, db, "show", "x") == (2, "usage")
    assert bad(capsys, db, "commit", "999") == (1, "unknown_reservation")

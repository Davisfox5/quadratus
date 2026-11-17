import argparse
import json
import sqlite3
import sys

import pytest

import stockroom.store as store_module
from stockroom.cli import main
from stockroom.store import Store

NOW = 1_000_000.0


@pytest.fixture
def inventory(tmp_path, monkeypatch):
    monkeypatch.setattr(store_module.time, "time", lambda: NOW)
    path = tmp_path / "stock.db"
    store = Store(path)
    store.add_item("A-1", "Anvil", 5)
    return path, store


def snapshot(path):
    connection = sqlite3.connect(str(path))
    try:
        return tuple(connection.iterdump())
    finally:
        connection.close()


def invoke(path, arguments, capsys, status=0, code=None):
    assert main(["--db", str(path), *arguments]) == status
    output = capsys.readouterr()
    if status == 0:
        assert output.err == ""
        lines = output.out.splitlines()
    else:
        assert output.out == ""
        lines = output.err.splitlines()
    assert len(lines) == 1
    payload = json.loads(lines[0])
    assert isinstance(payload, dict)
    if status:
        assert set(payload) == {"error", "message"}
        assert payload["error"] == code
        assert isinstance(payload["message"], str) and payload["message"].strip()
    return payload




@pytest.mark.parametrize(
    "command, state, on_hand, reserved",
    [
        ("show", "active", 5, 2),
        ("release", "released", 5, 0),
        ("commit", "committed", 3, 0),
    ],
)
def test_id_command_success(inventory, capsys, command, state, on_hand, reserved):
    path, store = inventory
    reservation = store.reserve("A-1", 2, "alice", 900)
    result = invoke(path, [command, "000" + str(reservation["id"])], capsys)
    assert result == dict(reservation, state=state)
    assert store.get_reservation(reservation["id"]) == result
    assert store.availability("A-1") == {
        "sku": "A-1",
        "on_hand": on_hand,
        "reserved": reserved,
        "available": on_hand - reserved,
    }


def test_availability_success(inventory, capsys):
    path, store = inventory
    store.reserve("A-1", 2, "alice", 900)
    assert invoke(path, ["availability", "A-1"], capsys) == {
        "sku": "A-1", "on_hand": 5, "reserved": 2, "available": 3,
    }


@pytest.mark.parametrize(
    "arguments, code",
    [
        (["reserve", "absent", "1", "h"], "unknown_sku"),
        (["availability", "absent"], "unknown_sku"),
        (["reserve", "A-1", "0", "h"], "invalid_quantity"),
        (["reserve", "A-1", "6", "h"], "insufficient_stock"),
        (["reserve", "A-1", "1", ""], "invalid_holder"),
        (["reserve", "A-1", "1", "   "], "invalid_holder"),
        (["reserve", "A-1", "1", "x" * 65], "invalid_holder"),
        (["reserve", "A-1", "1", "h", "--ttl", "0"], "invalid_ttl"),
        (["reserve", "A-1", "1", "h", "--ttl", "86401"], "invalid_ttl"),
        (["reserve", "A-1", str(2**63), "h"], "insufficient_stock"),
        (["reserve", "A-1", "9" * 5000, "h"], "insufficient_stock"),
        (["reserve", "A-1", "1", "h", "--ttl", "9" * 5000], "invalid_ttl"),
    ] + [
        ([command, value], "unknown_reservation")
        for command in ("release", "commit", "show")
        for value in ("0", "999", str(2**63), "9" * 5000)
    ],
)
def test_store_refusal_changes_nothing(inventory, capsys, arguments, code):
    path, store = inventory
    store.reserve("A-1", 1, "existing", 900)
    before = snapshot(path)
    invoke(path, arguments, capsys, status=1, code=code)
    assert snapshot(path) == before


@pytest.mark.parametrize(
    "command, state, code",
    [
        ("release", "committed", "already_committed"),
        ("commit", "committed", "already_committed"),
        ("commit", "released", "reservation_released"),
        ("commit", "expired", "reservation_expired"),
    ],
)
def test_transition_refusal_changes_nothing(inventory, capsys, command, state, code):
    path, store = inventory
    reservation = store.reserve("A-1", 1, "h", 60, now=NOW - 120 if state == "expired" else NOW)
    rid = reservation["id"]
    if state == "expired":
        store.get_reservation(rid)
    elif state == "released":
        store.release(rid)
    else:
        store.commit(rid)
    before = snapshot(path)
    invoke(path, [command, str(rid)], capsys, status=1, code=code)
    assert snapshot(path) == before


def test_expiry_refusal_records_only_expiry(inventory, capsys):
    path, store = inventory
    reservation = store.reserve("A-1", 2, "h", 60, now=NOW - 120)
    before = snapshot(path)
    invoke(path, ["commit", str(reservation["id"])], capsys, 1, "reservation_expired")
    assert store.get_item("A-1")["quantity"] == 5
    assert store.get_reservation(reservation["id"]) == dict(reservation, state="expired")
    expected = tuple(line.replace("'active'", "'expired'") for line in before)
    assert snapshot(path) == expected


BAD_DECIMALS = ["", "1.0", "2e0", "1_0", " 1", "1 ", "١", "１", "+1", "-1"]


@pytest.mark.parametrize("value", BAD_DECIMALS)
@pytest.mark.parametrize("field", ["quantity", "ttl", "release", "commit", "show"])
def test_non_decimal_and_signed_values_are_usage(inventory, capsys, value, field):
    path, store = inventory
    store.reserve("A-1", 1, "existing", 900)
    if field == "quantity":
        arguments = ["reserve", "A-1", value, "h"]
    elif field == "ttl":
        arguments = ["reserve", "A-1", "1", "h", "--ttl", value]
    else:
        arguments = [field, value]
    before = snapshot(path)
    invoke(path, arguments, capsys, status=2, code="usage")
    assert snapshot(path) == before


VALID_COMMANDS = [
    ["reserve", "A-1", "1", "h"],
    ["release", "1"],
    ["commit", "1"],
    ["show", "1"],
    ["availability", "A-1"],
]


@pytest.mark.parametrize("arguments", VALID_COMMANDS)
@pytest.mark.parametrize(
    "failure", ["unopenable", "not-sqlite", "store-call", "empty-error"]
)
def test_database_errors_are_usage(inventory, capsys, monkeypatch, arguments, failure):
    path, store = inventory
    store.reserve("A-1", 1, "existing", 900)
    database = path
    if failure == "unopenable":
        # An existing file cannot serve as the parent directory of a database.
        database = path / "unopenable.db"
    elif failure == "not-sqlite":
        database = path.with_name("invalid.db")
        database.write_bytes(b"This is not an SQLite database.\n" * 10)
    else:
        method = {
            "reserve": "reserve",
            "release": "release",
            "commit": "commit",
            "show": "get_reservation",
            "availability": "availability",
        }[arguments[0]]

        def fail_store_call(self, *args, **kwargs):
            if failure == "empty-error":
                raise sqlite3.Error()
            raise sqlite3.OperationalError("attempt to write a readonly database")

        monkeypatch.setattr(Store, method, fail_store_call)

    before = path.read_bytes()
    database_before = database.read_bytes() if failure == "not-sqlite" else None
    invoke(database, arguments, capsys, status=2, code="usage")
    assert path.read_bytes() == before
    if failure == "not-sqlite":
        assert database.read_bytes() == database_before
    elif failure == "unopenable":
        assert not database.exists()


@pytest.mark.parametrize("arguments", VALID_COMMANDS)
@pytest.mark.parametrize("component", ["bad\x00path", "bad\ud800path"])
def test_invalid_database_paths_are_usage(inventory, capsys, arguments, component):
    path, store = inventory
    if "\ud800" in component:
        try:
            component.encode(sys.getfilesystemencoding(), sys.getfilesystemencodeerrors())
        except UnicodeEncodeError:
            pass
        else:
            pytest.skip("filesystem encoding accepts this surrogate")
    store.reserve("A-1", 1, "existing", 900)
    before = path.read_bytes()
    database = str(path.parent / component)
    invoke(database, arguments, capsys, status=2, code="usage")
    assert path.read_bytes() == before


@pytest.mark.parametrize(
    "arguments", [["list"], ["add", "B-2", "Bucket", "1"]]
)
def test_legacy_commands_still_raise_for_null_database_path(inventory, capsys, arguments):
    path, _ = inventory
    before = path.read_bytes()
    with pytest.raises(ValueError):
        main(["--db", str(path.parent / "bad\x00path"), *arguments])
    captured = capsys.readouterr()
    assert captured.out == ""
    assert captured.err == ""
    assert path.read_bytes() == before


@pytest.mark.parametrize(
    "arguments,method",
    [(["list"], "list_items"), (["add", "B-2", "Bucket", "1"], "add_item")],
)
def test_legacy_commands_still_raise_sqlite_errors(
    inventory, capsys, monkeypatch, arguments, method
):
    path, _ = inventory
    before = path.read_bytes()

    def fail_store_call(self, *args, **kwargs):
        raise sqlite3.OperationalError("attempt to write a readonly database")

    monkeypatch.setattr(Store, method, fail_store_call)
    with pytest.raises(sqlite3.OperationalError):
        main(["--db", str(path), *arguments])
    captured = capsys.readouterr()
    assert captured.out == ""
    assert captured.err == ""
    assert path.read_bytes() == before


@pytest.mark.parametrize("arguments", VALID_COMMANDS)
@pytest.mark.parametrize("mistake", ["missing", "extra", "unknown-option", "missing-db"])
def test_argparse_refusals_are_json(inventory, capsys, arguments, mistake):
    path, store = inventory
    store.reserve("A-1", 1, "existing", 900)
    before = snapshot(path)
    argv = ["--db", str(path), *arguments]
    if mistake == "missing":
        argv.pop()
    elif mistake == "extra":
        argv.append("extra")
    elif mistake == "unknown-option":
        argv.append("--unknown")
    else:
        argv = list(arguments)
    assert main(argv) == 2
    captured = capsys.readouterr()
    assert captured.out == ""
    assert len(captured.err.splitlines()) == 1
    error = json.loads(captured.err)
    assert set(error) == {"error", "message"}
    assert error["error"] == "usage"
    assert error["message"].strip()
    assert snapshot(path) == before


@pytest.mark.parametrize("arguments", VALID_COMMANDS)
@pytest.mark.parametrize(
    "prefix",
    [
        ["--db", "--unknown"],
        ["--db", "--db", "{db}"],
        ["--db", "-h"],
        ["--db", "--help"],
        ["-h", "--db", "{db}"],
        ["--help", "--db", "{db}"],
        ["-h"],
        ["--help"],
        ["-h=unexpected", "--db", "{db}"],
        ["--help=unexpected", "--db", "{db}"],
    ],
)
def test_early_argparse_refusals_are_json(inventory, capsys, arguments, prefix):
    path, store = inventory
    store.reserve("A-1", 1, "existing", 900)
    before = path.read_bytes()
    argv = [str(path) if value == "{db}" else value for value in prefix]
    assert main([*argv, *arguments]) == 2
    captured = capsys.readouterr()
    assert captured.out == ""
    assert len(captured.err.splitlines()) == 1
    error = json.loads(captured.err)
    assert set(error) == {"error", "message"}
    assert error["error"] == "usage"
    assert isinstance(error["message"], str) and error["message"].strip()
    assert path.read_bytes() == before


def test_missing_ttl_value_is_usage(inventory, capsys):
    path, _ = inventory
    before = snapshot(path)
    invoke(path, ["reserve", "A-1", "1", "h", "--ttl"], capsys, 2, "usage")
    assert snapshot(path) == before


def test_main_uses_sys_argv(inventory, capsys, monkeypatch):
    path, _ = inventory
    monkeypatch.setattr(sys, "argv", ["stockroom", "--db", str(path), "availability", "A-1"])
    assert main() == 0
    captured = capsys.readouterr()
    assert captured.err == ""
    assert len(captured.out.splitlines()) == 1
    assert json.loads(captured.out)["available"] == 5


def legacy_parser():
    """The original parser, retained here to pin its output and exit behavior."""
    parser = argparse.ArgumentParser(prog="stockroom")
    parser.add_argument("--db", required=True)
    commands = parser.add_subparsers(dest="command", required=True)
    commands.add_parser("list")
    add = commands.add_parser("add")
    add.add_argument("sku")
    add.add_argument("name")
    add.add_argument("quantity", type=int)
    return parser


@pytest.mark.parametrize(
    "arguments",
    [
        [],
        ["--db"],
        ["--db", "--unknown"],
        ["--db", "--unknown", "unknown", "reserve"],
        ["--db", "--unknown", "list", "reserve"],
        ["--db", "--unknown", "add", "reserve"],
        ["--db", "reserve"],
        ["--db", "reserve", "--help"],
        ["--db=reserve", "list", "extra"],
        ["--d", "reserve", "list", "extra"],
        ["--help", "--db", "reserve", "list", "extra"],
        ["--db", "unused.db"],
        ["--db", "unused.db", "unknown"],
        ["list"],
        ["add", "A", "Anvil", "1"],
        ["--db", "unused.db", "list", "extra"],
        ["--db", "unused.db", "add", "A"],
        ["--db", "unused.db", "add", "A", "Anvil", "bad"],
        ["--db", "unused.db", "add", "A", "Anvil", "1", "extra"],
        ["--db", "reserve", "list", "extra"],
        ["--help"],
        ["--db", "unused.db", "add", "--help"],
    ],
)
def test_legacy_argparse_behavior_is_identical(arguments, capsys):
    with pytest.raises(SystemExit) as original:
        legacy_parser().parse_args(arguments)
    expected = capsys.readouterr()
    with pytest.raises(SystemExit) as current:
        main(arguments)
    actual = capsys.readouterr()
    assert current.value.code == original.value.code
    assert actual.out == expected.out
    assert actual.err == expected.err


@pytest.mark.parametrize("quantity", ["+2", " 2 ", "٢", "0_2"])
def test_legacy_add_retains_int_parsing(inventory, capsys, quantity):
    path, store = inventory
    assert invoke(path, ["add", "B-2", "Bucket", quantity], capsys) == {
        "sku": "B-2", "name": "Bucket", "quantity": 2,
    }
    assert invoke(path, ["list"], capsys) == {"items": store.list_items()}


def test_legacy_add_refusal_is_unchanged(inventory, capsys):
    path, _ = inventory
    before = snapshot(path)
    invoke(path, ["add", "A-1", "Duplicate", "1"], capsys, 1, "duplicate_sku")
    assert snapshot(path) == before

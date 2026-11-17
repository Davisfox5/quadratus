import json

from stockroom.cli import main


def test_add_and_list(tmp_path, capsys):
    db = str(tmp_path / "s.db")
    assert main(["--db", db, "add", "A-1", "Anvil", "3"]) == 0
    capsys.readouterr()
    assert main(["--db", db, "list"]) == 0
    assert json.loads(capsys.readouterr().out) == {"items": [{"sku": "A-1", "name": "Anvil", "quantity": 3}]}


def test_store_refusal_is_json_on_stderr(tmp_path, capsys):
    db = str(tmp_path / "s.db")
    main(["--db", db, "add", "A-1", "Anvil", "3"])
    capsys.readouterr()
    assert main(["--db", db, "add", "A-1", "Anvil", "3"]) == 1
    captured = capsys.readouterr()
    assert captured.out == "" and json.loads(captured.err)["error"] == "duplicate_sku"

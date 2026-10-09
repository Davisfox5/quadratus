import io
import os
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import app as application


@pytest.fixture
def client(tmp_path, monkeypatch):
    """Create a test client with a temp data directory."""
    monkeypatch.setattr(application, "DATA_DIR", str(tmp_path))
    monkeypatch.setattr(application, "VIDEOS_DIR", str(tmp_path / "videos"))
    monkeypatch.setattr(application, "RECORDINGS_DIR", str(tmp_path / "recordings"))
    monkeypatch.setattr(application, "PROJECTS_FILE", str(tmp_path / "projects.json"))
    os.makedirs(tmp_path / "videos", exist_ok=True)
    os.makedirs(tmp_path / "recordings", exist_ok=True)
    application._BULK_PREVIEWS.clear()
    application.app.config["TESTING"] = True
    with application.app.test_client() as c:
        yield c
    application._BULK_PREVIEWS.clear()


def _create_project(client):
    rv = client.post("/api/projects", json={"name": "Preview"})
    assert rv.status_code == 201
    return rv.get_json()["id"]


def _post_csv(client, project_id, raw):
    return client.post(
        f"/api/projects/{project_id}/clips/import_preview",
        data={"file": (io.BytesIO(raw), "clips.csv")},
        content_type="multipart/form-data",
    )


def test_preview_bom_crlf_rows_and_does_not_save(client, tmp_path):
    pid = _create_project(client)
    before = (tmp_path / "projects.json").read_bytes()
    raw = (
        b"\xef\xbb\xbfTag Type,Start (s),End (s),Label,Notes\r\n"
        b"Pass,1.5,4.0,x,\r\n"
        b"Pass,5,3,late,\r\n"
        b"\r\n"
        b"Nope,1,2,z,\r\n"
    )
    rv = _post_csv(client, pid, raw)
    assert rv.status_code == 200
    body = rv.get_json()
    assert body["preview_only"] is True
    assert body["summary"] == {"total": 3, "valid": 1, "malformed": 2, "duplicate": 0}
    assert [row["line"] for row in body["rows"]] == [2, 3, 5]

    valid, bad_end, bad_tag = body["rows"]
    assert valid["status"] == "valid"
    assert valid["reasons"] == []
    assert valid["clip"] == {
        "tag_type": "Pass",
        "start": 1.5,
        "end": 4.0,
        "label": "x",
        "notes": "",
        "players": [],
    }
    assert bad_end["status"] == "malformed"
    assert bad_end["clip"] is None
    assert bad_end["reasons"] == ["end must be greater than start"]
    assert bad_tag["status"] == "malformed"
    assert bad_tag["clip"] is None
    assert bad_tag["reasons"]

    project = client.get(f"/api/projects/{pid}").get_json()
    assert project["clips"] == []
    assert (tmp_path / "projects.json").read_bytes() == before


def test_preview_ignores_only_rows_whose_fields_are_empty(client):
    pid = _create_project(client)
    raw = (
        "Tag Type,Start (s),End (s)\n"
        ",,\n"
        "   ,,\n"
    ).encode("utf-8")

    rv = _post_csv(client, pid, raw)

    assert rv.status_code == 200
    body = rv.get_json()
    assert body["summary"] == {"total": 1, "valid": 0, "malformed": 1, "duplicate": 0}
    assert body["rows"][0]["line"] == 3


def test_preview_missing_file(client):
    pid = _create_project(client)
    rv = client.post(
        f"/api/projects/{pid}/clips/import_preview",
        data={},
        content_type="multipart/form-data",
    )
    assert rv.status_code == 400
    body = rv.get_json()
    assert body["code"] == "bad_request"
    assert body["error"]


def test_preview_missing_required_header(client):
    pid = _create_project(client)
    rv = _post_csv(client, pid, b"Tag Type,Start (s),Label\nPass,1.5,x\n")
    assert rv.status_code == 400
    body = rv.get_json()
    assert body["code"] == "bad_request"
    assert body["error"]


def test_preview_unknown_project(client):
    rv = _post_csv(client, "missing-project", b"Tag Type,Start (s),End (s)\nPass,1,2\n")
    assert rv.status_code == 404
    assert rv.get_json()["error"] == "Project not found"


def test_preview_too_many_rows(client):
    pid = _create_project(client)
    lines = ["Tag Type,Start (s),End (s)"]
    lines.extend(f"Pass,{i},{i + 1}" for i in range(5001))
    raw = ("\n".join(lines) + "\n").encode("utf-8")
    rv = _post_csv(client, pid, raw)
    assert rv.status_code == 400
    body = rv.get_json()
    assert body["code"] == "too_many_rows"
    assert body["error"]
    assert client.get(f"/api/projects/{pid}").get_json()["clips"] == []


def test_preview_row_clip_number_and_column_rules():
    project = {"tag_types": [{"name": "Pass", "color": "#2ecc71"}]}
    columns = {"tag type": 0, "start (s)": 1, "end (s)": 2, "label": 3, "notes": 4}

    clip, reasons = application._preview_row_clip(
        project, columns, ["  Pass  ", "0", "1.25", "ok", "n"]
    )
    assert reasons == []
    assert clip == {
        "tag_type": "Pass",
        "start": 0.0,
        "end": 1.25,
        "label": "ok",
        "notes": "n",
        "players": [],
    }

    clip, reasons = application._preview_row_clip(
        project, columns, ["Pass", "-0.1", "2", "", ""]
    )
    assert clip is None
    assert "start must be a finite number >= 0" in reasons

    for bad_start in ("nan", "inf", "-inf", "nope", ""):
        clip, reasons = application._preview_row_clip(
            project, columns, ["Pass", bad_start, "2", "", ""]
        )
        assert clip is None
        assert "start must be a finite number >= 0" in reasons

    clip, reasons = application._preview_row_clip(
        project, columns, ["Pass", "1", "nan", "", ""]
    )
    assert clip is None
    assert reasons == ["end must be a finite number"]

    clip, reasons = application._preview_row_clip(project, columns, ["Pass", "1", "2"])
    assert clip is None
    assert reasons == ["wrong number of columns"]

    minimal = {"tag type": 0, "start (s)": 1, "end (s)": 2}
    clip, reasons = application._preview_row_clip(project, minimal, ["Pass", "1.5", "4"])
    assert reasons == []
    assert clip["label"] == ""
    assert clip["notes"] == ""
    assert clip["players"] == []


def test_preview_repeated_ignored_headers_keep_physical_header_width(client):
    pid = _create_project(client)
    raw = (
        "Tag Type,Start (s),End (s),Ignored, ignored \n"
        "Pass,1,2,first,second\n"
    ).encode("utf-8")

    rv = _post_csv(client, pid, raw)

    assert rv.status_code == 200
    body = rv.get_json()
    assert body["summary"] == {"total": 1, "valid": 1, "malformed": 0, "duplicate": 0}
    assert body["rows"][0]["status"] == "valid"


def test_preview_multiline_label_uses_starting_line(client, tmp_path):
    pid = _create_project(client)
    before = (tmp_path / "projects.json").read_bytes()
    # Physical lines: 1 header, 2 first row, 3-4 quoted label, 5 third row.
    raw = (
        "Tag Type,Start (s),End (s),Label\n"
        "Pass,1,2,first\n"
        "Pass,3,4,\"line one\nline two\"\n"
        "Pass,5,6,third\n"
    ).encode("utf-8")
    rv = _post_csv(client, pid, raw)
    assert rv.status_code == 200
    rows = rv.get_json()["rows"]
    assert [r["line"] for r in rows] == [2, 3, 5]
    assert rows[1]["status"] == "valid"
    assert rows[1]["clip"]["label"] == "line one\nline two"
    assert "\n" in rows[1]["clip"]["label"]

    # Blank physical line 3 is ignored, so the quoted label starts on line 4
    # and the following data row keeps its own starting physical line.
    raw_from_4 = (
        "Tag Type,Start (s),End (s),Label\n"
        "Pass,1,2,first\n"
        "\n"
        "Pass,3,4,\"starts here\nends next\"\n"
        "Pass,5,6,after\n"
    ).encode("utf-8")
    rv = _post_csv(client, pid, raw_from_4)
    assert rv.status_code == 200
    rows = rv.get_json()["rows"]
    assert [r["line"] for r in rows] == [2, 4, 6]
    assert rows[1]["clip"]["label"] == "starts here\nends next"
    assert (tmp_path / "projects.json").read_bytes() == before


def test_preview_players_resolution(client, tmp_path):
    pid = _create_project(client)
    alice = client.post(
        f"/api/projects/{pid}/players",
        json={"name": "Alice", "number": "7"},
    )
    assert alice.status_code == 201
    alice_id = alice.get_json()["id"]
    before_bob = (tmp_path / "projects.json").read_bytes()
    unknown = _post_csv(
        client,
        pid,
        b"Tag Type,Start (s),End (s),Players\nPass,1,2,Bob\n",
    )
    assert unknown.status_code == 200
    unknown_row = unknown.get_json()["rows"][0]
    assert unknown_row["status"] == "malformed"
    assert unknown_row["clip"] is None
    assert any("Bob" in reason for reason in unknown_row["reasons"])
    assert (tmp_path / "projects.json").read_bytes() == before_bob

    bob = client.post(
        f"/api/projects/{pid}/players",
        json={"name": "Bob", "number": ""},
    )
    assert bob.status_code == 201
    bob_id = bob.get_json()["id"]

    before = (tmp_path / "projects.json").read_bytes()
    raw = (
        "Tag Type,Start (s),End (s),Players\n"
        "Pass,1,2,alice; Bob\n"
        "Pass,3,4,#7\n"
        "Pass,5,6,#7 Alice\n"
        "Pass,7,8,Alice; #7\n"
        "Pass,9,10,Carol\n"
        "Pass,11,12,\n"
        "Pass,13,14,Bob\n"
    ).encode("utf-8")
    rv = _post_csv(client, pid, raw)
    assert rv.status_code == 200
    body = rv.get_json()
    rows = body["rows"]
    assert [r["line"] for r in rows] == [2, 3, 4, 5, 6, 7, 8]
    assert rows[0]["status"] == "valid"
    assert rows[0]["clip"]["players"] == [alice_id, bob_id]
    assert rows[1]["status"] == "valid"
    assert rows[1]["clip"]["players"] == [alice_id]
    assert rows[2]["status"] == "valid"
    assert rows[2]["clip"]["players"] == [alice_id]
    assert rows[3]["status"] == "valid"
    assert rows[3]["clip"]["players"] == [alice_id, alice_id]
    carol = rows[4]
    assert carol["status"] == "malformed"
    assert carol["clip"] is None
    assert any("Carol" in reason for reason in carol["reasons"])
    assert rows[5]["status"] == "valid"
    assert rows[5]["clip"]["players"] == []
    assert rows[6]["status"] == "valid"
    assert rows[6]["clip"]["players"] == [bob_id]
    assert body["summary"]["malformed"] == 1
    assert (tmp_path / "projects.json").read_bytes() == before


def _seed_clip(client, pid):
    rv = client.post(
        f"/api/projects/{pid}/clips",
        json={"tag_type": "Pass", "start": 1, "end": 2},
    )
    assert rv.status_code == 201
    return rv.get_json()["id"]


def test_preview_duplicate_same_fields(client):
    pid = _create_project(client)
    header = "Tag Type,Start (s),End (s),Label\n"
    rv = _post_csv(client, pid, (header + "Pass,1.5,4.0,x\nPass,1.5,4.0,x\n").encode())
    assert rv.status_code == 200
    body = rv.get_json()
    assert body["summary"] == {"total": 2, "valid": 1, "malformed": 0, "duplicate": 1}
    dup = body["rows"][1]
    assert dup["line"] == 3
    assert dup["status"] == "duplicate"
    assert dup["reasons"] == ["same as line 2"]
    assert dup["clip"] is None

    rv = _post_csv(
        client, pid,
        (header + "Pass,1.5,4.0,x\nPass,1.5,4.0,x\nPass,1.5,4.0,y\n").encode(),
    )
    body = rv.get_json()
    assert body["rows"][2]["status"] == "valid"
    assert body["rows"][2]["clip"]["label"] == "y"
    assert body["summary"] == {"total": 3, "valid": 2, "malformed": 0, "duplicate": 1}


def test_preview_duplicate_clip_id_existing_and_repeated(client, tmp_path):
    pid = _create_project(client)
    existing_id = _seed_clip(client, pid)
    before = (tmp_path / "projects.json").read_bytes()
    raw = (
        "Tag Type,Start (s),End (s),Label,Clip ID\n"
        f"Pass,1.5,4.0,a,{existing_id}\n"
        "Pass,2.0,5.0,b,abc\n"
        "Pass,3.0,6.0,c,abc\n"
        "Pass,4.0,7.0,d,\n"
    ).encode("utf-8")
    rv = _post_csv(client, pid, raw)
    assert rv.status_code == 200
    body = rv.get_json()
    rows = body["rows"]
    assert rows[0]["status"] == "duplicate"
    assert rows[0]["reasons"] == ["clip id already exists"]
    assert rows[0]["clip"] is None
    assert rows[1]["status"] == "valid"
    assert rows[2]["status"] == "duplicate"
    assert rows[2]["reasons"] == ["same clip id as line 3"]
    assert rows[2]["clip"] is None
    assert rows[3]["status"] == "valid"
    assert rows[3]["clip"]["label"] == "d"
    assert (tmp_path / "projects.json").read_bytes() == before


def test_preview_malformed_wins_over_duplicate(client):
    pid = _create_project(client)
    existing_id = _seed_clip(client, pid)
    raw = (
        "Tag Type,Start (s),End (s),Clip ID\n"
        f"Pass,4.0,1.5,{existing_id}\n"
    ).encode("utf-8")
    rv = _post_csv(client, pid, raw)
    assert rv.status_code == 200
    body = rv.get_json()
    assert body["rows"][0]["status"] == "malformed"
    assert body["rows"][0]["clip"] is None
    assert "end must be greater than start" in body["rows"][0]["reasons"]
    assert body["summary"]["duplicate"] == 0
    assert body["summary"]["malformed"] == 1


def test_preview_malformed_row_clip_id_still_reserved(client):
    pid = _create_project(client)
    raw = (
        "Tag Type,Start (s),End (s),Label,Notes,Players,Clip ID\n"
        "Unknown,1,3,bad,,,used\n"
        "Pass,4,5,ok,,,used\n"
    ).encode("utf-8")
    rv = _post_csv(client, pid, raw)
    assert rv.status_code == 200
    body = rv.get_json()
    rows = body["rows"]
    assert [row["status"] for row in rows] == ["malformed", "duplicate"]
    assert rows[1]["reasons"] == ["same clip id as line 2"]
    assert rows[1]["clip"] is None
    assert body["summary"] == {"total": 2, "valid": 0, "malformed": 1, "duplicate": 1}


def test_preview_duplicate_row_still_reserves_key_and_id(client):
    pid = _create_project(client)
    existing_id = _seed_clip(client, pid)
    raw = (
        "Tag Type,Start (s),End (s),Label,Notes,Players,Clip ID\n"
        f"Pass,1,3,x,,,{existing_id}\n"
        "Pass,1,3,x,,,new-id\n"
        "Pass,4,5,y,,,new-id\n"
    ).encode("utf-8")
    rv = _post_csv(client, pid, raw)
    assert rv.status_code == 200
    body = rv.get_json()
    rows = body["rows"]
    assert [row["status"] for row in rows] == ["duplicate", "duplicate", "duplicate"]
    assert rows[0]["reasons"] == ["clip id already exists"]
    assert rows[1]["reasons"] == ["same as line 2"]
    assert rows[2]["reasons"] == ["same clip id as line 3"]
    assert body["summary"] == {"total": 3, "valid": 0, "malformed": 0, "duplicate": 3}


def test_preview_malformed_player_still_reserves_comparable_key(client, tmp_path):
    """An invalid player makes the row malformed, but its times still count."""
    pid = _create_project(client)
    before = (tmp_path / "projects.json").read_bytes()
    raw = (
        "Tag Type,Start (s),End (s),Label,Notes,Players,Clip ID\n"
        "Pass,1,3,x,,Nobody,\n"
        "Pass,1,3,x,,,\n"
    ).encode("utf-8")
    rv = _post_csv(client, pid, raw)
    assert rv.status_code == 200
    body = rv.get_json()
    rows = body["rows"]
    assert rows[0]["line"] == 2
    assert rows[0]["status"] == "malformed"
    assert rows[0]["reasons"] == ["unknown player: Nobody"]
    assert rows[0]["clip"] is None
    assert rows[1]["line"] == 3
    assert rows[1]["status"] == "duplicate"
    assert rows[1]["reasons"] == ["same as line 2"]
    assert rows[1]["clip"] is None
    assert body["summary"] == {"total": 2, "valid": 0, "malformed": 1, "duplicate": 1}
    assert (tmp_path / "projects.json").read_bytes() == before


def test_preview_nonfinite_times_are_not_remembered(client):
    """Rows whose Start (s) does not parse must not reserve a duplicate key."""
    pid = _create_project(client)
    raw = (
        "Tag Type,Start (s),End (s),Label,Notes,Players,Clip ID\n"
        "Pass,abc,3,x,,,\n"
        "Pass,abc,3,x,,,\n"
    ).encode("utf-8")
    rv = _post_csv(client, pid, raw)
    assert rv.status_code == 200
    body = rv.get_json()
    rows = body["rows"]
    assert [row["status"] for row in rows] == ["malformed", "malformed"]
    assert all(row["clip"] is None for row in rows)
    assert body["summary"] == {"total": 2, "valid": 0, "malformed": 2, "duplicate": 0}


def test_preview_malformed_still_wins_when_key_already_seen(client):
    """A later malformed row stays malformed even if its key matches an earlier row."""
    pid = _create_project(client)
    raw = (
        "Tag Type,Start (s),End (s),Label,Notes,Players,Clip ID\n"
        "Pass,1,3,x,,,\n"
        "Pass,1,3,x,,,\n"
        "Pass,1,3,x,,Nobody,\n"
    ).encode("utf-8")
    rv = _post_csv(client, pid, raw)
    assert rv.status_code == 200
    body = rv.get_json()
    rows = body["rows"]
    assert [row["status"] for row in rows] == ["valid", "duplicate", "malformed"]
    assert rows[0]["line"] == 2
    assert rows[0]["clip"]["label"] == "x"
    assert rows[1]["reasons"] == ["same as line 2"]
    assert rows[1]["clip"] is None
    assert rows[2]["status"] == "malformed"
    assert rows[2]["reasons"] == ["unknown player: Nobody"]
    assert rows[2]["clip"] is None
    assert body["summary"] == {"total": 3, "valid": 1, "malformed": 1, "duplicate": 1}

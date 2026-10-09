import json
import os
import sys
import tempfile

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
    application.app.config["TESTING"] = True
    with application.app.test_client() as c:
        yield c


def test_index(client):
    rv = client.get("/")
    assert rv.status_code == 200
    assert b"GameTape" in rv.data


def test_create_and_list_projects(client):
    rv = client.post("/api/projects", json={"name": "Game 1"})
    assert rv.status_code == 201
    data = rv.get_json()
    assert data["name"] == "Game 1"
    assert len(data["tag_types"]) > 0

    rv = client.get("/api/projects")
    assert rv.status_code == 200
    projects = rv.get_json()
    assert len(projects) == 1
    assert projects[0]["name"] == "Game 1"


def test_create_project_requires_name(client):
    rv = client.post("/api/projects", json={"name": ""})
    assert rv.status_code == 400


def test_get_project(client):
    rv = client.post("/api/projects", json={"name": "Game 2"})
    pid = rv.get_json()["id"]
    rv = client.get(f"/api/projects/{pid}")
    assert rv.status_code == 200
    assert rv.get_json()["name"] == "Game 2"


def test_delete_project(client):
    rv = client.post("/api/projects", json={"name": "To Delete"})
    pid = rv.get_json()["id"]
    rv = client.delete(f"/api/projects/{pid}")
    assert rv.status_code == 200
    rv = client.get("/api/projects")
    assert len(rv.get_json()) == 0


def test_create_clip(client):
    rv = client.post("/api/projects", json={"name": "Clip Test"})
    pid = rv.get_json()["id"]

    rv = client.post(f"/api/projects/{pid}/clips", json={
        "tag_type": "Goal",
        "start": 10.0,
        "end": 15.5,
        "label": "Great goal",
        "notes": "Top corner"
    })
    assert rv.status_code == 201
    clip = rv.get_json()
    assert clip["tag_type"] == "Goal"
    assert clip["start"] == 10.0
    assert clip["end"] == 15.5


def test_clip_end_must_be_after_start(client):
    rv = client.post("/api/projects", json={"name": "Bad Clip"})
    pid = rv.get_json()["id"]

    rv = client.post(f"/api/projects/{pid}/clips", json={
        "tag_type": "Shot",
        "start": 20.0,
        "end": 10.0,
    })
    assert rv.status_code == 400


def test_list_clips(client):
    rv = client.post("/api/projects", json={"name": "List Test"})
    pid = rv.get_json()["id"]

    client.post(f"/api/projects/{pid}/clips", json={
        "tag_type": "Pass", "start": 1, "end": 3, "label": "a"
    })
    client.post(f"/api/projects/{pid}/clips", json={
        "tag_type": "Shot", "start": 5, "end": 8, "label": "b"
    })

    rv = client.get(f"/api/projects/{pid}/clips")
    assert len(rv.get_json()) == 2


def test_update_clip(client):
    rv = client.post("/api/projects", json={"name": "Update Test"})
    pid = rv.get_json()["id"]

    rv = client.post(f"/api/projects/{pid}/clips", json={
        "tag_type": "Foul", "start": 30, "end": 35, "label": "orig"
    })
    cid = rv.get_json()["id"]

    rv = client.put(f"/api/projects/{pid}/clips/{cid}", json={"label": "updated"})
    assert rv.status_code == 200
    assert rv.get_json()["label"] == "updated"


def test_delete_clip(client):
    rv = client.post("/api/projects", json={"name": "Del Clip"})
    pid = rv.get_json()["id"]

    rv = client.post(f"/api/projects/{pid}/clips", json={
        "tag_type": "Corner", "start": 40, "end": 45
    })
    cid = rv.get_json()["id"]

    rv = client.delete(f"/api/projects/{pid}/clips/{cid}")
    assert rv.status_code == 200

    rv = client.get(f"/api/projects/{pid}/clips")
    assert len(rv.get_json()) == 0


def test_update_tag_types(client):
    rv = client.post("/api/projects", json={"name": "Tags Test"})
    pid = rv.get_json()["id"]

    new_types = [{"name": "Custom", "color": "#ff0000"}]
    rv = client.put(f"/api/projects/{pid}/tag_types", json={"tag_types": new_types})
    assert rv.status_code == 200
    assert len(rv.get_json()) == 1
    assert rv.get_json()[0]["name"] == "Custom"


def test_project_not_found(client):
    assert client.get("/api/projects/nope").status_code == 404
    assert client.delete("/api/projects/nope").status_code == 404
    assert client.post("/api/projects/nope/clips", json={}).status_code == 404
    assert client.get("/api/projects/nope/clips").status_code == 404


# ── Player Tests ───────────────────────────────────────────────────────

def test_create_and_list_players(client):
    rv = client.post("/api/projects", json={"name": "Player Test"})
    pid = rv.get_json()["id"]

    rv = client.post(f"/api/projects/{pid}/players", json={
        "name": "John Doe", "number": "10"
    })
    assert rv.status_code == 201
    player = rv.get_json()
    assert player["name"] == "John Doe"
    assert player["number"] == "10"
    assert "id" in player

    rv = client.get(f"/api/projects/{pid}/players")
    assert rv.status_code == 200
    assert len(rv.get_json()) == 1


def test_create_player_requires_name(client):
    rv = client.post("/api/projects", json={"name": "P2"})
    pid = rv.get_json()["id"]

    rv = client.post(f"/api/projects/{pid}/players", json={"name": "", "number": "5"})
    assert rv.status_code == 400


def test_update_player(client):
    rv = client.post("/api/projects", json={"name": "P3"})
    pid = rv.get_json()["id"]

    rv = client.post(f"/api/projects/{pid}/players", json={"name": "Alice", "number": "7"})
    player_id = rv.get_json()["id"]

    rv = client.put(f"/api/projects/{pid}/players/{player_id}", json={
        "name": "Alice Smith", "number": "11"
    })
    assert rv.status_code == 200
    assert rv.get_json()["name"] == "Alice Smith"
    assert rv.get_json()["number"] == "11"


def test_delete_player(client):
    rv = client.post("/api/projects", json={"name": "P4"})
    pid = rv.get_json()["id"]

    rv = client.post(f"/api/projects/{pid}/players", json={"name": "Bob", "number": "3"})
    player_id = rv.get_json()["id"]

    rv = client.delete(f"/api/projects/{pid}/players/{player_id}")
    assert rv.status_code == 200

    rv = client.get(f"/api/projects/{pid}/players")
    assert len(rv.get_json()) == 0


def test_clip_with_players(client):
    rv = client.post("/api/projects", json={"name": "Clip Players"})
    pid = rv.get_json()["id"]

    # Add two players
    rv1 = client.post(f"/api/projects/{pid}/players", json={"name": "Player A", "number": "9"})
    rv2 = client.post(f"/api/projects/{pid}/players", json={"name": "Player B", "number": "5"})
    p1_id = rv1.get_json()["id"]
    p2_id = rv2.get_json()["id"]

    # Create clip with both players tagged
    rv = client.post(f"/api/projects/{pid}/clips", json={
        "tag_type": "Goal",
        "start": 10,
        "end": 15,
        "label": "Nice goal",
        "players": [p1_id, p2_id],
    })
    assert rv.status_code == 201
    clip = rv.get_json()
    assert clip["players"] == [p1_id, p2_id]

    # Update clip to only one player
    rv = client.put(f"/api/projects/{pid}/clips/{clip['id']}", json={
        "players": [p1_id],
    })
    assert rv.status_code == 200
    assert rv.get_json()["players"] == [p1_id]


def test_clip_default_empty_players(client):
    rv = client.post("/api/projects", json={"name": "No Players"})
    pid = rv.get_json()["id"]

    rv = client.post(f"/api/projects/{pid}/clips", json={
        "tag_type": "Shot", "start": 1, "end": 5
    })
    assert rv.status_code == 201
    assert rv.get_json()["players"] == []


def test_player_not_found(client):
    rv = client.post("/api/projects", json={"name": "PNF"})
    pid = rv.get_json()["id"]

    rv = client.put(f"/api/projects/{pid}/players/nonexistent", json={"name": "X"})
    assert rv.status_code == 404


# ── Export Tests ───────────────────────────────────────────────────────

def _make_project_with_clips(client):
    """Helper: create a project with players and clips for export tests."""
    rv = client.post("/api/projects", json={"name": "Export Test"})
    pid = rv.get_json()["id"]

    rv1 = client.post(f"/api/projects/{pid}/players", json={"name": "Alice", "number": "10"})
    rv2 = client.post(f"/api/projects/{pid}/players", json={"name": "Bob", "number": "7"})
    p1 = rv1.get_json()["id"]
    p2 = rv2.get_json()["id"]

    client.post(f"/api/projects/{pid}/clips", json={
        "tag_type": "Goal", "start": 10, "end": 15,
        "label": "First goal", "notes": "Header", "players": [p1],
    })
    client.post(f"/api/projects/{pid}/clips", json={
        "tag_type": "Shot", "start": 20, "end": 23,
        "label": "Wide shot", "players": [p2],
    })
    client.post(f"/api/projects/{pid}/clips", json={
        "tag_type": "Goal", "start": 55, "end": 60,
        "label": "Second goal", "players": [p1, p2],
    })
    return pid, p1, p2


def test_export_csv_all_clips(client):
    pid, _, _ = _make_project_with_clips(client)
    rv = client.get(f"/api/projects/{pid}/export/csv")
    assert rv.status_code == 200
    assert rv.content_type.startswith("text/csv")
    text = rv.data.decode("utf-8")
    lines = text.strip().split("\n")
    assert len(lines) == 4  # header + 3 clips
    assert "Goal" in text
    assert "Shot" in text
    assert "Alice" in text


def test_export_csv_filtered_by_tag_type(client):
    pid, _, _ = _make_project_with_clips(client)
    rv = client.get(f"/api/projects/{pid}/export/csv?tag_type=Goal")
    assert rv.status_code == 200
    text = rv.data.decode("utf-8")
    lines = text.strip().split("\n")
    assert len(lines) == 3  # header + 2 Goal clips
    assert "Shot" not in text.split("\n", 1)[1]  # Not in data rows


def test_export_csv_filtered_by_player(client):
    pid, p1, p2 = _make_project_with_clips(client)
    rv = client.get(f"/api/projects/{pid}/export/csv?player={p2}")
    assert rv.status_code == 200
    text = rv.data.decode("utf-8")
    lines = text.strip().split("\n")
    assert len(lines) == 3  # header + 2 clips with Bob


def test_export_csv_filtered_by_search(client):
    pid, _, _ = _make_project_with_clips(client)
    rv = client.get(f"/api/projects/{pid}/export/csv?search=wide")
    assert rv.status_code == 200
    text = rv.data.decode("utf-8")
    lines = text.strip().split("\n")
    assert len(lines) == 2  # header + 1 clip


def test_export_json_all_clips(client):
    pid, _, _ = _make_project_with_clips(client)
    rv = client.get(f"/api/projects/{pid}/export/json")
    assert rv.status_code == 200
    data = json.loads(rv.data)
    assert data["project"] == "Export Test"
    assert data["clip_count"] == 3
    assert len(data["clips"]) == 3
    # Check player info is resolved
    goal_clip = [c for c in data["clips"] if c["label"] == "First goal"][0]
    assert goal_clip["players"][0]["name"] == "Alice"
    assert "duration" in goal_clip


def test_export_json_filtered(client):
    pid, _, _ = _make_project_with_clips(client)
    rv = client.get(f"/api/projects/{pid}/export/json?tag_type=Shot")
    data = json.loads(rv.data)
    assert data["clip_count"] == 1
    assert data["clips"][0]["tag_type"] == "Shot"


def test_export_csv_project_not_found(client):
    rv = client.get("/api/projects/nope/export/csv")
    assert rv.status_code == 404


def test_export_json_project_not_found(client):
    rv = client.get("/api/projects/nope/export/json")
    assert rv.status_code == 404


def test_export_video_no_video(client):
    rv = client.post("/api/projects", json={"name": "No Video"})
    pid = rv.get_json()["id"]
    client.post(f"/api/projects/{pid}/clips", json={
        "tag_type": "Goal", "start": 1, "end": 5
    })
    rv = client.post(f"/api/projects/{pid}/export/video", json={})
    assert rv.status_code == 400
    assert "No video" in rv.get_json()["error"]


def test_export_video_no_matching_clips(client, tmp_path):
    rv = client.post("/api/projects", json={"name": "Empty Export"})
    pid = rv.get_json()["id"]
    # Create a dummy video file so the file-exists check passes
    import app as application
    video_path = os.path.join(str(tmp_path), "videos", f"{pid}.mp4")
    with open(video_path, "wb") as f:
        f.write(b"fake")
    projects = application._load_projects()
    projects[pid]["video_filename"] = f"{pid}.mp4"
    application._save_projects(projects)

    rv = client.post(f"/api/projects/{pid}/export/video", json={"tag_type": "Nonexistent"})
    assert rv.status_code == 400
    assert "No clips" in rv.get_json()["error"]


# ── Annotation Tests ──────────────────────────────────────────────────

def _make_project_with_clip(client):
    """Helper: create a project with one clip for annotation tests."""
    rv = client.post("/api/projects", json={"name": "Ann Test"})
    pid = rv.get_json()["id"]
    rv = client.post(f"/api/projects/{pid}/clips", json={
        "tag_type": "Goal", "start": 10, "end": 20, "label": "Test clip"
    })
    cid = rv.get_json()["id"]
    return pid, cid


def test_create_and_list_annotations(client):
    pid, cid = _make_project_with_clip(client)

    rv = client.post(f"/api/projects/{pid}/clips/{cid}/annotations", json={
        "type": "arrow",
        "color": "#ff0000",
        "lineWidth": 3,
        "startTime": 10,
        "endTime": 15,
        "data": {"x1": 0.1, "y1": 0.2, "x2": 0.5, "y2": 0.6},
    })
    assert rv.status_code == 201
    ann = rv.get_json()
    assert ann["type"] == "arrow"
    assert ann["color"] == "#ff0000"
    assert ann["data"]["x1"] == 0.1
    assert "id" in ann

    rv = client.get(f"/api/projects/{pid}/clips/{cid}/annotations")
    assert rv.status_code == 200
    assert len(rv.get_json()) == 1


def test_create_multiple_annotation_types(client):
    pid, cid = _make_project_with_clip(client)

    types_data = [
        {"type": "arrow", "data": {"x1": 0.1, "y1": 0.2, "x2": 0.5, "y2": 0.6}},
        {"type": "rect", "data": {"x1": 0.2, "y1": 0.3, "x2": 0.7, "y2": 0.8}},
        {"type": "circle", "data": {"cx": 0.5, "cy": 0.5, "rx": 0.1, "ry": 0.1}},
        {"type": "freehand", "data": {"points": [{"x": 0.1, "y": 0.1}, {"x": 0.3, "y": 0.4}]}},
        {"type": "text", "data": {"x": 0.5, "y": 0.5, "text": "Nice play!"}},
    ]
    for td in types_data:
        rv = client.post(f"/api/projects/{pid}/clips/{cid}/annotations", json={
            **td, "color": "#00ff00", "lineWidth": 2, "startTime": 10, "endTime": 15,
        })
        assert rv.status_code == 201, f"Failed for type {td['type']}"

    rv = client.get(f"/api/projects/{pid}/clips/{cid}/annotations")
    assert len(rv.get_json()) == 5


def test_invalid_annotation_type(client):
    pid, cid = _make_project_with_clip(client)
    rv = client.post(f"/api/projects/{pid}/clips/{cid}/annotations", json={
        "type": "invalid", "data": {}
    })
    assert rv.status_code == 400


def test_update_annotation(client):
    pid, cid = _make_project_with_clip(client)

    rv = client.post(f"/api/projects/{pid}/clips/{cid}/annotations", json={
        "type": "rect", "color": "#ff0000", "lineWidth": 3,
        "startTime": 10, "endTime": 15,
        "data": {"x1": 0.1, "y1": 0.2, "x2": 0.5, "y2": 0.6},
    })
    aid = rv.get_json()["id"]

    rv = client.put(f"/api/projects/{pid}/clips/{cid}/annotations/{aid}", json={
        "color": "#00ff00",
        "endTime": 18,
    })
    assert rv.status_code == 200
    assert rv.get_json()["color"] == "#00ff00"
    assert rv.get_json()["endTime"] == 18


def test_delete_annotation(client):
    pid, cid = _make_project_with_clip(client)

    rv = client.post(f"/api/projects/{pid}/clips/{cid}/annotations", json={
        "type": "circle", "data": {"cx": 0.5, "cy": 0.5, "rx": 0.1, "ry": 0.1},
        "startTime": 10, "endTime": 15,
    })
    aid = rv.get_json()["id"]

    rv = client.delete(f"/api/projects/{pid}/clips/{cid}/annotations/{aid}")
    assert rv.status_code == 200

    rv = client.get(f"/api/projects/{pid}/clips/{cid}/annotations")
    assert len(rv.get_json()) == 0


def test_clear_all_annotations(client):
    pid, cid = _make_project_with_clip(client)

    for i in range(3):
        client.post(f"/api/projects/{pid}/clips/{cid}/annotations", json={
            "type": "arrow", "data": {"x1": 0.1, "y1": 0.1, "x2": 0.5, "y2": 0.5},
            "startTime": 10, "endTime": 15,
        })

    rv = client.get(f"/api/projects/{pid}/clips/{cid}/annotations")
    assert len(rv.get_json()) == 3

    rv = client.delete(f"/api/projects/{pid}/clips/{cid}/annotations")
    assert rv.status_code == 200

    rv = client.get(f"/api/projects/{pid}/clips/{cid}/annotations")
    assert len(rv.get_json()) == 0


def test_annotation_not_found(client):
    pid, cid = _make_project_with_clip(client)
    rv = client.put(f"/api/projects/{pid}/clips/{cid}/annotations/nope", json={})
    assert rv.status_code == 404


def test_annotation_clip_not_found(client):
    rv = client.post("/api/projects", json={"name": "Ann404"})
    pid = rv.get_json()["id"]
    rv = client.get(f"/api/projects/{pid}/clips/nope/annotations")
    assert rv.status_code == 404
    rv = client.post(f"/api/projects/{pid}/clips/nope/annotations", json={
        "type": "arrow", "data": {}
    })
    assert rv.status_code == 404


def test_annotation_defaults(client):
    pid, cid = _make_project_with_clip(client)
    rv = client.post(f"/api/projects/{pid}/clips/{cid}/annotations", json={
        "type": "arrow", "data": {"x1": 0, "y1": 0, "x2": 1, "y2": 1},
    })
    assert rv.status_code == 201
    ann = rv.get_json()
    # Should default startTime to clip start (10) and endTime to clip end (20)
    assert ann["startTime"] == 10
    assert ann["endTime"] == 20
    assert ann["color"] == "#ff0000"
    assert ann["lineWidth"] == 3


# ── Recording Tests ───────────────────────────────────────────────────

def test_upload_and_list_recordings(client, tmp_path):
    pid, cid = _make_project_with_clip(client)

    # Upload a fake recording
    from io import BytesIO
    data = {
        "recording": (BytesIO(b"fake webm data"), "test_recording.webm"),
    }
    rv = client.post(
        f"/api/projects/{pid}/clips/{cid}/recordings",
        data=data,
        content_type="multipart/form-data",
    )
    assert rv.status_code == 201
    rec = rv.get_json()
    assert "id" in rec
    assert rec["filename"].endswith(".webm")

    # List recordings
    rv = client.get(f"/api/projects/{pid}/clips/{cid}/recordings")
    assert rv.status_code == 200
    recs = rv.get_json()
    assert len(recs) == 1
    assert recs[0]["id"] == rec["id"]


def test_upload_recording_with_duration(client, tmp_path):
    pid, cid = _make_project_with_clip(client)

    from io import BytesIO
    data = {
        "recording": (BytesIO(b"fake"), "rec.webm"),
        "duration": "45",
    }
    rv = client.post(
        f"/api/projects/{pid}/clips/{cid}/recordings",
        data=data,
        content_type="multipart/form-data",
    )
    assert rv.status_code == 201
    assert rv.get_json()["duration"] == "45"


def test_upload_recording_no_file(client):
    pid, cid = _make_project_with_clip(client)
    rv = client.post(f"/api/projects/{pid}/clips/{cid}/recordings",
                     data={}, content_type="multipart/form-data")
    assert rv.status_code == 400


def test_delete_recording(client, tmp_path):
    pid, cid = _make_project_with_clip(client)

    from io import BytesIO
    rv = client.post(
        f"/api/projects/{pid}/clips/{cid}/recordings",
        data={"recording": (BytesIO(b"data"), "rec.webm")},
        content_type="multipart/form-data",
    )
    rec_id = rv.get_json()["id"]

    rv = client.delete(f"/api/projects/{pid}/clips/{cid}/recordings/{rec_id}")
    assert rv.status_code == 200

    rv = client.get(f"/api/projects/{pid}/clips/{cid}/recordings")
    assert len(rv.get_json()) == 0


def test_delete_recording_not_found(client):
    pid, cid = _make_project_with_clip(client)
    rv = client.delete(f"/api/projects/{pid}/clips/{cid}/recordings/nope")
    assert rv.status_code == 404


def test_serve_recording(client, tmp_path):
    pid, cid = _make_project_with_clip(client)

    from io import BytesIO
    content = b"fake webm content here"
    rv = client.post(
        f"/api/projects/{pid}/clips/{cid}/recordings",
        data={"recording": (BytesIO(content), "rec.webm")},
        content_type="multipart/form-data",
    )
    filename = rv.get_json()["filename"]

    rv = client.get(f"/recordings/{filename}")
    assert rv.status_code == 200
    assert rv.data == content


def test_recording_clip_not_found(client):
    rv = client.post("/api/projects", json={"name": "Rec404"})
    pid = rv.get_json()["id"]
    rv = client.get(f"/api/projects/{pid}/clips/nope/recordings")
    assert rv.status_code == 404

    from io import BytesIO
    rv = client.post(
        f"/api/projects/{pid}/clips/nope/recordings",
        data={"recording": (BytesIO(b"x"), "r.webm")},
        content_type="multipart/form-data",
    )
    assert rv.status_code == 404


def test_clip_includes_recordings_field(client):
    """New clips should have an empty recordings array."""
    rv = client.post("/api/projects", json={"name": "RecField"})
    pid = rv.get_json()["id"]
    rv = client.post(f"/api/projects/{pid}/clips", json={
        "tag_type": "Goal", "start": 1, "end": 5
    })
    clip = rv.get_json()
    assert "recordings" in clip
    assert clip["recordings"] == []


# ── Roster Import Tests ───────────────────────────────────────────────

def test_import_roster_csv(client):
    rv = client.post("/api/projects", json={"name": "CSV Import"})
    pid = rv.get_json()["id"]

    from io import BytesIO
    csv_data = b"Name,Number\nAlice Smith,10\nBob Jones,7\nCharlie Brown,3\n"
    rv = client.post(
        f"/api/projects/{pid}/players/import",
        data={"file": (BytesIO(csv_data), "roster.csv")},
        content_type="multipart/form-data",
    )
    assert rv.status_code == 201
    data = rv.get_json()
    assert data["imported"] == 3
    assert data["players"][0]["name"] == "Alice Smith"
    assert data["players"][0]["number"] == "10"
    assert data["players"][2]["name"] == "Charlie Brown"

    # Verify they show up in the player list
    rv = client.get(f"/api/projects/{pid}/players")
    assert len(rv.get_json()) == 3


def test_import_roster_csv_auto_detect_columns(client):
    """Headers with different names should still be detected."""
    rv = client.post("/api/projects", json={"name": "AutoDetect"})
    pid = rv.get_json()["id"]

    from io import BytesIO
    csv_data = b"Jersey #,Player Name\n22,Jane Doe\n5,John Doe\n"
    rv = client.post(
        f"/api/projects/{pid}/players/import",
        data={"file": (BytesIO(csv_data), "roster.csv")},
        content_type="multipart/form-data",
    )
    assert rv.status_code == 201
    data = rv.get_json()
    assert data["imported"] == 2
    assert data["players"][0]["name"] == "Jane Doe"
    assert data["players"][0]["number"] == "22"


def test_import_roster_csv_no_header(client):
    """When no header matches, fall back to col A=name, col B=number."""
    rv = client.post("/api/projects", json={"name": "NoHeader"})
    pid = rv.get_json()["id"]

    from io import BytesIO
    csv_data = b"Alice,10\nBob,7\n"
    rv = client.post(
        f"/api/projects/{pid}/players/import",
        data={"file": (BytesIO(csv_data), "roster.csv")},
        content_type="multipart/form-data",
    )
    assert rv.status_code == 201
    data = rv.get_json()
    assert data["imported"] == 2
    assert data["players"][0]["name"] == "Alice"
    assert data["players"][0]["number"] == "10"


def test_import_roster_xlsx(client):
    import openpyxl
    from io import BytesIO

    wb = openpyxl.Workbook()
    ws = wb.active
    ws.append(["Name", "Number"])
    ws.append(["Player One", 9])
    ws.append(["Player Two", 14])
    buf = BytesIO()
    wb.save(buf)
    buf.seek(0)

    rv = client.post("/api/projects", json={"name": "XLSX Import"})
    pid = rv.get_json()["id"]

    rv = client.post(
        f"/api/projects/{pid}/players/import",
        data={"file": (buf, "roster.xlsx")},
        content_type="multipart/form-data",
    )
    assert rv.status_code == 201
    data = rv.get_json()
    assert data["imported"] == 2
    assert data["players"][0]["name"] == "Player One"
    assert data["players"][0]["number"] == "9"  # cleaned from 9.0


def test_import_roster_skips_empty_rows(client):
    from io import BytesIO
    csv_data = b"Name,Number\nAlice,10\n,,\n,\nBob,7\n"
    rv = client.post("/api/projects", json={"name": "SkipEmpty"})
    pid = rv.get_json()["id"]

    rv = client.post(
        f"/api/projects/{pid}/players/import",
        data={"file": (BytesIO(csv_data), "roster.csv")},
        content_type="multipart/form-data",
    )
    assert rv.status_code == 201
    assert rv.get_json()["imported"] == 2


def test_import_roster_no_file(client):
    rv = client.post("/api/projects", json={"name": "NoFile"})
    pid = rv.get_json()["id"]
    rv = client.post(f"/api/projects/{pid}/players/import",
                     data={}, content_type="multipart/form-data")
    assert rv.status_code == 400


def test_import_roster_unsupported_type(client):
    from io import BytesIO
    rv = client.post("/api/projects", json={"name": "BadType"})
    pid = rv.get_json()["id"]
    rv = client.post(
        f"/api/projects/{pid}/players/import",
        data={"file": (BytesIO(b"data"), "roster.pdf")},
        content_type="multipart/form-data",
    )
    assert rv.status_code == 400
    assert "Unsupported" in rv.get_json()["error"]


def test_import_roster_appends_to_existing(client):
    """Imported players should be added to existing roster, not replace it."""
    rv = client.post("/api/projects", json={"name": "Append"})
    pid = rv.get_json()["id"]

    # Add one player manually first
    client.post(f"/api/projects/{pid}/players", json={"name": "Existing", "number": "1"})

    from io import BytesIO
    csv_data = b"Name,Number\nNew Player,99\n"
    client.post(
        f"/api/projects/{pid}/players/import",
        data={"file": (BytesIO(csv_data), "roster.csv")},
        content_type="multipart/form-data",
    )

    rv = client.get(f"/api/projects/{pid}/players")
    assert len(rv.get_json()) == 2


# ── Deduplication Tests ───────────────────────────────────────────────

def test_manual_add_duplicate_rejected(client):
    """Adding a player with the same name and number should return 409."""
    rv = client.post("/api/projects", json={"name": "Dedup Manual"})
    pid = rv.get_json()["id"]

    rv = client.post(f"/api/projects/{pid}/players", json={"name": "Alice", "number": "10"})
    assert rv.status_code == 201

    rv = client.post(f"/api/projects/{pid}/players", json={"name": "Alice", "number": "10"})
    assert rv.status_code == 409
    assert "already exists" in rv.get_json()["error"]

    rv = client.get(f"/api/projects/{pid}/players")
    assert len(rv.get_json()) == 1


def test_manual_add_case_insensitive_duplicate(client):
    """Dedup should be case-insensitive on name."""
    rv = client.post("/api/projects", json={"name": "Dedup Case"})
    pid = rv.get_json()["id"]

    client.post(f"/api/projects/{pid}/players", json={"name": "Bob Jones", "number": "7"})
    rv = client.post(f"/api/projects/{pid}/players", json={"name": "bob jones", "number": "7"})
    assert rv.status_code == 409

    rv = client.get(f"/api/projects/{pid}/players")
    assert len(rv.get_json()) == 1


def test_manual_add_same_name_different_number_allowed(client):
    """Same name but different number should be allowed (e.g., traded player)."""
    rv = client.post("/api/projects", json={"name": "Dedup Diff Num"})
    pid = rv.get_json()["id"]

    rv = client.post(f"/api/projects/{pid}/players", json={"name": "Alice", "number": "10"})
    assert rv.status_code == 201
    rv = client.post(f"/api/projects/{pid}/players", json={"name": "Alice", "number": "22"})
    assert rv.status_code == 201

    rv = client.get(f"/api/projects/{pid}/players")
    assert len(rv.get_json()) == 2


def test_import_skips_duplicates_against_existing(client):
    """Import should skip players already in the roster."""
    rv = client.post("/api/projects", json={"name": "Import Dedup"})
    pid = rv.get_json()["id"]

    # Add existing players
    client.post(f"/api/projects/{pid}/players", json={"name": "Alice", "number": "10"})
    client.post(f"/api/projects/{pid}/players", json={"name": "Bob", "number": "7"})

    from io import BytesIO
    csv_data = b"Name,Number\nAlice,10\nCharlie,3\nBob,7\nDiana,5\n"
    rv = client.post(
        f"/api/projects/{pid}/players/import",
        data={"file": (BytesIO(csv_data), "roster.csv")},
        content_type="multipart/form-data",
    )
    assert rv.status_code == 201
    data = rv.get_json()
    assert data["imported"] == 2
    assert data["skipped"] == 2
    names = [p["name"] for p in data["players"]]
    assert "Charlie" in names
    assert "Diana" in names
    assert "Alice" not in names

    rv = client.get(f"/api/projects/{pid}/players")
    assert len(rv.get_json()) == 4


def test_import_skips_duplicates_within_file(client):
    """If the same player appears twice in the import file, only add once."""
    rv = client.post("/api/projects", json={"name": "File Dedup"})
    pid = rv.get_json()["id"]

    from io import BytesIO
    csv_data = b"Name,Number\nAlice,10\nBob,7\nAlice,10\n"
    rv = client.post(
        f"/api/projects/{pid}/players/import",
        data={"file": (BytesIO(csv_data), "roster.csv")},
        content_type="multipart/form-data",
    )
    assert rv.status_code == 201
    data = rv.get_json()
    assert data["imported"] == 2
    assert data["skipped"] == 1

    rv = client.get(f"/api/projects/{pid}/players")
    assert len(rv.get_json()) == 2


def test_import_case_insensitive_dedup(client):
    """Import dedup should be case-insensitive."""
    rv = client.post("/api/projects", json={"name": "Case Import"})
    pid = rv.get_json()["id"]

    client.post(f"/api/projects/{pid}/players", json={"name": "Alice Smith", "number": "10"})

    from io import BytesIO
    csv_data = b"Name,Number\nalice smith,10\nBob,7\n"
    rv = client.post(
        f"/api/projects/{pid}/players/import",
        data={"file": (BytesIO(csv_data), "roster.csv")},
        content_type="multipart/form-data",
    )
    data = rv.get_json()
    assert data["imported"] == 1
    assert data["skipped"] == 1


# ── Video Editing Tests ──────────────────────────────────────────────

def test_trim_no_video(client):
    rv = client.post("/api/projects", json={"name": "Trim No Video"})
    pid = rv.get_json()["id"]
    rv = client.post(f"/api/projects/{pid}/video/trim", json={"start": 0, "end": 10})
    assert rv.status_code == 400
    assert "No video" in rv.get_json()["error"]


def test_trim_missing_params(client):
    rv = client.post("/api/projects", json={"name": "Trim Bad"})
    pid = rv.get_json()["id"]
    # Need a video for the check to reach param validation
    import app as application
    projects = application._load_projects()
    projects[pid]["video_filename"] = "fake.mp4"
    application._save_projects(projects)
    # Create dummy file
    video_path = os.path.join(str(application.VIDEOS_DIR), "fake.mp4")
    os.makedirs(os.path.dirname(video_path), exist_ok=True)
    with open(video_path, "wb") as f:
        f.write(b"fake")

    rv = client.post(f"/api/projects/{pid}/video/trim", json={"start": 0})
    assert rv.status_code == 400
    assert "required" in rv.get_json()["error"]


def test_trim_invalid_range(client, tmp_path):
    rv = client.post("/api/projects", json={"name": "Trim Range"})
    pid = rv.get_json()["id"]
    import app as application
    video_path = os.path.join(str(tmp_path), "videos", f"{pid}.mp4")
    with open(video_path, "wb") as f:
        f.write(b"x")
    projects = application._load_projects()
    projects[pid]["video_filename"] = f"{pid}.mp4"
    application._save_projects(projects)

    rv = client.post(f"/api/projects/{pid}/video/trim", json={"start": 10, "end": 5})
    assert rv.status_code == 400
    assert "after" in rv.get_json()["error"]


def test_split_no_video(client):
    rv = client.post("/api/projects", json={"name": "Split No Video"})
    pid = rv.get_json()["id"]
    rv = client.post(f"/api/projects/{pid}/video/split", json={"split_at": 30})
    assert rv.status_code == 400


def test_split_missing_param(client, tmp_path):
    rv = client.post("/api/projects", json={"name": "Split Bad"})
    pid = rv.get_json()["id"]
    import app as application
    video_path = os.path.join(str(tmp_path), "videos", f"{pid}.mp4")
    with open(video_path, "wb") as f:
        f.write(b"x")
    projects = application._load_projects()
    projects[pid]["video_filename"] = f"{pid}.mp4"
    application._save_projects(projects)

    rv = client.post(f"/api/projects/{pid}/video/split", json={})
    assert rv.status_code == 400
    assert "required" in rv.get_json()["error"]


def test_cut_no_video(client):
    rv = client.post("/api/projects", json={"name": "Cut No Video"})
    pid = rv.get_json()["id"]
    rv = client.post(f"/api/projects/{pid}/video/cut", json={"cut_start": 10, "cut_end": 20})
    assert rv.status_code == 400


def test_cut_invalid_range(client, tmp_path):
    rv = client.post("/api/projects", json={"name": "Cut Range"})
    pid = rv.get_json()["id"]
    import app as application
    video_path = os.path.join(str(tmp_path), "videos", f"{pid}.mp4")
    with open(video_path, "wb") as f:
        f.write(b"x")
    projects = application._load_projects()
    projects[pid]["video_filename"] = f"{pid}.mp4"
    application._save_projects(projects)

    rv = client.post(f"/api/projects/{pid}/video/cut", json={"cut_start": 30, "cut_end": 10})
    assert rv.status_code == 400


def test_edit_project_not_found(client):
    rv = client.post("/api/projects/nope/video/trim", json={"start": 0, "end": 10})
    assert rv.status_code == 404
    rv = client.post("/api/projects/nope/video/split", json={"split_at": 10})
    assert rv.status_code == 404
    rv = client.post("/api/projects/nope/video/cut", json={"cut_start": 0, "cut_end": 10})
    assert rv.status_code == 404


# ── Clip Adjustment Unit Tests ────────────────────────────────────────

def test_adjust_clips_trim():
    """Clips should be shifted back when trimming removes the start."""
    import app as application
    clips = [
        {"id": "a", "start": 5, "end": 10, "tag_type": "Goal"},
        {"id": "b", "start": 15, "end": 20, "tag_type": "Shot"},
        {"id": "c", "start": 25, "end": 30, "tag_type": "Pass"},
    ]
    # Trim to 10-30 means offset = 10
    result = application._adjust_clips_after_edit(clips, time_offset=10)
    assert len(result) == 2  # clip "a" (5-10) is fully before trim, becomes negative -> clamped
    # clip "b" shifts from 15-20 to 5-10
    b = [c for c in result if c["id"] == "b"][0]
    assert b["start"] == 5
    assert b["end"] == 10
    # clip "c" shifts from 25-30 to 15-20
    c = [c for c in result if c["id"] == "c"][0]
    assert c["start"] == 15
    assert c["end"] == 20


# ── Filter Preset Tests ───────────────────────────────────────────────

def _create_preset(client, pid, name, tag_type="", player="", search=""):
    return client.post(f"/api/projects/{pid}/filter_presets", json={
        "name": name, "tag_type": tag_type, "player": player, "search": search,
    })


def test_filter_preset_crud(client):
    rv = client.post("/api/projects", json={"name": "Preset CRUD"})
    pid = rv.get_json()["id"]

    rv = _create_preset(client, pid, "Goals only", tag_type="Goal", search="header")
    assert rv.status_code == 201
    preset = rv.get_json()
    assert preset["name"] == "Goals only"
    assert preset["tag_type"] == "Goal"
    assert preset["player"] == ""
    assert preset["search"] == "header"
    assert "id" in preset
    pid_preset = preset["id"]

    rv = client.get(f"/api/projects/{pid}/filter_presets")
    assert rv.status_code == 200
    listed = rv.get_json()
    assert len(listed) == 1
    assert listed[0]["id"] == pid_preset

    rv = client.put(
        f"/api/projects/{pid}/filter_presets/{pid_preset}",
        json={"name": "Alice goals", "player": "abc123"},
    )
    assert rv.status_code == 200
    updated = rv.get_json()
    assert updated["name"] == "Alice goals"
    assert updated["player"] == "abc123"
    assert updated["tag_type"] == "Goal"
    assert updated["search"] == "header"

    rv = client.delete(f"/api/projects/{pid}/filter_presets/{pid_preset}")
    assert rv.status_code == 200
    rv = client.get(f"/api/projects/{pid}/filter_presets")
    assert rv.get_json() == []


def test_filter_preset_persists_on_disk(client):
    rv = client.post("/api/projects", json={"name": "Persist Presets"})
    pid = rv.get_json()["id"]
    rv = _create_preset(client, pid, "Shots", tag_type="Shot")
    assert rv.status_code == 201
    preset_id = rv.get_json()["id"]

    projects = application._load_projects()
    presets = projects[pid]["filter_presets"]
    assert len(presets) == 1
    assert presets[0]["id"] == preset_id
    assert presets[0]["name"] == "Shots"
    assert presets[0]["tag_type"] == "Shot"


def test_filter_presets_are_per_project(client):
    a = client.post("/api/projects", json={"name": "Game A"}).get_json()["id"]
    b = client.post("/api/projects", json={"name": "Game B"}).get_json()["id"]

    _create_preset(client, a, "A only", tag_type="Goal")
    _create_preset(client, b, "B only", tag_type="Shot")

    names_a = [p["name"] for p in client.get(f"/api/projects/{a}/filter_presets").get_json()]
    names_b = [p["name"] for p in client.get(f"/api/projects/{b}/filter_presets").get_json()]
    assert names_a == ["A only"]
    assert names_b == ["B only"]


def test_filter_preset_validation(client):
    rv = client.post("/api/projects", json={"name": "Validate"})
    pid = rv.get_json()["id"]

    rv = _create_preset(client, pid, "")
    assert rv.status_code == 400
    assert "required" in rv.get_json()["error"].lower()

    rv = _create_preset(client, pid, "   ")
    assert rv.status_code == 400

    rv = _create_preset(client, pid, "x" * 61)
    assert rv.status_code == 400
    assert "60" in rv.get_json()["error"]

    rv = _create_preset(client, pid, "x" * 60, tag_type="Goal")
    assert rv.status_code == 201

    rv = _create_preset(client, pid, "Keepers")
    assert rv.status_code == 201
    rv = _create_preset(client, pid, "keepers")
    assert rv.status_code == 409
    assert "already exists" in rv.get_json()["error"]

    rv = client.post(
        f"/api/projects/{pid}/filter_presets",
        json={"name": 12, "tag_type": "Goal"},
    )
    assert rv.status_code == 400

    rv = client.post(
        f"/api/projects/{pid}/filter_presets",
        json={"name": "Bad type", "tag_type": ["Goal"]},
    )
    assert rv.status_code == 400

    rv = client.post(
        f"/api/projects/{pid}/filter_presets",
        data="[]",
        content_type="application/json",
    )
    assert rv.status_code == 400
    assert "JSON object" in rv.get_json()["error"]

    rv = client.post(
        f"/api/projects/{pid}/filter_presets",
        data="not-json",
        content_type="application/json",
    )
    assert rv.status_code == 400


def test_filter_preset_legacy_project_without_key(client):
    rv = client.post("/api/projects", json={"name": "Legacy"})
    pid = rv.get_json()["id"]
    client.post(f"/api/projects/{pid}/players", json={"name": "Pat", "number": "4"})
    client.post(f"/api/projects/{pid}/clips", json={
        "tag_type": "Goal", "start": 1, "end": 2, "label": "legacy clip",
    })

    projects = application._load_projects()
    assert "filter_presets" not in projects[pid]
    clips_before = json.loads(json.dumps(projects[pid]["clips"]))
    players_before = json.loads(json.dumps(projects[pid]["players"]))

    rv = client.get(f"/api/projects/{pid}/filter_presets")
    assert rv.status_code == 200
    assert rv.get_json() == []

    # Listing must not persist a presets key or mutate clips/players.
    reloaded = application._load_projects()
    assert "filter_presets" not in reloaded[pid]
    assert reloaded[pid]["clips"] == clips_before
    assert reloaded[pid]["players"] == players_before

    rv = _create_preset(client, pid, "From legacy", tag_type="Goal")
    assert rv.status_code == 201
    saved = application._load_projects()
    assert saved[pid]["clips"] == clips_before
    assert saved[pid]["players"] == players_before
    assert len(saved[pid]["filter_presets"]) == 1


def test_filter_preset_does_not_mutate_clips_or_players(client):
    pid, p1, p2 = _make_project_with_clips(client)
    clips_before = client.get(f"/api/projects/{pid}/clips").get_json()
    players_before = client.get(f"/api/projects/{pid}/players").get_json()

    rv = _create_preset(client, pid, "Keep roster", tag_type="Goal", player=p1)
    assert rv.status_code == 201
    preset_id = rv.get_json()["id"]
    client.put(
        f"/api/projects/{pid}/filter_presets/{preset_id}",
        json={"name": "Still keep roster"},
    )
    client.delete(f"/api/projects/{pid}/filter_presets/{preset_id}")

    assert client.get(f"/api/projects/{pid}/clips").get_json() == clips_before
    assert client.get(f"/api/projects/{pid}/players").get_json() == players_before


def test_filter_preset_keeps_stale_player_and_tag(client):
    rv = client.post("/api/projects", json={"name": "Stale refs"})
    pid = rv.get_json()["id"]
    player = client.post(
        f"/api/projects/{pid}/players", json={"name": "Gone", "number": "9"}
    ).get_json()
    client.post(f"/api/projects/{pid}/clips", json={
        "tag_type": "Goal", "start": 1, "end": 3, "players": [player["id"]],
    })

    rv = _create_preset(
        client, pid, "Gone player goals", tag_type="Goal", player=player["id"]
    )
    preset_id = rv.get_json()["id"]

    client.delete(f"/api/projects/{pid}/players/{player['id']}")
    client.put(f"/api/projects/{pid}/tag_types", json={
        "tag_types": [{"name": "Shot", "color": "#3498db"}],
    })

    stored = client.get(f"/api/projects/{pid}/filter_presets").get_json()[0]
    assert stored["id"] == preset_id
    assert stored["player"] == player["id"]
    assert stored["tag_type"] == "Goal"

    # Applying the stored values must still filter — not broaden to all clips.
    csv_text = client.get(
        f"/api/projects/{pid}/export/csv?tag_type={stored['tag_type']}&player={stored['player']}"
    ).data.decode("utf-8")
    assert csv_text.strip().count("\n") == 1  # Header plus the original matching clip.
    # Player was removed from roster but clip still tags that id, so 1 data row.
    assert "Goal" in csv_text


def test_filter_preset_put_partial_and_not_found(client):
    rv = client.post("/api/projects", json={"name": "Partial"})
    pid = rv.get_json()["id"]
    created = _create_preset(
        client, pid, "Original", tag_type="Pass", player="p1", search="note"
    ).get_json()

    rv = client.put(
        f"/api/projects/{pid}/filter_presets/{created['id']}",
        json={"name": "Original"},
    )
    assert rv.status_code == 200
    body = rv.get_json()
    assert body["tag_type"] == "Pass"
    assert body["player"] == "p1"
    assert body["search"] == "note"

    rv = client.put(
        f"/api/projects/{pid}/filter_presets/nope",
        json={"name": "X"},
    )
    assert rv.status_code == 404
    rv = client.delete(f"/api/projects/{pid}/filter_presets/nope")
    assert rv.status_code == 404
    assert client.get("/api/projects/nope/filter_presets").status_code == 404
    assert client.post(
        "/api/projects/nope/filter_presets", json={"name": "X"}
    ).status_code == 404


def test_filter_preset_search_whitespace_stripped(client):
    pid, _, _ = _make_project_with_clips(client)
    rv = _create_preset(client, pid, "Wide shots", search="  wide  ")
    assert rv.status_code == 201
    assert rv.get_json()["search"] == "wide"

    stored = rv.get_json()["search"]
    csv_rv = client.get(f"/api/projects/{pid}/export/csv?search={stored}")
    json_rv = client.get(f"/api/projects/{pid}/export/json?search={stored}")
    csv_lines = csv_rv.data.decode("utf-8").strip().split("\n")
    data = json.loads(json_rv.data)
    assert len(csv_lines) == 2
    assert data["clip_count"] == 1
    assert data["clips"][0]["label"] == "Wide shot"


def test_filter_preset_matches_csv_json_export(client):
    pid, p1, p2 = _make_project_with_clips(client)
    rv = _create_preset(
        client, pid, "Alice first goal",
        tag_type="Goal", player=p1, search="first",
    )
    assert rv.status_code == 201
    preset = rv.get_json()

    qs = (
        f"tag_type={preset['tag_type']}"
        f"&player={preset['player']}"
        f"&search={preset['search']}"
    )
    csv_rv = client.get(f"/api/projects/{pid}/export/csv?{qs}")
    json_rv = client.get(f"/api/projects/{pid}/export/json?{qs}")
    assert csv_rv.status_code == 200
    assert json_rv.status_code == 200

    csv_lines = csv_rv.data.decode("utf-8").strip().split("\n")
    data = json.loads(json_rv.data)
    assert len(csv_lines) == 2  # header + 1 clip
    assert data["clip_count"] == 1
    assert data["clips"][0]["label"] == "First goal"
    assert data["clips"][0]["tag_type"] == "Goal"
    assert "First goal" in csv_rv.data.decode("utf-8")
    assert "Second goal" not in csv_rv.data.decode("utf-8")
    assert "Wide shot" not in csv_rv.data.decode("utf-8")

    unfiltered = json.loads(
        client.get(f"/api/projects/{pid}/export/json").data
    )
    assert unfiltered["clip_count"] == 3

"""Published validation checks for the CSV clip-manifest import preview (examiner bundle).

Run against the solver's output tree:

    GAMETAPE_ROOT=/path/to/solver/tree python -m pytest -q test_manifest_preview.py

A missing route or element fails; nothing here skips. The contract these
checks enforce is in README.md next to this file and is part of the brief.
"""

from __future__ import annotations

import hashlib
import io
import json
import os
import sys
from pathlib import Path

import pytest

ROOT = Path(os.environ["GAMETAPE_ROOT"]).resolve()
sys.path.insert(0, str(ROOT))
import app as application  # noqa: E402

FIXTURES = Path(__file__).resolve().parent / "fixtures"
PREVIEW = "/api/projects/{pid}/clips/import_preview"


@pytest.fixture
def client(tmp_path, monkeypatch):
    monkeypatch.setattr(application, "DATA_DIR", str(tmp_path))
    monkeypatch.setattr(application, "VIDEOS_DIR", str(tmp_path / "videos"))
    monkeypatch.setattr(application, "RECORDINGS_DIR", str(tmp_path / "recordings"))
    monkeypatch.setattr(application, "PROJECTS_FILE", str(tmp_path / "projects.json"))
    os.makedirs(tmp_path / "videos", exist_ok=True)
    os.makedirs(tmp_path / "recordings", exist_ok=True)
    application.app.config["TESTING"] = True
    with application.app.test_client() as c:
        yield c


@pytest.fixture
def project(client):
    pid = client.post("/api/projects", json={"name": "Manifest QA"}).get_json()["id"]
    assert client.put(f"/api/projects/{pid}/tag_types", json={"tag_types": [
        {"name": "Pass", "color": "#2ecc71"}, {"name": "Shot", "color": "#3498db"},
        {"name": "Goal", "color": "#e74c3c"}]}).status_code == 200
    player = client.post(f"/api/projects/{pid}/players",
                         json={"name": "Alex QA", "number": "9"}).get_json()
    existing = client.post(f"/api/projects/{pid}/clips", json={
        "tag_type": "Pass", "start": 100, "end": 101, "label": "stored"}).get_json()
    projects = application._load_projects()
    existing_clip = next(c for c in projects[pid]["clips"] if c["id"] == existing["id"])
    existing_clip["id"] = "EXISTING"
    application._save_projects(projects)
    return {"id": pid, "player_id": player["id"]}


def _store_hash():
    return hashlib.sha256(Path(application.PROJECTS_FILE).read_bytes()).hexdigest()


def _post(client, pid, name, filename=None):
    data = {"file": (io.BytesIO((FIXTURES / name).read_bytes()), filename or name)}
    return client.post(PREVIEW.format(pid=pid), data=data, content_type="multipart/form-data")


def _by_line(body):
    return {row["line"]: row for row in body["rows"]}


# -- happy path ---------------------------------------------------------------


def test_valid_manifest_previews_every_row_and_writes_nothing(client, project):
    before = _store_hash()
    rv = _post(client, project["id"], "valid.csv")
    assert rv.status_code == 200, rv.get_data(as_text=True)
    body = rv.get_json()
    assert body["preview_only"] is True
    assert body["summary"] == {"total": 3, "valid": 3, "malformed": 0, "duplicate": 0}
    rows = _by_line(body)
    assert set(rows) == {2, 3, 4}
    assert rows[2]["clip"] == {"tag_type": "Pass", "start": 1.5, "end": 4.0, "label": "Build-up",
                               "notes": "", "players": [project["player_id"]]}
    assert rows[3]["clip"]["players"] == [project["player_id"]], "#number name must resolve"
    assert rows[3]["clip"]["notes"] == "keep"
    assert rows[4]["clip"]["players"] == []
    assert all(row["status"] == "valid" and row["reasons"] == [] for row in body["rows"])
    assert _store_hash() == before, "preview must not write the project store"
    assert len(client.get(f"/api/projects/{project['id']}").get_json()["clips"]) == 1


def test_bom_crlf_and_quoted_commas_are_read_correctly(client, project):
    body = _post(client, project["id"], "bom_crlf.csv").get_json()
    assert body["summary"]["valid"] == 2 and body["summary"]["malformed"] == 0
    rows = _by_line(body)
    assert rows[2]["clip"]["label"] == "Left, then right"
    assert rows[3]["clip"]["label"] == 'Quote "inside" label'


def test_header_matching_is_case_and_space_insensitive(client, project):
    text = " tag type , START (S),end (s) ,label\nPass,1,2,x\n"
    data = {"file": (io.BytesIO(text.encode()), "loose.csv")}
    rv = client.post(PREVIEW.format(pid=project["id"]), data=data, content_type="multipart/form-data")
    assert rv.status_code == 200
    assert rv.get_json()["summary"]["valid"] == 1


# -- validation ---------------------------------------------------------------


def test_mixed_manifest_classifies_each_row_with_reasons(client, project):
    before = _store_hash()
    rv = _post(client, project["id"], "mixed.csv")
    assert rv.status_code == 200, rv.get_data(as_text=True)
    body = rv.get_json()
    rows = _by_line(body)
    expected = {
        2: "valid",
        3: "malformed",   # non-numeric start
        4: "malformed",   # end == start
        5: "malformed",   # end < start
        6: "malformed",   # negative start
        7: "duplicate",   # repeats line 2
        8: "malformed",   # unknown tag type
        9: "malformed",   # unknown player
        10: "valid",      # hostile label is data, not an error
        11: "malformed",  # wrong column count
    }
    assert {line: row["status"] for line, row in rows.items()} == expected
    for line, row in rows.items():
        if row["status"] != "valid":
            assert row["reasons"], f"line {line} needs at least one reason"
            assert row["clip"] is None
    assert body["summary"] == {"total": 10, "valid": 2, "malformed": 7, "duplicate": 1}
    assert "2" in " ".join(rows[7]["reasons"]), "a duplicate names the line it repeats"
    assert rows[10]["clip"]["label"] == '<script>alert(1)</script> & "quotes" — ‏RTL‏ 😀'
    assert _store_hash() == before


def test_clip_id_collisions_with_store_and_within_file(client, project):
    body = _post(client, project["id"], "clip_id_collision.csv").get_json()
    rows = _by_line(body)
    assert rows[2]["status"] == "duplicate"
    assert rows[3]["status"] == "valid"
    assert rows[4]["status"] == "duplicate"


def test_header_only_manifest_is_an_empty_preview_not_an_error(client, project):
    rv = _post(client, project["id"], "header_only.csv")
    assert rv.status_code == 200
    assert rv.get_json()["summary"] == {"total": 0, "valid": 0, "malformed": 0, "duplicate": 0}
    assert rv.get_json()["rows"] == []


# -- request errors -----------------------------------------------------------


def test_missing_required_header_is_bad_request(client, project):
    rv = _post(client, project["id"], "no_required_header.csv")
    assert rv.status_code == 400
    assert rv.get_json()["code"] == "bad_request"


def test_missing_file_is_bad_request(client, project):
    rv = client.post(PREVIEW.format(pid=project["id"]), data={}, content_type="multipart/form-data")
    assert rv.status_code == 400
    assert rv.get_json()["code"] == "bad_request"


def test_undecodable_file_is_bad_request(client, project):
    data = {"file": (io.BytesIO(b"\xff\xfe\x00\x00" + os.urandom(64)), "junk.csv")}
    rv = client.post(PREVIEW.format(pid=project["id"]), data=data, content_type="multipart/form-data")
    assert rv.status_code == 400
    assert rv.get_json()["code"] == "bad_request"


def test_unknown_project_is_404(client):
    rv = _post(client, "nope", "valid.csv")
    assert rv.status_code == 404
    assert rv.get_json()["error"] == "Project not found"


def test_row_cap_is_enforced(client, project):
    rv = _post(client, project["id"], "too_many_rows.csv")
    assert rv.status_code == 400
    assert rv.get_json()["code"] == "too_many_rows"


# -- preservation -------------------------------------------------------------


def test_other_routes_and_data_are_untouched(client, project):
    pid = project["id"]
    other = client.post("/api/projects", json={"name": "Other"}).get_json()["id"]
    before = json.dumps(application._load_projects(), sort_keys=True)
    for name in ("valid.csv", "mixed.csv", "clip_id_collision.csv"):
        assert _post(client, pid, name).status_code == 200
    assert json.dumps(application._load_projects(), sort_keys=True) == before
    assert client.get(f"/api/projects/{other}").status_code == 200
    csv_export = client.get(f"/api/projects/{pid}/export/csv").data.decode()
    assert "stored" in csv_export, "existing export still works"


# -- documentation ------------------------------------------------------------


def test_format_note_exists_and_cites_the_standard():
    note = ROOT / "docs" / "CSV_IMPORT.md"
    assert note.exists(), "docs/CSV_IMPORT.md is part of the brief"
    text = note.read_text(encoding="utf-8")
    assert "4180" in text
    for column in ("Tag Type", "Start (s)", "End (s)"):
        assert column in text
    assert "BOM" in text or "byte order mark" in text.lower()

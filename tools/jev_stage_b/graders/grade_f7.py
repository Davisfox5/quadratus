"""f7-projects-migration: versioned projects.json with a one-time migration."""
import glob
import json
import os
import re

import pytest

from conftest import seed_project
from support import run_baseline_regressions

LEGACY = {
    "p1": {
        "id": "p1", "name": "Legacy one", "video_filename": "p1.mp4",
        "tag_types": [{"name": "Goal", "color": "#e74c3c"}, {"name": "Pass", "color": "#2ecc71"}],
        "players": [{"id": "pl1", "name": "Alex", "number": "7"}],
        "clips": [{"id": "c1", "tag_type": "Pass", "start": 1.0, "end": 2.5, "label": "x", "notes": "",
                   "players": ["pl1"], "recordings": [{"id": "r1", "filename": "c1_r1.webm", "duration": "3"}]}],
        "filter_presets": [{"id": "fp1", "name": "Passes", "tag_type": "Pass", "player": "", "search": ""}],
    },
    "p2": {"id": "p2", "name": "Legacy two", "video_filename": None, "tag_types": [], "players": [], "clips": []},
}


def _projects_file(application):
    return application.PROJECTS_FILE


def _write_legacy(application):
    raw = json.dumps(LEGACY, indent=2).encode("utf-8")
    with open(_projects_file(application), "wb") as handle:
        handle.write(raw)
    return raw


def _read_file(application):
    with open(_projects_file(application), "rb") as handle:
        return handle.read()


def test_R1_saved_file_is_versioned_and_load_returns_the_mapping(application, client):
    project = seed_project(client, "Versioned")
    on_disk = json.loads(_read_file(application).decode("utf-8"))
    assert isinstance(on_disk, dict), type(on_disk)
    assert on_disk.get("schema_version") == 1, on_disk.keys()
    assert isinstance(on_disk.get("projects"), dict) and project["id"] in on_disk["projects"], on_disk.keys()
    assert on_disk["projects"][project["id"]]["name"] == "Versioned"
    loaded = application._load_projects()
    assert isinstance(loaded, dict) and loaded[project["id"]]["name"] == "Versioned"
    assert "schema_version" not in loaded, "the mapping returned by _load_projects must hold projects only"


def test_R2_legacy_file_is_migrated_exactly_once_with_everything_preserved(application, client):
    _write_legacy(application)
    rv = client.get("/api/projects")
    assert rv.status_code == 200, rv.data
    names = sorted(p["name"] for p in rv.get_json())
    assert names == ["Legacy one", "Legacy two"], names
    after = json.loads(_read_file(application).decode("utf-8"))
    assert after.get("schema_version") == 1, after.keys()
    assert after.get("projects") == LEGACY, "every project, tag type, player, clip and recording must be preserved exactly"
    stat_before = os.stat(_projects_file(application))
    bytes_before = _read_file(application)
    assert client.get("/api/projects").status_code == 200
    assert application._load_projects() == LEGACY
    assert _read_file(application) == bytes_before and os.stat(_projects_file(application)).st_mtime_ns == stat_before.st_mtime_ns, \
        "a second load must not rewrite the file"


def test_R3_the_legacy_bytes_are_backed_up_with_a_utc_timestamp_before_the_rewrite(application, client):
    raw = _write_legacy(application)
    assert client.get("/api/projects").status_code == 200
    backups = glob.glob(_projects_file(application) + ".bak-*")
    assert len(backups) == 1, backups
    name = os.path.basename(backups[0])
    assert re.fullmatch(r"projects\.json\.bak-\d{8}T\d{6}Z", name), name
    with open(backups[0], "rb") as handle:
        assert handle.read() == raw, "the backup must hold the legacy file's exact bytes"


def test_R4_a_failed_replace_leaves_the_legacy_file_intact_and_no_partial_file(application, client, monkeypatch):
    raw = _write_legacy(application)
    original_replace = os.replace

    def failing_replace(src, dst, *args, **kwargs):
        if os.path.abspath(str(dst)) == os.path.abspath(_projects_file(application)):
            raise OSError("simulated crash mid-write")
        return original_replace(src, dst, *args, **kwargs)

    monkeypatch.setattr(os, "replace", failing_replace)
    monkeypatch.setattr(application.os, "replace", failing_replace, raising=False)
    try:
        application._load_projects()
    except Exception:  # noqa: BLE001 -- surfacing the failure is acceptable; the file state is what is judged
        pass
    assert _read_file(application) == raw, "projects.json must still hold the legacy bytes"
    leftovers = [n for n in os.listdir(os.path.dirname(_projects_file(application)))
                 if n.startswith(".projects-") or n.endswith(".tmp")]
    assert leftovers == [], leftovers


def test_R5_a_newer_schema_is_refused_naming_both_versions(application, client):
    with open(_projects_file(application), "w", encoding="utf-8") as handle:
        json.dump({"schema_version": 2, "projects": {}}, handle)
    with pytest.raises(Exception) as caught:
        application._load_projects()
    text = str(caught.value)
    assert "2" in text and "1" in text, text
    rv = client.get("/api/projects")
    assert rv.status_code == 500, rv.status_code
    body = rv.get_json(silent=True)
    assert isinstance(body, dict) and "error" in body, rv.data[:200]
    assert "2" in str(body["error"]) and "1" in str(body["error"]), body


def test_R6_baseline_tests_still_pass(project_root, tmp_path):
    failures = run_baseline_regressions(project_root, tmp_path)
    assert failures == [], "\n\n".join(failures)

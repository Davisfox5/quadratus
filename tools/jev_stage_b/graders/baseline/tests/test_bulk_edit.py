import json
import os
import sys
import tempfile
import threading

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


def test_concurrent_clip_updates_are_not_lost(client, monkeypatch):
    rv = client.post("/api/projects", json={"name": "Concurrent"})
    pid = rv.get_json()["id"]

    rv = client.post(f"/api/projects/{pid}/clips", json={
        "tag_type": "Pass", "start": 1, "end": 3, "label": "orig-a",
    })
    clip_a = rv.get_json()["id"]

    rv = client.post(f"/api/projects/{pid}/clips", json={
        "tag_type": "Shot", "start": 5, "end": 8, "label": "orig-b",
    })
    clip_b = rv.get_json()["id"]

    armed = False
    barrier = threading.Barrier(2, timeout=0.5)
    original_load = application._load_projects

    def wrapped_load():
        projects = original_load()
        if armed:
            # With @_locked the second thread cannot enter _load_projects until
            # the first handler finishes, so the barrier times out. That timeout
            # path (BrokenBarrierError) is the expected behaviour when the lock
            # is present.
            try:
                barrier.wait()
            except threading.BrokenBarrierError:
                pass
        return projects

    monkeypatch.setattr(application, "_load_projects", wrapped_load)
    armed = True

    errors = []

    def put_label(clip_id, label):
        try:
            c = application.app.test_client()
            resp = c.put(
                f"/api/projects/{pid}/clips/{clip_id}",
                json={"label": label},
            )
            if resp.status_code != 200:
                errors.append((clip_id, resp.status_code, resp.get_data(as_text=True)))
        except Exception as exc:
            errors.append((clip_id, repr(exc)))

    t1 = threading.Thread(target=put_label, args=(clip_a, "label-a"))
    t2 = threading.Thread(target=put_label, args=(clip_b, "label-b"))
    t1.start()
    t2.start()
    t1.join()
    t2.join()

    assert not errors, errors

    rv = client.get(f"/api/projects/{pid}")
    clips = {c["id"]: c["label"] for c in rv.get_json()["clips"]}
    assert clips[clip_a] == "label-a"
    assert clips[clip_b] == "label-b"


def _projects_tmp_leftovers(directory):
    return [
        name for name in os.listdir(directory)
        if name.startswith(".projects-") and name.endswith(".tmp")
    ]


def test_failed_save_leaves_prior_state_intact(client, tmp_path, monkeypatch):
    rv = client.post("/api/projects", json={"name": "Atomic Fail"})
    assert rv.status_code == 201
    pid = rv.get_json()["id"]

    prior = application._load_projects()
    assert pid in prior

    def exploding_dump(obj, fp, **kwargs):
        fp.write("{")
        raise RuntimeError("simulated dump failure")

    monkeypatch.setattr(application.json, "dump", exploding_dump)
    monkeypatch.setitem(application.app.config, "PROPAGATE_EXCEPTIONS", False)

    rv = client.post(f"/api/projects/{pid}/clips", json={
        "tag_type": "Pass", "start": 1, "end": 3, "label": "should-not-persist",
    })
    assert rv.status_code == 500

    assert application._load_projects() == prior
    assert _projects_tmp_leftovers(tmp_path) == []


def test_reader_during_save_sees_complete_old_state(client, tmp_path, monkeypatch):
    rv = client.post("/api/projects", json={"name": "Atomic Read"})
    assert rv.status_code == 201
    pid = rv.get_json()["id"]

    prior = application._load_projects()
    assert pid in prior
    assert prior[pid]["clips"] == []

    original_dump = application.json.dump
    mid_save = threading.Event()
    release = threading.Event()

    def stalled_dump(obj, fp, *args, **kwargs):
        original_dump(obj, fp, *args, **kwargs)
        mid_save.set()
        assert release.wait(timeout=5), "reader did not release in-progress save"

    monkeypatch.setattr(application.json, "dump", stalled_dump)

    result = {}

    def post_clip():
        try:
            c = application.app.test_client()
            resp = c.post(
                f"/api/projects/{pid}/clips",
                json={"tag_type": "Pass", "start": 1, "end": 3, "label": "new-clip"},
            )
            result["status"] = resp.status_code
            result["body"] = resp.get_json()
        except Exception as exc:
            result["error"] = repr(exc)

    t = threading.Thread(target=post_clip)
    t.start()
    try:
        assert mid_save.wait(timeout=5), "save did not reach json.dump"
        loaded = application._load_projects()
        assert loaded == prior
    finally:
        release.set()
        t.join(timeout=5)

    assert not t.is_alive()
    assert "error" not in result, result.get("error")
    assert result["status"] == 201
    new_id = result["body"]["id"]

    after = application._load_projects()
    clip_ids = [c["id"] for c in after[pid]["clips"]]
    assert new_id in clip_ids
    assert _projects_tmp_leftovers(tmp_path) == []


def _seed(client):
    project_a = client.post("/api/projects", json={"name": "Alpha"}).get_json()["id"]
    project_b = client.post("/api/projects", json={"name": "Beta"}).get_json()["id"]
    player_1 = client.post(f"/api/projects/{project_a}/players",
                           json={"name": "Alex", "number": "7"}).get_json()["id"]
    player_2 = client.post(f"/api/projects/{project_a}/players",
                           json={"name": "Blair", "number": "11"}).get_json()["id"]

    def clip(pid, tag, start, label, players=None, notes=""):
        return client.post(f"/api/projects/{pid}/clips", json={
            "tag_type": tag, "start": start, "end": start + 2,
            "label": label, "notes": notes, "players": players or [],
        }).get_json()["id"]

    clip_1 = clip(project_a, "Pass", 10, "build-up", [player_1], "keep this")
    clip_2 = clip(project_a, "Shot", 20, "chance", [player_2])
    clip_3 = clip(project_a, "Goal", 30, "winner")
    foreign = clip(project_b, "Pass", 40, "other project", notes="foreign note")
    client.post(f"/api/projects/{project_a}/clips/{clip_1}/annotations",
                json={"type": "text", "data": {"x": .5, "y": .5, "text": "press"}})
    return {
        "a": project_a, "b": project_b, "p1": player_1, "p2": player_2,
        "c1": clip_1, "c2": clip_2, "c3": clip_3, "foreign": foreign,
    }


def test_bulk_preview_apply_persists_and_isolates(client):
    ids = _seed(client)
    before = application._load_projects()
    selected = [ids["c2"], ids["c1"]]
    rv = client.post(f"/api/projects/{ids['a']}/clips/bulk_preview",
                     json={"clip_ids": selected, "changes": {
                         "tag_type": "Goal", "players": [ids["p2"]]}})
    assert rv.status_code == 200
    preview = rv.get_json()
    assert (preview["project_id"], preview["count"]) == (ids["a"], 2)
    assert [clip["id"] for clip in preview["clips"]] == [ids["c1"], ids["c2"]]
    assert preview["clips"][0]["before"] == {"tag_type": "Pass", "players": [ids["p1"]]}
    assert all(clip["after"] == {"tag_type": "Goal", "players": [ids["p2"]]}
               for clip in preview["clips"])

    rv = client.post(f"/api/projects/{ids['a']}/clips/bulk_apply",
                     json={"preview_id": preview["preview_id"]})
    assert rv.status_code == 200
    assert rv.get_json() == {"updated": 2, "clip_ids": [ids["c1"], ids["c2"]]}

    for persisted in (application._load_projects(),
                      {ids["a"]: client.get(f"/api/projects/{ids['a']}").get_json(),
                       ids["b"]: client.get(f"/api/projects/{ids['b']}").get_json()}):
        old_a = {clip["id"]: clip for clip in before[ids["a"]]["clips"]}
        new_a = {clip["id"]: clip for clip in persisted[ids["a"]]["clips"]}
        for clip_id in selected:
            assert (new_a[clip_id]["tag_type"], new_a[clip_id]["players"]) == (
                "Goal", [ids["p2"]])
            for field in ("start", "end", "label", "notes", "annotations", "recordings"):
                assert new_a[clip_id].get(field, []) == old_a[clip_id].get(field, [])
        assert new_a[ids["c3"]] == old_a[ids["c3"]]
        assert persisted[ids["b"]]["clips"] == before[ids["b"]]["clips"]


def test_bulk_apply_can_explicitly_clear_players(client):
    ids = _seed(client)
    preview = client.post(f"/api/projects/{ids['a']}/clips/bulk_preview",
                          json={"clip_ids": [ids["c1"]],
                                "changes": {"players": []}}).get_json()
    rv = client.post(f"/api/projects/{ids['a']}/clips/bulk_apply",
                     json={"preview_id": preview["preview_id"]})
    assert rv.status_code == 200
    clip = next(c for c in client.get(f"/api/projects/{ids['a']}").get_json()["clips"]
                if c["id"] == ids["c1"])
    assert clip["players"] == []


@pytest.mark.parametrize("case,code", [
    ("duplicate_clip_ids", "duplicate_clip_ids"),
    ("unknown_clip_ids", "unknown_clip_ids"),
    ("invalid_tag_type", "invalid_tag_type"),
    ("unknown_players", "unknown_players"),
    ("duplicate_players", "duplicate_players"),
    ("no_changes", "no_changes"),
    ("empty_clip_ids", "empty_clip_ids"),
    ("bad_request", "bad_request"),
])
def test_bulk_preview_validation(client, case, code):
    ids = _seed(client)
    bodies = {
        "duplicate_clip_ids": {"clip_ids": [ids["c1"], ids["c1"]], "changes": {"tag_type": "Goal"}},
        "unknown_clip_ids": {"clip_ids": [ids["foreign"], "missing"], "changes": {"tag_type": "Goal"}},
        "invalid_tag_type": {"clip_ids": [ids["c1"]], "changes": {"tag_type": "Assist"}},
        "unknown_players": {"clip_ids": [ids["c1"]], "changes": {"players": ["missing"]}},
        "duplicate_players": {"clip_ids": [ids["c1"]], "changes": {"players": [ids["p1"], ids["p1"]]}},
        "no_changes": {"clip_ids": [ids["c1"]], "changes": {}},
        "empty_clip_ids": {"clip_ids": [], "changes": {"tag_type": "Goal"}},
        "bad_request": {"clip_ids": [ids["c1"]], "changes": {"tag_type": "Goal"}, "extra": True},
    }
    rv = client.post(f"/api/projects/{ids['a']}/clips/bulk_preview", json=bodies[case])
    assert rv.status_code == 400
    assert rv.get_json()["code"] == code


def test_bulk_noop_and_wrong_project_preview_is_not_consumed(client):
    ids = _seed(client)
    noop = client.post(f"/api/projects/{ids['a']}/clips/bulk_preview", json={
        "clip_ids": [ids["c1"]], "changes": {
            "tag_type": "Pass", "players": [ids["p1"]]}}).get_json()
    assert (noop["count"], noop["preview_id"], noop["clips"]) == (0, None, [])

    preview_id = client.post(f"/api/projects/{ids['a']}/clips/bulk_preview", json={
        "clip_ids": [ids["c1"]], "changes": {"tag_type": "Shot"},
    }).get_json()["preview_id"]
    wrong = client.post(f"/api/projects/{ids['b']}/clips/bulk_apply",
                        json={"preview_id": preview_id})
    assert wrong.status_code == 409
    assert wrong.get_json()["code"] == "preview_project_mismatch"
    right = client.post(f"/api/projects/{ids['a']}/clips/bulk_apply",
                        json={"preview_id": preview_id})
    assert right.status_code == 200
    assert right.get_json() == {"updated": 1, "clip_ids": [ids["c1"]]}


def _goal_preview(client, ids):
    return client.post(f"/api/projects/{ids['a']}/clips/bulk_preview", json={
        "clip_ids": [ids["c1"], ids["c2"]], "changes": {"tag_type": "Goal"},
    }).get_json()["preview_id"]


def _probe_concurrent_loads(monkeypatch):
    barrier = threading.Barrier(2, timeout=0.5)
    original_load = application._load_projects

    def wrapped_load():
        projects = original_load()
        try:
            barrier.wait()
        except threading.BrokenBarrierError:
            pass
        return projects

    monkeypatch.setattr(application, "_load_projects", wrapped_load)
    return original_load


def _concurrent_requests(requests, start_together=False):
    results = []
    start = threading.Barrier(len(requests), timeout=5) if start_together else None

    def send(method, path, body):
        if start:
            start.wait()
        with application.app.test_client() as thread_client:
            rv = getattr(thread_client, method)(path, json=body)
            results.append((rv.status_code, rv.get_json()))

    threads = [threading.Thread(target=send, args=request) for request in requests]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=10)
    assert not any(thread.is_alive() for thread in threads)
    return results


def test_bulk_apply_rejects_clip_modified_after_preview(client):
    ids = _seed(client)
    preview_id = _goal_preview(client, ids)

    edited = client.put(f"/api/projects/{ids['a']}/clips/{ids['c1']}",
                        json={"notes": "edited elsewhere"})
    assert edited.status_code == 200
    rv = client.post(f"/api/projects/{ids['a']}/clips/bulk_apply",
                     json={"preview_id": preview_id})
    assert rv.status_code == 409
    body = rv.get_json()
    assert body["code"] == "stale"
    assert len(body["conflicts"]) == 1
    conflict = body["conflicts"][0]
    assert (conflict["id"], conflict["reason"]) == (ids["c1"], "modified")
    assert conflict["current"]["notes"] == "edited elsewhere"
    assert conflict["current"]["tag_type"] == "Pass"

    clips = {clip["id"]: clip for clip in application._load_projects()[ids["a"]]["clips"]}
    assert clips[ids["c1"]]["tag_type"] == "Pass"
    assert clips[ids["c2"]]["tag_type"] == "Shot"
    retry = client.post(f"/api/projects/{ids['a']}/clips/bulk_apply",
                        json={"preview_id": preview_id})
    assert retry.status_code == 409
    assert retry.get_json()["code"] == "preview_missing"


def test_bulk_apply_consumes_invalid_changes_without_saving(client, monkeypatch):
    ids = _seed(client)
    preview_id = _goal_preview(client, ids)
    assert client.put(f"/api/projects/{ids['a']}/tag_types", json={
        "tag_types": [{"name": "Pass", "color": "#2ecc71"}],
    }).status_code == 200

    def unexpected_save(_projects):
        pytest.fail("changes_invalid must not save projects")

    monkeypatch.setattr(application, "_save_projects", unexpected_save)
    invalid = client.post(f"/api/projects/{ids['a']}/clips/bulk_apply",
                          json={"preview_id": preview_id})
    assert invalid.status_code == 409
    assert invalid.get_json()["code"] == "changes_invalid"

    consumed = client.post(f"/api/projects/{ids['a']}/clips/bulk_apply",
                           json={"preview_id": preview_id})
    assert consumed.status_code == 409
    assert consumed.get_json()["code"] == "preview_missing"


def test_bulk_apply_rejects_clip_deleted_after_preview(client):
    ids = _seed(client)
    preview_id = _goal_preview(client, ids)
    assert client.delete(
        f"/api/projects/{ids['a']}/clips/{ids['c2']}").status_code == 200

    rv = client.post(f"/api/projects/{ids['a']}/clips/bulk_apply",
                     json={"preview_id": preview_id})
    assert rv.status_code == 409
    assert rv.get_json()["code"] == "stale"
    assert rv.get_json()["conflicts"] == [
        {"id": ids["c2"], "reason": "deleted", "current": None}]
    clips = {clip["id"]: clip for clip in application._load_projects()[ids["a"]]["clips"]}
    assert clips[ids["c1"]]["tag_type"] == "Pass"


def test_simultaneous_bulk_confirmations_apply_once(client, monkeypatch):
    ids = _seed(client)
    before = application._load_projects()
    preview_id = _goal_preview(client, ids)
    original_load = _probe_concurrent_loads(monkeypatch)
    request = ("post", f"/api/projects/{ids['a']}/clips/bulk_apply",
               {"preview_id": preview_id})
    results = _concurrent_requests([request, request], start_together=True)

    assert sorted(status for status, _ in results) == [200, 409]
    success = next(body for status, body in results if status == 200)
    conflict = next(body for status, body in results if status == 409)
    assert success["updated"] == 2
    assert conflict["code"] == "preview_missing"
    persisted = original_load()
    clips = {clip["id"]: clip for clip in persisted[ids["a"]]["clips"]}
    assert [clips[ids[key]]["tag_type"] for key in ("c1", "c2")] == ["Goal", "Goal"]
    assert clips[ids["c3"]] == next(
        clip for clip in before[ids["a"]]["clips"] if clip["id"] == ids["c3"])
    assert persisted[ids["b"]] == before[ids["b"]]


def test_bulk_apply_racing_unrelated_edit_loses_neither_update(client, monkeypatch):
    ids = _seed(client)
    preview_id = _goal_preview(client, ids)
    original_load = _probe_concurrent_loads(monkeypatch)
    results = _concurrent_requests([
        ("post", f"/api/projects/{ids['a']}/clips/bulk_apply", {"preview_id": preview_id}),
        ("put", f"/api/projects/{ids['a']}/clips/{ids['c3']}", {"label": "renamed"}),
    ])
    assert sorted(status for status, _ in results) == [200, 200]

    clips = {clip["id"]: clip for clip in original_load()[ids["a"]]["clips"]}
    assert [clips[ids[key]]["tag_type"] for key in ("c1", "c2")] == ["Goal", "Goal"]
    assert clips[ids["c3"]]["label"] == "renamed"


def test_bulk_apply_updates_exports_and_preserves_filter_presets(client):
    ids = _seed(client)
    created = client.post(f"/api/projects/{ids['a']}/filter_presets", json={
        "name": "Blair winners", "tag_type": "Goal",
        "player": ids["p2"], "search": "winner",
    })
    assert created.status_code == 201
    preset = created.get_json()
    presets_before = application._load_projects()[ids["a"]]["filter_presets"]

    preview = client.post(f"/api/projects/{ids['a']}/clips/bulk_preview", json={
        "clip_ids": [ids["c1"], ids["c2"]],
        "changes": {"tag_type": "Goal", "players": [ids["p2"]]},
    }).get_json()
    applied = client.post(f"/api/projects/{ids['a']}/clips/bulk_apply",
                          json={"preview_id": preview["preview_id"]})
    assert applied.status_code == 200

    goals = client.get(
        f"/api/projects/{ids['a']}/export/json?tag_type=Goal").get_json()
    assert goals["clip_count"] == 3
    clips = {clip["id"]: clip for clip in goals["clips"]}
    assert set(clips) == {ids["c1"], ids["c2"], ids["c3"]}
    assert all(clip["tag_type"] == "Goal" for clip in clips.values())
    blair = [{"id": ids["p2"], "name": "Blair", "number": "11"}]
    assert clips[ids["c1"]]["players"] == blair
    assert clips[ids["c2"]]["players"] == blair

    passes = client.get(
        f"/api/projects/{ids['a']}/export/json?tag_type=Pass").get_json()
    assert passes["clip_count"] == 0

    player_csv = client.get(
        f"/api/projects/{ids['a']}/export/csv?player={ids['p2']}")
    player_rows = player_csv.data.decode("utf-8").splitlines()
    assert len(player_rows) == 3
    assert all("Goal" in row and "#11 Blair" in row for row in player_rows[1:])
    assert "Pass" not in player_csv.data.decode("utf-8")

    search_csv = client.get(
        f"/api/projects/{ids['a']}/export/csv?tag_type=Goal&search=winner")
    search_rows = search_csv.data.decode("utf-8").splitlines()
    assert len(search_rows) == 2
    assert "winner" in search_rows[1]

    assert client.get(
        f"/api/projects/{ids['a']}/filter_presets").get_json() == [preset]
    assert application._load_projects()[ids["a"]]["filter_presets"] == presets_before


def test_bulk_player_clear_is_reflected_in_exports(client):
    ids = _seed(client)
    preview = client.post(f"/api/projects/{ids['a']}/clips/bulk_preview", json={
        "clip_ids": [ids["c1"]], "changes": {"players": []},
    }).get_json()
    applied = client.post(f"/api/projects/{ids['a']}/clips/bulk_apply",
                          json={"preview_id": preview["preview_id"]})
    assert applied.status_code == 200

    player_csv = client.get(
        f"/api/projects/{ids['a']}/export/csv?player={ids['p1']}")
    assert len(player_csv.data.decode("utf-8").splitlines()) == 1

    exported = client.get(f"/api/projects/{ids['a']}/export/json").get_json()
    clip = next(clip for clip in exported["clips"] if clip["id"] == ids["c1"])
    assert clip["players"] == []

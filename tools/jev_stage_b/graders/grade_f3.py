"""f3-project-rename: PATCH endpoint, persistence, inline card control."""
import copy
import glob
import os

from conftest import no_console_errors, seed_project
from support import run_baseline_regressions


def _patch(client, pid, name):
    return client.patch(f"/api/projects/{pid}", json={"name": name})


def test_R1_patch_trims_validates_and_answers_with_the_project(application, client):
    first = seed_project(client, "First")
    second = seed_project(client, "Second")
    rv = _patch(client, first["id"], "  Renamed first  ")
    assert rv.status_code == 200, rv.data
    body = rv.get_json()
    assert body["name"] == "Renamed first" and body["id"] == first["id"] and "tag_types" in body and "clips" in body
    for bad, needle in (("   ", "empty"), ("x" * 81, "long"), ("second", "")):
        rv = _patch(client, first["id"], bad)
        assert rv.status_code == 400, (bad[:10], rv.status_code, rv.data[:200])
        err = rv.get_json(silent=True)
        assert isinstance(err, dict) and isinstance(err.get("error"), str) and err["error"], rv.data[:200]
        if needle:
            assert needle in err["error"].lower() or ("required" in err["error"].lower() and needle == "empty") \
                or ("80" in err["error"] and needle == "long"), err
    rv = _patch(client, first["id"], "SECOND")
    assert rv.status_code == 400, "a duplicate is matched case-insensitively"
    assert "second" in rv.get_json()["error"].lower() or "duplicate" in rv.get_json()["error"].lower() or "exists" in rv.get_json()["error"].lower()
    rv = _patch(client, first["id"], "Renamed first")
    assert rv.status_code == 200, "renaming to the project's own current name is allowed"
    assert _patch(client, "nope", "x").status_code == 404
    assert client.get(f"/api/projects/{second['id']}").get_json()["name"] == "Second"


def test_R2_the_new_name_is_persisted_atomically(application, client):
    project = seed_project(client, "Before")
    assert _patch(client, project["id"], "After").status_code == 200
    assert application._load_projects()[project["id"]]["name"] == "After"
    assert [p["name"] for p in client.get("/api/projects").get_json()] == ["After"]
    leftovers = glob.glob(os.path.join(os.path.dirname(application.PROJECTS_FILE), ".projects-*"))
    assert leftovers == [], leftovers


def _open(live, page):
    page.goto(live.url + "/")
    page.wait_for_selector(".project-card", timeout=5000)
    page.wait_for_function("() => document.querySelectorAll('.project-card').length === 2", timeout=5000)


def test_R3_inline_rename_control_enter_saves_escape_cancels_and_errors_show(live_factory, page_factory):
    live = live_factory([{"name": "One"}, {"name": "Two"}])
    page = page_factory()
    _open(live, page)
    patches = []
    page.on("request", lambda req: patches.append(req.url) if req.method == "PATCH" else None)
    page.evaluate("() => { window.__graderMarker = 'kept'; }")
    first = page.locator(".project-card").first
    first.locator(".rename-btn").click()
    box = first.locator(".rename-input")
    box.wait_for(state="visible", timeout=5000)
    assert box.input_value() == "One"
    box.fill("One renamed")
    box.press("Enter")
    page.wait_for_function("() => document.querySelector('.project-card .name').textContent.trim() === 'One renamed'", timeout=5000)
    assert len(patches) == 1, patches
    assert page.evaluate("() => window.__graderMarker") == "kept", "the page reloaded"
    assert [p["name"] for p in live.api("/api/projects")][0] == "One renamed"
    first = page.locator(".project-card").first
    first.locator(".rename-btn").click()
    box = first.locator(".rename-input")
    box.wait_for(state="visible", timeout=5000)
    box.fill("Should not stick")
    box.press("Escape")
    page.wait_for_timeout(300)
    assert page.evaluate("() => document.querySelector('.project-card .name').textContent.trim()") == "One renamed"
    assert len(patches) == 1, "Escape must not send a request"
    first.locator(".rename-btn").click()
    box = first.locator(".rename-input")
    box.wait_for(state="visible", timeout=5000)
    box.fill("two")
    box.press("Enter")
    err = first.locator(".rename-error")
    err.wait_for(state="visible", timeout=5000)
    shown = err.inner_text().strip()
    assert shown, "the server's error text must be shown"
    assert len(patches) == 2
    assert page.evaluate("() => document.querySelector('.project-card .name').textContent.trim()") == "One renamed"
    # The duplicate name must come back as a 400 from PATCH /api/projects/<id>;
    # Chromium logs that response as a console error, and only that one is
    # excused, with the response itself required as evidence.
    no_console_errors(page, expected_http=(("PATCH", "/api/projects/", 400),))


def test_R4_rename_changes_nothing_but_the_name(application, client):
    project = seed_project(client, "Keep", players=[{"name": "Alex", "number": "7"}],
                           clips=[{"tag_type": "Pass", "start": 1, "end": 2, "label": "l", "notes": "n", "players": []}])
    pid = project["id"]
    clip = project["clips"][0]["id"]
    import io
    rv = client.post(f"/api/projects/{pid}/clips/{clip}/recordings",
                     data={"recording": (io.BytesIO(b"webm"), "r.webm"), "duration": "2"}, content_type="multipart/form-data")
    assert rv.status_code == 201, rv.data
    rv = client.post(f"/api/projects/{pid}/filter_presets", json={"name": "Passes", "tag_type": "Pass", "player": "", "search": ""})
    assert rv.status_code in (200, 201), rv.data
    before = copy.deepcopy(application._load_projects()[pid])
    assert _patch(client, pid, "Kept renamed").status_code == 200
    after = copy.deepcopy(application._load_projects()[pid])
    assert after["name"] == "Kept renamed"
    before.pop("name"), after.pop("name")
    assert before == after, "every field but name must be unchanged"


def test_R5_baseline_tests_still_pass_and_the_rename_flow_logs_no_console_errors(project_root, tmp_path, live_factory, page_factory):
    live = live_factory([{"name": "One"}, {"name": "Two"}])
    page = page_factory()
    _open(live, page)
    page.locator(".project-card").first.locator(".rename-btn").click()
    page.wait_for_timeout(300)
    no_console_errors(page)
    failures = run_baseline_regressions(project_root, tmp_path)
    assert failures == [], "\n\n".join(failures)

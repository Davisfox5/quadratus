"""f4-tags-export: clip manifest export in the import preview's format."""
import io

from conftest import no_console_errors, seed_project
from support import parse_csv, run_baseline_regressions

COLUMNS = ["Clip ID", "Tag Type", "Start (s)", "End (s)", "Label", "Notes", "Players"]
PLAYERS = [{"name": "Alex", "number": "7"}, {"name": "Bo", "number": ""}]


def _seed(client, name="Game"):
    project = seed_project(client, name, players=PLAYERS)
    alex = next(p["id"] for p in project["players"] if p["name"] == "Alex")
    bo = next(p["id"] for p in project["players"] if p["name"] == "Bo")
    clips = [
        {"tag_type": "Pass", "start": 1.5, "end": 4, "label": "plain", "notes": "", "players": [alex]},
        {"tag_type": "Goal", "start": 10, "end": 12.25, "label": 'say "hi", twice', "notes": "line one\nline two", "players": [alex, bo]},
        {"tag_type": "Shot", "start": 20, "end": 21, "label": "", "notes": "", "players": []},
    ]
    for clip in clips:
        assert client.post(f"/api/projects/{project['id']}/clips", json=clip).status_code == 201
    return client.get(f"/api/projects/{project['id']}").get_json()


def test_R1_per_project_export_has_the_exact_header_and_one_row_per_clip(client):
    project = _seed(client)
    rv = client.get(f"/api/projects/{project['id']}/export/manifest.csv")
    assert rv.status_code == 200, rv.data[:200]
    assert rv.mimetype == "text/csv", rv.mimetype
    rows = parse_csv(rv.data.decode("utf-8-sig"))
    assert rows[0] == COLUMNS, rows[0]
    assert [r[0] for r in rows[1:]] == [c["id"] for c in project["clips"]]
    assert [r[1] for r in rows[1:]] == ["Pass", "Goal", "Shot"]
    assert float(rows[1][2]) == 1.5 and float(rows[1][3]) == 4
    rv = client.get("/api/projects/nope/export/manifest.csv")
    assert rv.status_code == 404 and rv.get_json(silent=True) and "error" in rv.get_json()


def test_R2_global_export_prefixes_the_project_name(client):
    first = _seed(client, "First game")
    second = seed_project(client, "Second game", players=PLAYERS,
                          clips=[{"tag_type": "Pass", "start": 2, "end": 3, "label": "s", "notes": "", "players": []}])
    rv = client.get("/api/export/manifest.csv")
    assert rv.status_code == 200, rv.data[:200]
    rows = parse_csv(rv.data.decode("utf-8-sig"))
    assert rows[0] == ["Project"] + COLUMNS, rows[0]
    assert [r[0] for r in rows[1:]] == ["First game"] * 3 + ["Second game"]
    assert [r[1] for r in rows[1:]] == [c["id"] for c in first["clips"]] + [c["id"] for c in second["clips"]]


def test_R3_values_are_quoted_and_players_use_the_import_form(client):
    project = _seed(client)
    rv = client.get(f"/api/projects/{project['id']}/export/manifest.csv")
    rows = parse_csv(rv.data.decode("utf-8-sig"))
    goal = rows[2]
    assert goal[4] == 'say "hi", twice', goal
    assert goal[5] == "line one\nline two", goal
    assert goal[6] == "#7 Alex; Bo", goal
    assert rows[1][6] == "#7 Alex" and rows[3][6] == "", (rows[1][6], rows[3][6])


def test_R4_content_disposition_and_card_links(client, live_factory, page_factory):
    project = _seed(client, "My Big Game")
    rv = client.get(f"/api/projects/{project['id']}/export/manifest.csv")
    disposition = rv.headers.get("Content-Disposition", "")
    assert "attachment" in disposition and "My_Big_Game_manifest.csv" in disposition, disposition
    rv = client.get("/api/export/manifest.csv")
    disposition = rv.headers.get("Content-Disposition", "")
    assert "attachment" in disposition and "all_projects_manifest.csv" in disposition, disposition
    live = live_factory([{"name": "Card game"}])
    page = page_factory()
    page.goto(live.url + "/")
    page.wait_for_selector(".project-card .export-manifest", timeout=5000)
    href = page.evaluate("() => document.querySelector('.project-card .export-manifest').getAttribute('href')")
    assert href == f"/api/projects/{live.projects[0]['id']}/export/manifest.csv", href
    no_console_errors(page)


def test_R5_the_export_round_trips_through_the_import_preview(client):
    project = _seed(client)
    export = client.get(f"/api/projects/{project['id']}/export/manifest.csv").data
    target = seed_project(client, "Target", players=PLAYERS)
    rv = client.post(f"/api/projects/{target['id']}/clips/import_preview",
                     data={"file": (io.BytesIO(export), "manifest.csv")}, content_type="multipart/form-data")
    assert rv.status_code == 200, rv.data[:300]
    summary = rv.get_json()["summary"]
    assert summary["malformed"] == 0, rv.get_json()["rows"]
    assert summary["valid"] == len(project["clips"]), summary


def test_R6_baseline_tests_still_pass(project_root, tmp_path):
    failures = run_baseline_regressions(project_root, tmp_path)
    assert failures == [], "\n\n".join(failures)

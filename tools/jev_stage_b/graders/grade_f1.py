"""f1-preview-errors: per-row error list, table and CSV download under the import preview."""
import io

from conftest import no_console_errors, seed_project
from support import parse_csv, run_baseline_regressions

HEADER = "Tag Type,Start (s),End (s),Label,Players"
LINES = [
    HEADER,
    "Pass,1,2,ok one,",                 # line 2 valid
    "Nope,1,2,bad tag,",                # line 3 malformed: unknown tag type
    "Pass,abc,2,bad start,",            # line 4 malformed: start
    "Pass,3,4,ok two,",                 # line 5 valid
    "Pass,3,4,ok two,",                 # line 6 duplicate of line 5
    'Pass,5,"4",end before start,',     # line 7 malformed: end <= start
    "Pass,6,7,unknown,Zed",             # line 8 malformed: unknown player
]
CSV = ("\r\n".join(LINES) + "\r\n").encode("utf-8")
MALFORMED_LINES = [3, 4, 7, 8]


def _post(client, pid, raw=CSV):
    return client.post(f"/api/projects/{pid}/clips/import_preview",
                       data={"file": (io.BytesIO(raw), "m.csv")}, content_type="multipart/form-data")


def test_R1_response_lists_malformed_rows_with_line_reasons_and_raw(application, client):
    project = seed_project(client, "Preview", players=[{"name": "Alex", "number": "7"}])
    rv = _post(client, project["id"])
    assert rv.status_code == 200, rv.data
    body = rv.get_json()
    assert body["preview_only"] is True
    assert body["summary"] == {"total": 7, "valid": 2, "malformed": 4, "duplicate": 1}, body["summary"]
    assert [r["line"] for r in body["rows"]] == [2, 3, 4, 5, 6, 7, 8]
    errors = body.get("errors")
    assert isinstance(errors, list), body.keys()
    assert [e["line"] for e in errors] == MALFORMED_LINES, errors
    by_line = {r["line"]: r for r in body["rows"]}
    for entry in errors:
        assert isinstance(entry["line"], int)
        assert entry["reasons"] == by_line[entry["line"]]["reasons"], entry
        assert entry["raw"].rstrip("\r\n") == LINES[entry["line"] - 1], entry
    assert all(e["line"] not in (2, 5, 6) for e in errors), "valid and duplicate rows never appear in errors"


def _upload(live, page, project, raw=CSV):
    page.goto(live.url + "/")
    page.wait_for_selector(".project-card", timeout=5000)
    page.click(".project-card")
    page.wait_for_selector("#btn-import-preview", timeout=5000)
    page.set_input_files("#import-preview-file", {"name": "m.csv", "mimeType": "text/csv", "buffer": raw})
    page.wait_for_function("() => document.getElementById('import-preview-summary').textContent.startsWith('Total')", timeout=5000)


def test_R2_error_table_under_the_summary_lists_malformed_rows_in_order(live_factory, page_factory):
    live = live_factory([{"name": "Preview", "players": [{"name": "Alex", "number": "7"}]}])
    page = page_factory()
    _upload(live, page, live.projects[0])
    page.wait_for_selector("#import-preview-errors", state="attached", timeout=5000)
    page.wait_for_function("() => document.querySelectorAll('#import-preview-errors tr[data-line]').length === 4", timeout=5000)
    after_summary = page.evaluate("() => { const s = document.getElementById('import-preview-summary'); const t = document.getElementById('import-preview-errors'); "
                                  "return !!(s.compareDocumentPosition(t) & Node.DOCUMENT_POSITION_FOLLOWING); }")
    assert after_summary, "#import-preview-errors must follow #import-preview-summary"
    rows = page.evaluate("() => Array.from(document.querySelectorAll('#import-preview-errors tr[data-line]')).map(tr => [tr.dataset.line, tr.innerText])")
    assert [int(r[0]) for r in rows] == MALFORMED_LINES, rows
    assert "unknown tag type" in rows[0][1] and "3" in rows[0][1], rows[0]
    assert "unknown player" in rows[3][1], rows[3]
    assert not page.evaluate("() => document.getElementById('import-preview-errors').hidden")
    clean = (HEADER + "\r\nPass,1,2,fine,\r\n").encode("utf-8")
    page.set_input_files("#import-preview-file", {"name": "ok.csv", "mimeType": "text/csv", "buffer": clean})
    page.wait_for_function("() => document.getElementById('import-preview-summary').textContent.startsWith('Total 1')", timeout=5000)
    page.wait_for_function("() => document.getElementById('import-preview-errors').hidden === true", timeout=5000)
    no_console_errors(page)


def test_R3_download_link_carries_the_malformed_rows_as_csv(live_factory, page_factory):
    live = live_factory([{"name": "Preview", "players": [{"name": "Alex", "number": "7"}]}])
    page = page_factory()
    _upload(live, page, live.projects[0])
    page.wait_for_selector("#import-preview-errors-download", state="attached", timeout=5000)
    page.wait_for_function("() => { const a = document.getElementById('import-preview-errors-download'); return a && !a.hidden && a.href; }", timeout=5000)
    assert page.evaluate("() => document.getElementById('import-preview-errors-download').getAttribute('download')") == "import-errors.csv"
    text = page.evaluate("async () => { const a = document.getElementById('import-preview-errors-download'); const r = await fetch(a.href); return await r.text(); }")
    rows = parse_csv(text)
    assert rows[0] == ["line", "reason", "raw"], rows[0]
    assert [int(r[0]) for r in rows[1:]] == MALFORMED_LINES, rows
    assert rows[1][1] == "unknown tag type" and rows[1][2].rstrip("\r\n") == LINES[2], rows[1]
    assert rows[3][1] == "end must be greater than start" and rows[3][2].rstrip("\r\n") == LINES[6], rows[3]
    clean = (HEADER + "\r\nPass,1,2,fine,\r\n").encode("utf-8")
    page.set_input_files("#import-preview-file", {"name": "ok.csv", "mimeType": "text/csv", "buffer": clean})
    page.wait_for_function("() => document.getElementById('import-preview-errors-download').hidden === true", timeout=5000)
    no_console_errors(page)


def test_R4_baseline_tests_still_pass_and_the_preview_flow_logs_no_console_errors(project_root, tmp_path, live_factory, page_factory):
    live = live_factory([{"name": "Preview", "players": [{"name": "Alex", "number": "7"}]}])
    page = page_factory()
    _upload(live, page, live.projects[0])
    page.wait_for_timeout(500)
    no_console_errors(page)
    failures = run_baseline_regressions(project_root, tmp_path)
    assert failures == [], "\n\n".join(failures)

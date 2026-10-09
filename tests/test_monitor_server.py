"""The monitor page serves the read-only status on loopback and touches nothing."""

from __future__ import annotations

import json
import os
import threading
import urllib.request

import pytest

from quadratus import monitor_server
from quadratus.monitor import read_status, run_history


def _tree(root):
    out = {}
    for base, dirs, files in os.walk(root):
        for name in dirs + files:
            path = os.path.join(base, name)
            stat = os.lstat(path)
            out[path] = (stat.st_size, stat.st_mtime_ns)
    return out


def _project(tmp_path):
    run_dir = tmp_path / ".quadratus" / "runs" / "20261007T120000Z-0000beef"
    run_dir.mkdir(parents=True)
    (run_dir / "invocations.jsonl").write_text(json.dumps(dict(
        task="T4", role="verifier", canonical_model="claude:opus", invoked=True, outcome="ok",
        seconds=2.0, input_tokens=100, output_tokens=50)) + "\n")
    (run_dir / "budget.json").write_text(json.dumps(dict(
        limits=dict(max_reported_tokens=1000), reserved_attempts=1, reported_tokens=150)))
    return run_dir


@pytest.fixture
def server(tmp_path):
    _project(tmp_path)
    srv = monitor_server.MonitorServer(0, project=str(tmp_path), history=5)
    thread = threading.Thread(target=srv.serve_forever, daemon=True)
    thread.start()
    try:
        yield srv, f"http://127.0.0.1:{srv.server_address[1]}", tmp_path
    finally:
        srv.shutdown()
        srv.server_close()


def _get(url):
    with urllib.request.urlopen(url, timeout=5) as resp:
        return resp.status, resp.headers.get("Content-Type", ""), resp.read().decode("utf-8")


def test_the_server_binds_to_loopback_only(server):
    srv, _, _ = server
    assert srv.server_address[0] == "127.0.0.1"
    assert monitor_server.BIND == "127.0.0.1"


def test_the_page_renders_the_status_and_reloads_itself(server):
    _, base, tmp_path = server
    before = _tree(tmp_path)
    code, ctype, body = _get(base + "/")
    assert code == 200 and ctype.startswith("text/html")
    assert 'http-equiv="refresh" content="5"' in body
    assert 'apple-mobile-web-app-capable' in body and 'rel="manifest"' in body
    assert "T4 (stage review)" in body and "claude:opus (role verifier)" in body
    assert "150 of 1,000 (15%)" in body and 'style="width:15.0%"' in body
    assert "20261007T120000Z-0000beef" in body
    assert "This page only reads the run directory" in body
    assert _tree(tmp_path) == before


def test_status_json_is_the_same_dict_the_cli_prints(server):
    _, base, tmp_path = server
    code, ctype, body = _get(base + "/status.json")
    assert code == 200 and ctype.startswith("application/json")
    payload = json.loads(body)
    expected = read_status(tmp_path)
    for key in ("run_id", "task", "stage", "seat", "tokens_reported", "max_reported_tokens", "live"):
        assert payload["status"][key] == expected[key]
    assert [r["run"] for r in payload["history"]] == [r["run"] for r in run_history(tmp_path, limit=5)]


def test_query_parameters_switch_the_watched_project_and_the_form_keeps_them(server, tmp_path):
    _, base, _ = server
    other = tmp_path / "other"
    other.mkdir()
    code, _, body = _get(base + f"/?project={other}&series=")
    assert code == 200
    assert "Not running" in body and "no run directories" in body
    assert f'value="{other}"' in body


def test_a_blank_parameter_clears_a_launch_default_instead_of_restoring_it(tmp_path):
    # parse_qs drops blank values unless told to keep them; without that, a
    # server started with --series could never be pointed at a bare project.
    series = tmp_path / "series"
    series.mkdir()
    (series / "manifest.json").write_text("{}")
    _project(tmp_path)
    srv = monitor_server.MonitorServer(0, project=str(tmp_path), series=str(series), history=5)
    thread = threading.Thread(target=srv.serve_forever, daemon=True)
    thread.start()
    try:
        base = f"http://127.0.0.1:{srv.server_address[1]}"
        _, _, with_default = _get(base + "/status.json")
        assert json.loads(with_default)["status"]["series"] is not None
        _, _, cleared = _get(base + f"/status.json?project={tmp_path}&series=")
        assert json.loads(cleared)["status"]["series"] is None
    finally:
        srv.shutdown()
        srv.server_close()


def test_the_manifest_declares_a_standalone_app(server):
    _, base, _ = server
    code, ctype, body = _get(base + "/manifest.webmanifest")
    assert code == 200 and "manifest" in ctype
    assert json.loads(body)["display"] == "standalone"


def test_other_paths_are_not_found_and_nothing_is_served_from_disk(server, tmp_path):
    _, base, _ = server
    (tmp_path / "secret.txt").write_text("nope")
    for path in ("/secret.txt", "/../secret.txt", "/.quadratus/runs", "/report.md"):
        with pytest.raises(urllib.error.HTTPError) as err:
            _get(base + path)
        assert err.value.code == 404


def test_a_reader_failure_becomes_a_500_not_a_dead_server(server, monkeypatch):
    srv, base, _ = server

    def boom(*args, **kwargs):
        raise RuntimeError("disk on fire")

    monkeypatch.setattr(srv, "snapshot", boom)
    with pytest.raises(urllib.error.HTTPError) as err:
        _get(base + "/")
    assert err.value.code == 500 and b"disk on fire" in err.value.read()
    monkeypatch.undo()
    assert _get(base + "/manifest.webmanifest")[0] == 200


def test_render_html_escapes_what_the_run_files_say(tmp_path):
    run_dir = _project(tmp_path)
    (run_dir / "invocations.jsonl").write_text(json.dumps(dict(
        task="<script>alert(1)</script>", role="lead", canonical_model="x", invoked=True)) + "\n")
    page = monitor_server.render_html(read_status(tmp_path), [], project="<b>")
    assert "<script>alert(1)</script>" not in page
    assert "&lt;script&gt;alert(1)&lt;/script&gt;" in page
    assert 'value="&lt;b&gt;"' in page


def test_serve_announces_the_loopback_address_and_the_phone_steps(monkeypatch):
    lines = []
    served = {}

    class FakeServer:
        def __init__(self, port, **kw):
            served.update(kw, port=port)
            self.server_address = ("127.0.0.1", 7861 if port == 0 else port)

        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return False

        def serve_forever(self):
            raise KeyboardInterrupt

    monkeypatch.setattr(monitor_server, "MonitorServer", FakeServer)
    assert monitor_server.serve(0, project="/p", series="/s", history=3, announce=lines.append) == 0
    assert served["project"] == "/p" and served["series"] == "/s" and served["history"] == 3
    assert "http://127.0.0.1:7861/" in lines[0] and "read-only" in lines[0]
    assert "tailscale serve --bg 7861" in lines[1] and "Add to Home Screen" in lines[1]

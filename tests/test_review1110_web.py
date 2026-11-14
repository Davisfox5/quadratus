"""The two web surfaces no longer rely on "loopback is the lock".

Whole-repo review 2026-11-10, report gui_records:

- item 1: the Gradio GUI had no Host check and no token, so a request with
  ``Host: evil.example`` opened a project folder and could run commands;
- item 2: the monitor page answered any Host and read any ``?project=``;
- item 10: ``render_story`` raised on records ``monitor.py`` tolerates;
- item 16: ``quadratus-gui --port=8000`` served on 7860 in silence;
- item 17: a cleared Task limit raised TypeError out of the run generator;
- item 25: the launch agent logged to a fixed path under ``/tmp``.
"""

from __future__ import annotations

import asyncio
import http.client
import http.server
import json
import os
import plistlib
import re
import signal
import socket
import stat
import subprocess
import sys
import threading
import time
from pathlib import Path

import pytest

from quadratus import gui, local_http, monitor_server, monitor_story
from quadratus.config import Settings
from quadratus.local_http import LocalGuard, host_allowed, parse_allowed_hosts
from quadratus.monitor import read_status

REPO = Path(gui.__file__).resolve().parents[1]
RUN_ID = "20261007T120000Z-0000beef"


def _free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
        probe.bind(("127.0.0.1", 0))
        return probe.getsockname()[1]


def _request(port, path="/", *, method="GET", host=None, headers=None, body=None, hosts=None, timeout=30):
    """One raw request to 127.0.0.1:``port``. ``host`` replaces the Host
    header; ``hosts`` sends several. Returns (status, headers, text)."""
    conn = http.client.HTTPConnection("127.0.0.1", port, timeout=timeout)
    try:
        conn.putrequest(method, path, skip_host=True, skip_accept_encoding=True)
        for value in hosts if hosts is not None else [host if host is not None else f"127.0.0.1:{port}"]:
            conn.putheader("Host", value)
        payload = None
        if body is not None:
            payload = json.dumps(body).encode("utf-8")
            conn.putheader("Content-Type", "application/json")
            conn.putheader("Content-Length", str(len(payload)))
        for name, value in (headers or {}).items():
            conn.putheader(name, value)
        conn.endheaders(payload)
        resp = conn.getresponse()
        return resp.status, {k.lower(): v for k, v in resp.getheaders()}, resp.read().decode("utf-8", "replace")
    finally:
        conn.close()


# -- the shared Host rule -----------------------------------------------------

@pytest.mark.parametrize("values, port, extra, allowed", [
    (["127.0.0.1:7861"], 7861, (), True),
    (["localhost:7861"], 7861, (), True),
    (["LOCALHOST:7861"], 7861, (), True),
    (["[::1]:7861"], 7861, (), True),
    (["127.0.0.1:7862"], 7861, (), False),      # another local server's port
    (["127.0.0.1"], 7861, (), False),           # no port is port 80
    (["127.0.0.1"], 80, (), True),
    (["evil.example"], 7861, (), False),
    (["evil.example:7861"], 7861, (), False),   # the rebinding request
    (["127.0.0.1.evil.example:7861"], 7861, (), False),
    (["localhost.:7861"], 7861, (), False),
    ([], 7861, (), False),
    (["127.0.0.1:7861", "evil.example"], 7861, (), False),
    (["127.0.0.1:7861, evil.example"], 7861, (), False),
    (["127.0.0.1:07861"], 7861, (), False),
    ([None], 7861, (), False),
    (["mac.example.ts.net"], 7861, (("mac.example.ts.net", None),), True),
    (["mac.example.ts.net:443"], 7861, (("mac.example.ts.net", None),), True),
    (["Mac.Example.ts.net"], 7861, (("mac.example.ts.net", None),), True),
    (["mac.example.ts.net:443"], 7861, (("mac.example.ts.net", "8443"),), False),
    (["mac.example.ts.net:8443"], 7861, (("mac.example.ts.net", "8443"),), True),
    (["other.example.ts.net"], 7861, (("mac.example.ts.net", None),), False),
    (["xmac.example.ts.net"], 7861, (("mac.example.ts.net", None),), False),
])
def test_the_host_rule(values, port, extra, allowed):
    assert host_allowed(values, port, extra) is allowed


def test_extra_hosts_are_host_names_and_a_pasted_address_is_refused_by_name():
    assert parse_allowed_hosts([" Mac.Example.ts.net ", "", "tunnel.example.com:8443"], "SETTING") == (
        ("mac.example.ts.net", None), ("tunnel.example.com", "8443"))
    for bad in ("https://mac.example.ts.net", "mac.example.ts.net/path", "*", "a b", "*.example.com"):
        with pytest.raises(ValueError, match="SETTING: .* is not a host name"):
            parse_allowed_hosts([bad], "SETTING")


# -- item 2: the monitor page -------------------------------------------------

def _run(root, error="RunStalled: T1 twice"):
    run_dir = root / ".quadratus" / "runs" / RUN_ID
    run_dir.mkdir(parents=True)
    (run_dir / "invocations.jsonl").write_text(json.dumps(dict(
        task="T4", role="verifier", canonical_model="claude:opus", invoked=True, outcome="ok",
        seconds=2.0, input_tokens=100, output_tokens=50)) + "\n")
    (run_dir / "result.json").write_text(json.dumps(dict(completed=False, error=error)))
    return run_dir


def _serving(**kwargs):
    srv = monitor_server.MonitorServer(0, **kwargs)
    # A short poll so shutdown() returns at once; the default waits half a second per test.
    threading.Thread(target=srv.serve_forever, kwargs=dict(poll_interval=0.02), daemon=True).start()
    return srv


@pytest.fixture
def monitor(tmp_path):
    launch, other = tmp_path / "launch", tmp_path / "other"
    _run(launch, error="launch project error")
    _run(other, error="the other project's secret")
    srv = _serving(project=str(launch), history=5)
    try:
        yield srv, srv.server_address[1], launch, other
    finally:
        srv.shutdown()
        srv.server_close()


@pytest.mark.parametrize("path", ["/", "/status.json", "/manifest.webmanifest", "/nothing-here"])
@pytest.mark.parametrize("host", ["evil.example", "evil.example:{port}", "127.0.0.1:1", "127.0.0.1", ""])
def test_the_monitor_refuses_a_host_it_was_not_started_with(monitor, path, host):
    _, port, launch, _ = monitor
    code, headers, body = _request(port, path, host=host.format(port=port))
    assert code == 421
    assert headers["content-type"].startswith("text/plain")
    assert "QUADRATUS_MONITOR_ALLOW_HOST" in body and len(body.strip().splitlines()) == 1
    assert str(launch) not in body and RUN_ID not in body and "launch project error" not in body


def test_the_monitor_refuses_two_host_headers(monitor):
    _, port, _, _ = monitor
    assert _request(port, "/status.json", hosts=[f"127.0.0.1:{port}", "evil.example"])[0] == 421


def test_the_monitor_answers_to_its_loopback_names(monitor):
    _, port, _, _ = monitor
    for host in (f"127.0.0.1:{port}", f"localhost:{port}", f"[::1]:{port}", f"LocalHost:{port}"):
        code, _, body = _request(port, "/status.json", host=host)
        assert code == 200 and json.loads(body)["status"]["run_id"] == RUN_ID, host


def test_the_refusal_names_a_plain_host_and_never_echoes_anything_else(monitor):
    _, port, _, _ = monitor
    assert "the host name mac.example.ts.net" in _request(port, "/", host="mac.example.ts.net")[2]
    body = _request(port, "/", host="<script>alert(1)</script>")[2]
    assert "script" not in body and "that host name" in body


def test_an_admitted_host_name_is_answered_and_no_other(tmp_path):
    launch = tmp_path / "launch"
    _run(launch)
    srv = _serving(project=str(launch), allow_hosts=["Mac.Example.ts.net", "tunnel.example.com:8443"])
    try:
        port = srv.server_address[1]
        assert _request(port, "/status.json", host="mac.example.ts.net")[0] == 200
        assert _request(port, "/status.json", host="mac.example.ts.net:443")[0] == 200
        assert _request(port, "/status.json", host="tunnel.example.com:8443")[0] == 200
        assert _request(port, "/status.json", host="tunnel.example.com")[0] == 421
        assert _request(port, "/status.json", host="evil.example")[0] == 421
    finally:
        srv.shutdown()
        srv.server_close()


def test_a_path_that_was_not_admitted_gets_one_fixed_refusal_and_no_read(monitor, monkeypatch):
    srv, port, launch, other = monitor
    reads = []
    real = srv.snapshot
    monkeypatch.setattr(srv, "snapshot", lambda *a: reads.append(a) or real(*a))
    missing = launch.parent / "no-such-folder"
    answers = set()
    for route in ("/", "/status.json", "/index.html"):
        for query in (f"project={other}", f"project={missing}", f"series={other}", f"series={missing}",
                      f"project={launch}&series={other}", "project=/", "project=~", "project=%00",
                      # A NUL after "~" made the path comparison itself raise: a 500, not the refusal.
                      "project=~%00", "series=~nobody%00x/y"):
            code, headers, body = _request(port, f"{route}?{query}")
            assert code == 403, (route, query)
            assert headers["content-type"].startswith("text/plain")
            assert "secret" not in body and str(other) not in body and "does not exist" not in body
            answers.add(body)
    # One answer for a folder that exists and one that does not: no oracle.
    assert answers == {monitor_server.PATH_REFUSAL}
    assert "QUADRATUS_MONITOR_ALLOW_PROJECT" in monitor_server.PATH_REFUSAL
    assert reads == [], "a refused path is never read"


def test_the_launch_values_are_still_honoured_however_they_are_spelled(monitor):
    _, port, launch, _ = monitor
    for spelling in (str(launch), f"{launch}/", f"{launch}/./", f"{launch.parent}/other/../launch"):
        code, _, body = _request(port, f"/status.json?project={spelling}&series=")
        assert code == 200 and json.loads(body)["status"]["run_id"] == RUN_ID, spelling


def test_a_spelling_of_the_launch_value_reads_the_launch_folder_and_no_other(tmp_path):
    # The comparison is text, and text drops "link/.." where the filesystem
    # follows the link first. Reading the request's own spelling let
    # <base>/link/../launch pass as the launch folder and read a different one.
    base, elsewhere = tmp_path / "base", tmp_path / "elsewhere"
    launch = base / "launch"
    _run(launch, error="launch project error")
    _run(elsewhere / "launch", error="the other project's secret")
    (elsewhere / "sub").mkdir()
    (base / "link").symlink_to(elsewhere / "sub", target_is_directory=True)
    srv = _serving(project=str(launch), series=str(base / "series"), allow_projects=[str(base / "admitted")])
    try:
        port = srv.server_address[1]
        for field in ("project", "series"):
            for spelled in (base / "link" / ".." / "launch", base / "link" / ".." / "series",
                            base / "link" / ".." / "admitted"):
                code, _, body = _request(port, f"/status.json?{field}={spelled}")
                assert code == 200, (field, spelled)
                assert "secret" not in body and str(elsewhere) not in body, (field, spelled)
        code, _, body = _request(port, f"/status.json?project={base}/link/../launch&series=")
        status = json.loads(body)["status"]
        assert status["stop_reason"] == "launch project error" and status["project"] == str(launch)
        # What is read is the operator's own spelling, so the form shows that too.
        assert f'value="{launch}"' in _request(port, f"/?project={base}/link/../launch")[2]
    finally:
        srv.shutdown()
        srv.server_close()


def test_an_admitted_project_can_be_watched_and_the_form_offers_only_admitted_folders(tmp_path):
    launch, other = tmp_path / "launch", tmp_path / "other"
    _run(launch)
    _run(other, error="other error text")
    srv = _serving(project=str(launch), allow_projects=[f" {other} ", ""])
    try:
        port = srv.server_address[1]
        code, _, body = _request(port, f"/status.json?project={other}")
        assert code == 200 and "other error text" in body
        page = _request(port, f"/?project={other}")[2]
        assert "<form" in page and f'<option value="{other}">' in page
        assert _request(port, f"/?project={tmp_path}")[0] == 403
    finally:
        srv.shutdown()
        srv.server_close()


def test_the_page_draws_no_form_when_nothing_else_is_admitted(monitor):
    _, port, _, _ = monitor
    code, _, page = _request(port, "/")
    assert code == 200 and RUN_ID in page
    assert "<form" not in page and "<input" not in page and "<button" not in page
    # Drawn directly, as before, the form is unchanged and still escaped.
    drawn = monitor_server.render_html(read_status(None, None), [], project="<b>", admitted=['"><i>'])
    assert 'value="&lt;b&gt;"' in drawn and "<i>" not in drawn and "&quot;&gt;&lt;i&gt;" in drawn


def test_serve_takes_extra_hosts_and_projects_from_the_environment_too(monkeypatch):
    built, lines = {}, []

    class FakeServer:
        def __init__(self, port, **kw):
            built.update(kw)
            self.server_address = ("127.0.0.1", 7861)

        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return False

        def serve_forever(self):
            raise KeyboardInterrupt

    monkeypatch.setattr(monitor_server, "MonitorServer", FakeServer)
    monkeypatch.setenv("QUADRATUS_MONITOR_ALLOW_HOST", "mac.example.ts.net, tunnel.example.com:8443 ,")
    monkeypatch.setenv("QUADRATUS_MONITOR_ALLOW_PROJECT", "/abs/a, /abs/b")
    assert monitor_server.serve(0, project="/p", announce=lines.append, allow_hosts=["flag.example"],
                                allow_projects=["/abs/flag"]) == 0
    assert built["allow_hosts"] == ["flag.example", "mac.example.ts.net", "tunnel.example.com:8443"]
    assert built["allow_projects"] == ["/abs/flag", "/abs/a", "/abs/b"]
    assert "flag.example, mac.example.ts.net, tunnel.example.com:8443" in lines[2]
    assert "QUADRATUS_MONITOR_ALLOW_HOST" in lines[2]

    built.clear()
    lines.clear()
    monkeypatch.delenv("QUADRATUS_MONITOR_ALLOW_HOST")
    monkeypatch.delenv("QUADRATUS_MONITOR_ALLOW_PROJECT")
    assert monitor_server.serve(0, project="/p", announce=lines.append) == 0
    assert built["allow_hosts"] == [] and built["allow_projects"] == [], "off is the loopback-only page"
    assert "no other name" in lines[2]


def test_a_bad_extra_host_stops_the_monitor_before_it_serves(monkeypatch):
    lines = []
    with pytest.raises(ValueError, match="QUADRATUS_MONITOR_ALLOW_HOST"):
        monitor_server.MonitorServer(0, project="/p", allow_hosts=["http://x"])
    monkeypatch.setattr(monitor_server, "MonitorServer",
                        lambda *a, **kw: pytest.fail("nothing is served with a bad host entry"))
    monkeypatch.setenv("QUADRATUS_MONITOR_ALLOW_HOST", "https://mac.example.ts.net/")
    assert monitor_server.serve(0, project="/p", announce=lines.append) == 2
    assert lines == ["Error: QUADRATUS_MONITOR_ALLOW_HOST: 'https://mac.example.ts.net/' is not a host name. "
                     "Give the name alone, such as mac.example.ts.net, without http:// or a path."]


# -- item 10: the story panel never raises ------------------------------------

_STARTED = dict(kind="call-started", call_id="c1", role="lead", model="claude:opus", at="2026-10-07T12:00:00Z")

MALFORMED = {
    "receipts null": (dict(checks=[dict(passed=False, receipts=None)]), []),
    "receipts a string": (dict(checks=[dict(passed=False, receipts="x")]), []),
    "receipt not an object": (dict(checks=[dict(passed=False, receipts=[3, None])]), []),
    "report a list": (dict(checks=[dict(passed=False, receipts=[dict(report=["x"])])]), []),
    "failures null": (dict(checks=[dict(passed=False, receipts=[dict(report=dict(failures=None))])]), []),
    "failure not an object": (dict(checks=[dict(passed=False, receipts=[dict(report=dict(
        failures=["x", 3, None, dict(nodeid=["a"]), dict(nodeid="tests/a.py::test_kept_name")]))])]), []),
    "list task started": (dict(completed=False, error="x"), [dict(_STARTED, task=["t1"])]),
    "dict task started": (dict(completed=False, error="x"), [dict(_STARTED, task={"a": 1})]),
    "list task unfinished": (None, [dict(_STARTED, task=["t1"])]),
    "list artifact": (None, [dict(kind="reply", artifact=["a"], call_id="c1", model="claude:opus")]),
    "list call id": (None, [dict(kind="reply", call_id=["c1"]), dict(kind="call-ended", call_id=["c1"])]),
    "dict call id": (None, [dict(kind="call-ended", call_id={"a": 1}, role="lead")]),
    "everything a list": (dict(checks=[[]], goal=["g"], tasks=[1], in_flight=[1], source_changed=[1]),
                          [dict(kind=["k"], task=["t"], call_id=["c"], role=["r"], model=["m"], at=["a"],
                                stage=["s"], message=["m"], goal=["g"], artifact=["a"])]),
}


@pytest.mark.parametrize("label", sorted(MALFORMED))
def test_the_story_renders_a_malformed_record_instead_of_raising(tmp_path, label):
    result, events = MALFORMED[label]
    run_dir = tmp_path / ".quadratus" / "runs" / RUN_ID
    run_dir.mkdir(parents=True)
    if result is not None:
        (run_dir / "result.json").write_text(json.dumps(result))
    if events:
        (run_dir / "activity.jsonl").write_text("".join(json.dumps(e) + "\n" for e in events))
    page = monitor_story.render_story(read_status(tmp_path))
    assert isinstance(page, str) and 'class="qm"' in page
    # Each field was checked where it is used: the panel is the ordinary one.
    assert "Unreadable record" not in page
    # The GUI's Monitor tab is this same read, every five seconds.
    shown, rows = gui.monitor_view(str(tmp_path))
    assert 'class="qm"' in shown and isinstance(rows, list)


def test_a_malformed_failure_list_still_shows_the_failures_that_can_be_read(tmp_path):
    result, _ = MALFORMED["failure not an object"]
    run_dir = tmp_path / ".quadratus" / "runs" / RUN_ID
    run_dir.mkdir(parents=True)
    (run_dir / "result.json").write_text(json.dumps(result))
    page = monitor_story.render_story(read_status(tmp_path))
    # A broken entry never turns a failed check into anything else.
    assert "Did not pass" in page and "test_kept_name" in page and "Passed" not in page


def test_a_digit_run_too_long_to_be_a_count_states_no_count(tmp_path):
    run_dir = tmp_path / ".quadratus" / "runs" / RUN_ID
    (run_dir / "artifacts").mkdir(parents=True)
    (run_dir / "artifacts" / ("a" * 12 + ".json")).write_text(json.dumps(dict(kind="check-output", author="harness")))
    (run_dir / "artifacts" / ("a" * 12 + ".txt")).write_text("# pass " + "9" * 5000 + "\n# fail 0\n")
    page = monitor_story.render_story(read_status(tmp_path))
    assert "Unreadable record" not in page and "result details unavailable" in page
    assert "passed" not in page.split("<details>")[0]


def test_anything_else_becomes_an_unreadable_record_panel_that_claims_nothing(tmp_path, monkeypatch):
    run_dir = tmp_path / ".quadratus" / "runs" / RUN_ID
    run_dir.mkdir(parents=True)
    (run_dir / "result.json").write_text(json.dumps(dict(completed=True, checks=[dict(passed=True)])))
    status = read_status(tmp_path)

    def boom(_status):
        raise RuntimeError("<b>boom</b>")
    monkeypatch.setattr(monitor_story, "_render_story", boom)
    page = monitor_story.render_story(status)
    assert "Unreadable record" in page and "RuntimeError" in page and "<b>boom</b>" not in page
    for claim in ("Passed", "Completed", "Finished", "goal recorded as met"):
        assert claim not in page.split("<details>")[0], claim
    assert RUN_ID in page, "the technical facts are still shown"
    # A status that is not even a dict: the fallback's own extras fail quietly.
    monkeypatch.undo()
    for broken in (None, [], "status", 3):
        assert "Unreadable record" in monitor_story.render_story(broken)


# -- item 1: the GUI guard, rule by rule --------------------------------------

PORT = 7860
TOKEN = "T0ken-for_the-test"


class _Inner:
    """A stand-in for the whole GUI application: records what reaches it."""

    def __init__(self):
        self.seen = []

    async def __call__(self, scope, receive, send):
        self.seen.append((scope["type"], scope.get("path")))
        if scope["type"] == "http":
            await send({"type": "http.response.start", "status": 200, "headers": []})
            await send({"type": "http.response.body", "body": b"inner"})


def _scope(path="/", *, method="GET", host=f"127.0.0.1:{PORT}", query="", headers=(), kind="http"):
    raw = [(b"host", host.encode())] if host is not None else []
    raw += [(name.lower().encode(), value.encode()) for name, value in headers]
    return {"type": kind, "method": method, "path": path, "query_string": query.encode(), "headers": raw}


def _through(guard, scope):
    sent = []

    async def receive():
        return {"type": "http.request", "body": b"", "more_body": False}

    async def send(message):
        sent.append(message)

    asyncio.run(guard(scope, receive, send))
    status = next((m["status"] for m in sent if m["type"] == "http.response.start"), None)
    headers = {k.decode(): v.decode() for m in sent if m["type"] == "http.response.start" for k, v in m["headers"]}
    body = b"".join(m.get("body", b"") for m in sent if m["type"] == "http.response.body").decode()
    return status, headers, body, sent


def _guard(token=TOKEN):
    inner = _Inner()
    return LocalGuard(inner, port=PORT, token=token), inner


COOKIE = ("Cookie", f"quadratus_gui_{PORT}={TOKEN}")


@pytest.mark.parametrize("host", ["evil.example", f"evil.example:{PORT}", "127.0.0.1:7861", "127.0.0.1", None])
@pytest.mark.parametrize("token", [TOKEN, None])
def test_the_guard_refuses_a_foreign_host_even_with_the_cookie_or_the_token(host, token):
    guard, inner = _guard(token)
    for scope in (_scope("/gradio_api/call/open_project", method="POST", host=host, headers=[COOKIE]),
                  _scope("/", host=host, query=f"token={TOKEN}")):
        status, headers, body, _ = _through(guard, scope)
        assert status == 421 and "set-cookie" not in headers
        assert f"http://127.0.0.1:{PORT}" in body and "no setting admits another name" in body
    assert inner.seen == []


@pytest.mark.parametrize("path, method", [
    ("/", "GET"), ("/config", "GET"), ("/gradio_api/call/run_selected", "POST"),
    ("/gradio_api/queue/join", "POST"), ("/gradio_api/queue/data", "GET"),
    ("/gradio_api/file=/etc/passwd", "GET"), ("/gradio_api/upload", "POST"), ("/static/x.js", "GET"),
    ("/favicon.ico", "GET"), ("/", "HEAD"), ("/", "POST"),
])
def test_without_the_cookie_nothing_is_served(path, method):
    guard, inner = _guard()
    status, headers, body, _ = _through(guard, _scope(path, method=method))
    assert status == 403 and inner.seen == []
    assert "access token" in body and "QUADRATUS_GUI_NO_TOKEN=1" in body
    assert headers["content-type"].startswith("text/plain") and "set-cookie" not in headers


def test_the_token_address_sets_the_cookie_and_moves_on_to_the_page():
    guard, inner = _guard()
    status, headers, body, _ = _through(guard, _scope("/", query=f"token={TOKEN}"))
    assert status == 200 and inner.seen == [], "the token page is the guard's own; the app is not reached"
    assert headers["set-cookie"] == f"quadratus_gui_{PORT}={TOKEN}; Path=/; HttpOnly; SameSite=Strict"
    assert headers["cache-control"] == "no-store" and headers["referrer-policy"] == "no-referrer"
    assert '<meta http-equiv="refresh" content="0; url=/">' in body and TOKEN not in body
    # The cookie it set is what every later request needs.
    cookie = headers["set-cookie"].split(";")[0]
    status, _, body, _ = _through(guard, _scope("/gradio_api/call/run_selected", method="POST",
                                                headers=[("Cookie", f"other=1; {cookie}; more=2")]))
    assert (status, body) == (200, "inner") and inner.seen == [("http", "/gradio_api/call/run_selected")]


@pytest.mark.parametrize("query", ["token=wrong", "token=", f"token={TOKEN}x", f"token={TOKEN[:-1]}",
                                   f"TOKEN={TOKEN}", f"x=1&token=wrong&y={TOKEN}"])
def test_a_wrong_token_sets_no_cookie(query):
    guard, inner = _guard()
    status, headers, _, _ = _through(guard, _scope("/", query=query))
    assert status == 403 and "set-cookie" not in headers and inner.seen == []


@pytest.mark.parametrize("cookie", [
    f"quadratus_gui_{PORT}=wrong", f"quadratus_gui_{PORT}=", f"quadratus_gui_7861={TOKEN}",
    f"quadratus_gui={TOKEN}", f"x=quadratus_gui_{PORT}={TOKEN}", TOKEN, f"quadratus_gui_{PORT}={TOKEN}x",
])
def test_only_this_launch_and_this_port_s_cookie_is_accepted(cookie):
    guard, inner = _guard()
    assert _through(guard, _scope("/config", headers=[("Cookie", cookie)]))[0] == 403
    assert inner.seen == []


def test_the_token_is_only_traded_on_a_get_of_the_root():
    guard, inner = _guard()
    for scope in (_scope("/", method="POST", query=f"token={TOKEN}"),
                  _scope("/config", query=f"token={TOKEN}"),
                  _scope("/gradio_api/call/run_selected", method="POST", query=f"token={TOKEN}")):
        status, headers, _, _ = _through(guard, scope)
        assert status == 403 and "set-cookie" not in headers
    assert inner.seen == []


@pytest.mark.parametrize("headers", [
    [("Origin", "http://127.0.0.1:9999")],          # another local server: same site, cookie sent
    [("Origin", "http://localhost:7860")],          # not the name this request was sent to
    [("Origin", "https://127.0.0.1:7860")],
    [("Origin", "http://evil.example")],
    [("Origin", "null")],
    [("Origin", f"http://127.0.0.1:{PORT}"), ("Origin", "http://evil.example")],
    [("Sec-Fetch-Site", "same-site")],
    [("Sec-Fetch-Site", "cross-site")],
    [("Sec-Fetch-Site", "cross-site"), ("Sec-Fetch-Mode", "no-cors"), ("Sec-Fetch-Dest", "image")],
    [("Sec-Fetch-Site", "same-site"), ("Sec-Fetch-Mode", "navigate"), ("Sec-Fetch-Dest", "iframe")],
    [("Sec-Fetch-Site", "same-origin"), ("Sec-Fetch-Site", "cross-site")],
])
@pytest.mark.parametrize("token", [TOKEN, None])
def test_a_request_from_another_page_is_refused_even_with_the_cookie(headers, token):
    guard, inner = _guard(token)
    for method in ("GET", "POST"):
        status, _, body, _ = _through(guard, _scope("/gradio_api/call/run_selected", method=method,
                                                    headers=[COOKIE, *headers]))
        assert status == 403 and "another web page" in body
    assert inner.seen == []


def test_the_page_s_own_requests_and_a_followed_link_pass():
    guard, inner = _guard()
    own = [("Origin", f"http://127.0.0.1:{PORT}"), ("Sec-Fetch-Site", "same-origin")]
    assert _through(guard, _scope("/gradio_api/queue/join", method="POST", headers=[COOKIE, *own]))[0] == 200
    assert _through(guard, _scope("/config", headers=[COOKIE, ("Sec-Fetch-Site", "none")]))[0] == 200
    link = [("Sec-Fetch-Site", "cross-site"), ("Sec-Fetch-Mode", "navigate"), ("Sec-Fetch-Dest", "document")]
    assert _through(guard, _scope("/", headers=[COOKIE, *link]))[0] == 200
    # The same link without the cookie, or as a POST, gets nothing.
    assert _through(guard, _scope("/", headers=link))[0] == 403
    assert _through(guard, _scope("/", method="POST", headers=[COOKIE, *link]))[0] == 403
    # The private file the launch opens is such a link, carrying the token.
    status, headers, _, _ = _through(guard, _scope("/", query=f"token={TOKEN}", headers=link))
    assert status == 200 and "set-cookie" in headers
    assert len(inner.seen) == 3


def test_a_websocket_is_closed_before_it_is_accepted_and_lifespan_passes():
    guard, inner = _guard()
    for scope in (_scope("/ws", kind="websocket", host="evil.example", headers=[COOKIE]),
                  _scope("/ws", kind="websocket"),
                  _scope("/ws", kind="websocket", headers=[COOKIE, ("Origin", "http://127.0.0.1:9999")])):
        assert _through(guard, scope)[3] == [{"type": "websocket.close", "code": 1008}]
    assert inner.seen == []
    _through(guard, _scope("/ws", kind="websocket", headers=[COOKIE, ("Origin", f"http://127.0.0.1:{PORT}")]))
    _through(guard, {"type": "lifespan"})
    assert inner.seen == [("websocket", "/ws"), ("lifespan", None)]
    assert _through(guard, {"type": "something-new", "headers": []})[3] == [] and len(inner.seen) == 2


def test_with_the_token_off_only_the_token_step_is_skipped():
    guard, inner = _guard(None)
    assert _through(guard, _scope("/config"))[0] == 200
    # ?token= is then an ordinary query for the app, not an exchange.
    status, headers, body, _ = _through(guard, _scope("/", query="token=anything"))
    assert (status, body) == (200, "inner") and "set-cookie" not in headers
    assert _through(guard, _scope("/config", host="evil.example"))[0] == 421
    # Only None is "off": an empty or weak token is a mistake, never a silent opt-out.
    for weak in ("", "short", "has a space in it!!", "semi;colon;semi;colon", 12345678901234567890):
        with pytest.raises(ValueError, match="access token"):
            LocalGuard(_Inner(), port=PORT, token=weak)


def test_the_cookie_comparison_is_constant_time_and_tokens_are_fresh(monkeypatch):
    compared = []
    real = local_http.hmac.compare_digest
    monkeypatch.setattr(local_http.hmac, "compare_digest", lambda a, b: compared.append((a, b)) or real(a, b))
    guard, _ = _guard()
    _through(guard, _scope("/", query="token=guess"))
    _through(guard, _scope("/config", headers=[("Cookie", f"quadratus_gui_{PORT}=guess")]))
    assert compared == [(b"guess", TOKEN.encode())] * 2
    monkeypatch.delenv("QUADRATUS_GUI_NO_TOKEN", raising=False)
    tokens = {gui.launch_token() for _ in range(20)}
    assert len(tokens) == 20 and all(re.fullmatch(r"[A-Za-z0-9_-]{43}", t) for t in tokens)
    for off in ("1", "true", "YES"):
        monkeypatch.setenv("QUADRATUS_GUI_NO_TOKEN", off)
        assert gui.launch_token() is None
    for not_off in ("0", "", "no"):
        monkeypatch.setenv("QUADRATUS_GUI_NO_TOKEN", not_off)
        assert gui.launch_token() is not None


# -- item 1: the real application on a real port ------------------------------

@pytest.fixture(scope="module")
def served(tmp_path_factory):
    """The interface exactly as ``quadratus-gui`` serves it, on a free port."""
    pytest.importorskip("gradio")
    work = tmp_path_factory.mktemp("gui")
    project = work / "project"
    project.mkdir()
    (project / "app.py").write_text("x = 1\n")
    port = _free_port()
    token = local_http.new_token()
    server = gui.local_server(gui.guarded_app(gui.build_interface(Settings()), port, token), port)
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()
    deadline = time.monotonic() + 60
    while not server.started and time.monotonic() < deadline:
        time.sleep(0.05)
    assert server.started, "the GUI server did not start"
    try:
        yield dict(port=port, token=token, work=work, project=project, base=f"http://127.0.0.1:{port}")
    finally:
        server.should_exit = True
        thread.join(15)


def _cookie(served):
    code, headers, _ = _request(served["port"], f"/?token={served['token']}")
    assert code == 200
    return {"Cookie": headers["set-cookie"].split(";")[0]}


def test_the_server_is_on_loopback_and_a_wrong_host_is_refused(served):
    port, target = served["port"], served["work"] / "made-by-evil-host"
    for host in ("evil.example", f"evil.example:{port}", "127.0.0.1:1"):
        code, _, body = _request(port, "/gradio_api/call/open_project", method="POST", host=host,
                                 body={"data": [str(target), "", ""]}, headers=_cookie(served))
        assert code == 421 and "answers only at" in body
        assert _request(port, "/", host=host)[0] == 421
    assert not target.exists(), "the request the review made with Host: evil.example no longer opens a folder"


def test_no_cookie_is_refused_on_every_kind_of_route(served):
    port, target = served["port"], served["work"] / "made-without-cookie"
    code, _, body = _request(port, "/gradio_api/call/open_project", method="POST",
                             body={"data": [str(target), "", ""]})
    assert code == 403 and "access token" in body and not target.exists()
    for method, path in (("GET", "/"), ("GET", "/config"), ("GET", "/gradio_api/info"),
                         ("POST", "/gradio_api/queue/join"), ("GET", "/gradio_api/queue/data?session_hash=x"),
                         ("GET", f"/gradio_api/file={served['project'] / 'app.py'}"),
                         ("POST", "/gradio_api/upload"), ("GET", "/theme.css"), ("GET", "/favicon.ico"),
                         ("GET", "/gradio_api/call/monitor_view/abc"), ("GET", "/docs"), ("GET", "/openapi.json")):
        assert _request(port, path, method=method)[0] == 403, (method, path)
    assert _request(port, "/?token=not-the-token")[0] == 403


def test_the_token_address_sets_the_cookie_and_the_app_then_works_end_to_end(served):
    port = served["port"]
    code, headers, body = _request(port, f"/?token={served['token']}")
    assert code == 200 and 'content="0; url=/"' in body
    assert headers["set-cookie"] == (f"quadratus_gui_{port}={served['token']}; Path=/; HttpOnly; "
                                     "SameSite=Strict")
    cookie = _cookie(served)
    code, _, page = _request(port, "/", headers=cookie)
    assert code == 200 and "Quadratus" in page and len(page) > 10_000
    code, _, config = _request(port, "/config", headers=cookie)
    assert code == 200 and json.loads(config)["root"] == served["base"]
    # One real call through the queue and its event stream: the Project tab's policy preview.
    # One value per input, as the page sends them: the opened folder's slot (session state,
    # so the server uses its own), the paths, the forbidden paths, the edit switch and the
    # Trust section's two inputs a preview reads (the policy approval and "Run only my own
    # checks"), which the preview gained with that section.
    code, _, queued = _request(port, "/gradio_api/call/policy_preview_ui", method="POST", headers=cookie,
                               body={"data": [str(served["project"]), "", "", False, "", False]})
    assert code == 200
    code, _, stream = _request(port, f"/gradio_api/call/policy_preview_ui/{json.loads(queued)['event_id']}",
                               headers=cookie)
    assert code == 200 and "event: complete" in stream and "Plan hash" in stream
    # And the Monitor tab's read.
    code, _, queued = _request(port, "/gradio_api/call/monitor_view", method="POST", headers=cookie,
                               body={"data": ["", ""]})
    assert code == 200
    stream = _request(port, f"/gradio_api/call/monitor_view/{json.loads(queued)['event_id']}", headers=cookie)[2]
    assert "event: complete" in stream and "Enter a project folder" in stream


def test_another_local_page_cannot_ride_the_cookie(served):
    port, target = served["port"], served["work"] / "made-by-other-origin"
    cookie = _cookie(served)
    for extra in ({"Origin": "http://127.0.0.1:9999"}, {"Origin": "http://localhost:9999"},
                  {"Sec-Fetch-Site": "same-site"}, {"Sec-Fetch-Site": "cross-site", "Sec-Fetch-Mode": "no-cors"}):
        code, _, body = _request(port, "/gradio_api/call/open_project", method="POST",
                                 body={"data": [str(target), "", ""]}, headers={**cookie, **extra})
        assert code == 403 and "another web page" in body, extra
    assert not target.exists()
    # Gradio's own CORS rule admits any localhost origin with credentials; the guard answers first.
    code, headers, _ = _request(port, "/config", headers={**cookie, "Origin": "http://127.0.0.1:9999"})
    assert code == 403 and "access-control-allow-origin" not in headers


def test_a_real_browser_gets_in_through_the_private_file_and_a_hostile_local_page_does_not(served, tmp_path):
    sync_api = pytest.importorskip("playwright.sync_api")
    base, port, project = served["base"], served["port"], served["project"]
    opener = tmp_path / "open.html"
    opener.write_text(f'<!doctype html><meta http-equiv="refresh" content="0; url={base}/?token={served["token"]}">')
    hostile_root = tmp_path / "hostile"
    hostile_root.mkdir()
    (hostile_root / "index.html").write_text("<!doctype html><title>hostile</title>")

    class Quiet(http.server.SimpleHTTPRequestHandler):
        def __init__(self, *args, **kwargs):
            super().__init__(*args, directory=str(hostile_root), **kwargs)

        def log_message(self, *args):
            pass

    hostile = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Quiet)
    threading.Thread(target=hostile.serve_forever, daemon=True).start()
    target = tmp_path / "made-by-hostile-page"
    try:
        with sync_api.sync_playwright() as p:
            try:
                browser = p.chromium.launch()
            except Exception as exc:  # noqa: BLE001 -- no browser on this machine
                pytest.skip(f"Chromium is not available: {exc}")
            try:
                context = browser.new_context()
                page = context.new_page()
                page.set_default_timeout(90_000)
                assert page.goto(base + "/").status == 403, "no cookie yet"
                # As the launch opens it: a file page, the token address, then the app.
                page.goto(opener.as_uri())
                page.wait_for_url(base + "/")
                page.wait_for_selector("text=Open or create project")
                (cookie,) = context.cookies(base)
                assert (cookie["name"], cookie["httpOnly"], cookie["sameSite"], cookie["path"]) == (
                    f"quadratus_gui_{port}", True, "Strict", "/")
                assert page.evaluate("document.cookie") == "", "script cannot read the token"
                # The page's own requests pass the guard: the Project tab, then the Monitor tab.
                page.get_by_label("Project folder").first.fill(str(project))
                page.get_by_role("button", name="Open or create project").click()
                page.wait_for_selector("text=files found")
                page.get_by_role("button", name="Preview policy").click()
                page.wait_for_selector("text=Plan hash")
                page.get_by_role("tab", name="Monitor").click()
                page.wait_for_selector("text=No activity yet")
                # Another local port is the same site, so this browser sends it the cookie.
                other = context.new_page()
                other.set_default_timeout(90_000)
                other.goto(f"http://127.0.0.1:{hostile.server_address[1]}/")
                outcome = other.evaluate("""async ([base, target]) => {
                    const out = {};
                    const body = JSON.stringify({data: [target, '', '']});
                    const tries = {
                        read: [base + '/config', {credentials: 'include'}],
                        post: [base + '/gradio_api/call/open_project', {method: 'POST', credentials: 'include',
                               headers: {'Content-Type': 'application/json'}, body}],
                        nocors: [base + '/gradio_api/call/open_project', {method: 'POST', credentials: 'include',
                                 mode: 'no-cors', headers: {'Content-Type': 'text/plain'}, body}],
                    };
                    for (const [name, [url, init]] of Object.entries(tries)) {
                        try { const r = await fetch(url, init); out[name] = r.type + ' ' + r.status; }
                        catch (e) { out[name] = 'blocked'; }
                    }
                    return out;
                }""", [base, str(target)])
                assert outcome == {"read": "blocked", "post": "blocked", "nocors": "opaque 0"}
                other.set_content(f'<iframe src="{base}/"></iframe>')
                other.frames[-1].wait_for_selector("text=another web page")
            finally:
                browser.close()
    finally:
        hostile.shutdown()
        hostile.server_close()
    time.sleep(0.5)
    assert not target.exists(), "the hostile page's request reached the Project tab"


# -- item 1: the launch itself ------------------------------------------------

class _FakeServer:
    started, should_exit = True, False

    def __init__(self, ran):
        self.ran = ran

    def run(self):
        self.ran()


def _fake_launch(monkeypatch, seen, opened, wait_for_open=True):
    pytest.importorskip("gradio")
    monkeypatch.delenv("QUADRATUS_GUI_PORT", raising=False)
    monkeypatch.setattr(gui, "_port_busy", lambda port: False)
    monkeypatch.setattr(gui, "build_interface", lambda **kw: "demo")

    def guarded(demo, port, token, *, favicon=None):
        seen.update(demo=demo, port=port, token=token)
        return "app"

    def ran():
        deadline = time.monotonic() + 5
        while wait_for_open and not opened and time.monotonic() < deadline:
            time.sleep(0.01)
        seen["ran"] = True
    monkeypatch.setattr(gui, "guarded_app", guarded)
    monkeypatch.setattr(gui, "local_server", lambda app, port: seen.update(app=app) or _FakeServer(ran))
    monkeypatch.setattr(gui, "open_in_browser", lambda url: opened.append(url))


def test_the_launch_prints_and_opens_the_token_address_and_serves_the_guarded_app(monkeypatch, capsys):
    seen, opened = {}, []
    _fake_launch(monkeypatch, seen, opened)
    monkeypatch.delenv("QUADRATUS_GUI_NO_TOKEN", raising=False)
    assert gui.main(["--port=8123"]) == 0
    out = capsys.readouterr().out
    assert seen["ran"] and seen["app"] == "app" and seen["port"] == 8123
    assert re.fullmatch(r"[A-Za-z0-9_-]{43}", seen["token"])
    address = f"http://127.0.0.1:8123/?token={seen['token']}"
    assert f"Quadratus is serving on {address}\n" in out and "QUADRATUS_GUI_NO_TOKEN=1" in out
    assert opened == [address]


def test_no_browser_prints_only_and_the_token_can_be_turned_off_out_loud(monkeypatch, capsys):
    seen, opened = {}, []
    _fake_launch(monkeypatch, seen, opened, wait_for_open=False)
    monkeypatch.delenv("QUADRATUS_GUI_NO_TOKEN", raising=False)
    assert gui.main(["--port", "8123", "--no-browser"]) == 0
    assert opened == [] and seen["token"] and "?token=" in capsys.readouterr().out
    monkeypatch.setenv("QUADRATUS_GUI_NO_TOKEN", "1")
    assert gui.main(["--port", "8123", "--no-browser"]) == 0
    out = capsys.readouterr().out
    assert seen["token"] is None and "?token=" not in out
    assert "access token turned off (QUADRATUS_GUI_NO_TOKEN)" in out and "run commands as you" in out


def test_sharing_is_still_refused_at_launch(monkeypatch, capsys):
    seen, opened = {}, []
    _fake_launch(monkeypatch, seen, opened, wait_for_open=False)
    monkeypatch.setenv("QUADRATUS_ALLOW_SHARE", "1")
    monkeypatch.setenv("GRADIO_SHARE", "true")
    assert gui.main(["--no-browser"]) == 0
    assert "Refusing to enable Gradio sharing" in capsys.readouterr().err


def test_the_gui_server_binds_to_loopback_only():
    pytest.importorskip("uvicorn")
    server = gui.local_server(lambda *a: None, 8123)
    assert server.config.host == "127.0.0.1" and server.config.port == 8123


def test_the_browser_is_handed_a_private_file_and_never_the_token(monkeypatch):
    import webbrowser
    handed = []
    monkeypatch.setattr(webbrowser, "open", lambda url: handed.append(url) or True)
    monkeypatch.setattr(gui, "_has_desktop", lambda: True)
    address = "http://127.0.0.1:8123/?token=SECRET&x=<b>"
    folder = gui.open_in_browser(address)
    try:
        (uri,) = handed
        assert uri.startswith("file://") and "SECRET" not in uri, "the token is not a command-line argument"
        page = folder / "open.html"
        assert uri == page.as_uri()
        assert stat.S_IMODE(folder.stat().st_mode) == 0o700 and stat.S_IMODE(page.stat().st_mode) == 0o600
        text = page.read_text()
        assert 'content="0; url=http://127.0.0.1:8123/?token=SECRET&amp;x=&lt;b&gt;"' in text
    finally:
        import shutil
        shutil.rmtree(folder, ignore_errors=True)
    # No desktop: nothing is opened (a text browser would take the terminal).
    handed.clear()
    monkeypatch.setattr(gui, "_has_desktop", lambda: False)
    assert gui.open_in_browser(address) is None and handed == []
    # A browser that cannot be started never fails the launch.
    monkeypatch.setattr(gui, "_has_desktop", lambda: True)
    monkeypatch.setattr(webbrowser, "open", lambda url: (_ for _ in ()).throw(RuntimeError("no browser")))
    folder = gui.open_in_browser(address)
    assert folder is not None
    import shutil
    shutil.rmtree(folder, ignore_errors=True)


def test_the_installed_entry_point_serves_behind_the_token_and_stops_on_interrupt(tmp_path):
    pytest.importorskip("gradio")
    port = _free_port()
    env = {k: v for k, v in os.environ.items()
           if k not in ("DISPLAY", "WAYLAND_DISPLAY", "QUADRATUS_GUI_NO_TOKEN", "QUADRATUS_GUI_PORT")}
    env.update(PYTHONPATH=str(REPO) + os.pathsep + env.get("PYTHONPATH", ""), PYTHONUNBUFFERED="1")
    child = subprocess.Popen([sys.executable, "-m", "quadratus.gui", f"--port={port}", "--no-browser"],
                             cwd=tmp_path, env=env, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
    try:
        lines = []
        reader = threading.Thread(target=lambda: lines.append(child.stdout.readline()), daemon=True)
        reader.start()
        reader.join(180)
        first = lines[0] if lines else ""
        found = re.search(rf"Quadratus is serving on (http://127\.0\.0\.1:{port}/\?token=([A-Za-z0-9_-]+))\s", first)
        assert found, f"no launch line within the wait: {first!r}"
        deadline = time.monotonic() + 60
        while True:
            try:
                assert _request(port, "/config", timeout=5)[0] == 403, "no cookie, no page"
                break
            except OSError:
                assert time.monotonic() < deadline and child.poll() is None, "the GUI never listened"
                time.sleep(0.1)
        assert _request(port, "/config", host="evil.example")[0] == 421
        code, headers, _ = _request(port, f"/?token={found[2]}")
        assert code == 200
        assert _request(port, "/config", headers={"Cookie": headers["set-cookie"].split(";")[0]})[0] == 200
        child.send_signal(signal.SIGINT)
        assert child.wait(timeout=30) == 0
    finally:
        if child.poll() is None:
            child.kill()
            child.wait()
        child.stdout.close()


# -- item 16: the quadratus-gui command line ----------------------------------

@pytest.mark.parametrize("argv, expected", [
    (["--port=8000"], 8000),
    (["--port", "8000"], 8000),
    (["--no-browser", "--port=8000"], 8000),
    (["--port", "8000", "--no-browser"], 8000),
    (["--no-browser"], 7860),
    ([], 7860),
])
def test_both_spellings_of_the_port_flag_are_read(monkeypatch, argv, expected):
    monkeypatch.delenv("QUADRATUS_GUI_PORT", raising=False)
    assert gui.gui_port(argv) == expected


@pytest.mark.parametrize("argv", [["--port=80"], ["--port=abc"], ["--port="], ["--port=99999"], ["--port=-1"],
                                  ["--port=8000x"], ["--port=٨٠٠٠"], ["--port=" + "9" * 5000]])
def test_a_bad_port_in_the_equals_form_is_refused_by_name(monkeypatch, capsys, argv):
    monkeypatch.delenv("QUADRATUS_GUI_PORT", raising=False)
    with pytest.raises(ValueError, match="--port must be a port number from 1024 to 65535"):
        gui.gui_port(argv)
    assert gui.main(argv) == 2
    assert "Error: --port must be a port number" in capsys.readouterr().err


@pytest.mark.parametrize("argv", [["--bogus"], ["--port=8000", "--share"], ["extra"], ["--por", "8000"],
                                  ["--port", "8000", "8001"], ["-p", "8000"]])
def test_an_unknown_flag_is_an_error_with_the_usage_not_a_silent_default(monkeypatch, capsys, argv):
    monkeypatch.delenv("QUADRATUS_GUI_PORT", raising=False)
    monkeypatch.setattr(gui, "build_interface", lambda **kw: pytest.fail("nothing is built for a bad command line"))
    for entry in (gui.gui_port, gui.main):
        with pytest.raises(SystemExit) as stop:
            entry(argv)
        assert stop.value.code == 2
        err = capsys.readouterr().err
        assert "usage: quadratus-gui" in err and "unrecognized arguments" in err


def test_help_names_the_flags(capsys):
    with pytest.raises(SystemExit) as stop:
        gui.main(["--help"])
    assert stop.value.code == 0
    out = capsys.readouterr().out
    assert "--port" in out and "--no-browser" in out and "QUADRATUS_GUI_PORT" in out


# -- item 17: a cleared Task limit --------------------------------------------

def test_a_cleared_task_limit_reaches_the_runner_s_refusal_instead_of_raising(tmp_path, monkeypatch):
    seen = {}

    def refusing_runner(goal, folder, settings, **kw):
        seen.update(kw)
        raise ValueError("max_tasks must be at least 1")
    monkeypatch.setattr("quadratus.project_run.run_project", refusing_runner)
    for cleared in (None, "", "abc"):
        seen.clear()
        outputs = list(gui.run_project_ui("goal", str(tmp_path), False, "", "adversarial", cleared, Settings()))
        assert seen["max_tasks"] is None, "the guarded value is what the runner is given"
        assert outputs[-1] == ("Run failed: max_tasks must be at least 1", "", [])
    seen.clear()
    list(gui.run_project_ui("goal", str(tmp_path), False, "", "adversarial", 7.0, Settings()))
    assert seen["max_tasks"] == 7 and type(seen["max_tasks"]) is int


def test_the_real_runner_refuses_a_cleared_task_limit_before_it_touches_the_project(tmp_path, monkeypatch):
    # This calls the real runner, and a vendor CLI may be installed and
    # signed in where the tests run. If the refusal ever moved, the test
    # must fail here, not start a run on someone's subscription.
    import quadratus.runtime as runtime
    for name in ("Fleet", "new_session"):
        monkeypatch.setattr(runtime, name, lambda *a, _name=name, **kw: pytest.fail(
            f"the runner reached {_name} with no Task limit"))
    (tmp_path / "app.py").write_text("x = 1\n")
    outputs = list(gui.run_project_ui("goal", str(tmp_path), False, "", "adversarial", None, Settings()))
    assert outputs[-1][0].startswith("Run failed:") and outputs[-1][1:] == ("", [])
    assert not (tmp_path / ".quadratus").exists()


def test_the_run_button_refuses_a_cleared_task_limit_by_name(tmp_path, monkeypatch):
    gr = pytest.importorskip("gradio")
    reached = []
    monkeypatch.setattr("quadratus.project_run.run_project", lambda *a, **kw: reached.append(kw))
    demo = gui.build_interface(Settings())
    run_selected = next(block.fn for block in demo.fns.values()
                        if getattr(block.fn, "__name__", None) == "run_selected")
    # goal, folder, writes, check, mode, tasks, paths, forbid, repairs, neutral, profile, extra, probes, limits
    for cleared in (None, "", 0, 2.5):
        with pytest.raises(gr.Error, match="Run limits are not valid: Task limit"):
            list(run_selected("goal", str(tmp_path), False, "", "adversarial", cleared, "", "", "", False,
                              "", "", "", False, 24, 500_000, 900, 2, 0, 0, 0))
    assert reached == [], "a refused form never reaches the runner"


# -- item 25: the launch agent's log ------------------------------------------

def test_the_launch_agent_logs_in_the_user_s_library_and_admits_the_tunnel_name():
    folder = REPO / "tools" / "monitor"
    agent = plistlib.loads((folder / "com.quadratus.monitor.plist").read_bytes())
    out, err = agent["StandardOutPath"], agent["StandardErrorPath"]
    assert out == err and re.fullmatch(r"/Users/[^/]+/Library/Logs/quadratus-monitor\.log", out)
    assert not out.startswith(("/tmp", "/private/tmp", "/var/tmp"))
    readme = (folder / "README.md").read_text()
    assert "/tmp/quadratus-monitor.log" not in readme and "~/Library/Logs/quadratus-monitor.log" in readme
    assert "QUADRATUS_MONITOR_ALLOW_HOST" in readme and "QUADRATUS_MONITOR_ALLOW_PROJECT" in readme
    # The committed pair still works together: the agent admits the name the tunnel forwards.
    tunnel = re.search(r"hostname:\s*(\S+)", (folder / "cloudflared-config.yml").read_text())[1]
    admitted = parse_allowed_hosts(agent["EnvironmentVariables"]["QUADRATUS_MONITOR_ALLOW_HOST"].split(","),
                                   "QUADRATUS_MONITOR_ALLOW_HOST")
    assert host_allowed([tunnel], 7861, admitted)
    assert agent["ProgramArguments"][1:4] == ["--monitor", "--serve", "7861"]

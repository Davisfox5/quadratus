"""The operator's capture profile and the harness-owned preview (Codex, Run 18).

Real subprocesses throughout: the preview is a real server, its process
group is really signalled, and a real browser captures the page where one is
available. No model is involved.
"""

import json
import os
import socket
import subprocess
import sys
import time
from pathlib import Path

import pytest

from quadratus import preview
from quadratus.preview import (
    PreviewFailed,
    capture_task,
    profile_from_dict,
    running,
    validate_capture,
)


def _free_port():
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


def _alive(pid):
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    try:
        # A zombie still answers kill(0); read its state where we can.
        state = Path(f"/proc/{pid}/stat").read_text().split(")")[-1].split()[0]
        return state != "Z"
    except OSError:
        return True


def _profile(root, argv, port, **kw):
    return profile_from_dict(dict(preview=argv, origin=f"http://127.0.0.1:{port}", **kw), root)


def _server(port, directory="."):
    return [sys.executable, "-m", "http.server", str(port), "--bind", "127.0.0.1", "--directory", directory]


# -- the profile is validated before any model call -------------------------------------

@pytest.mark.parametrize("data, message", [
    (dict(preview=["python", "app.py; rm -rf x"]), "no shell syntax"),
    (dict(preview=["python", "$(whoami)"]), "no shell syntax"),
    (dict(preview=["/usr/bin/env", "python"]), "known runner or a project file"),
    (dict(preview=["/tmp/tool/app"]), "known runner or a project file"),
    (dict(preview=["python", "../outside/app.py"]), "outside the project"),
    (dict(preview=["python", "/etc/app.py"]), "outside the project"),
    (dict(preview=["python", "linked/app.py"]), "symlink"),
    (dict(origin="http://10.0.0.5:5000"), "origin must be"),
    (dict(origin="http://127.0.0.1:80"), "origin must be"),
    (dict(origin="https://localhost:5000"), "origin must be"),
    (dict(ready_path="http://evil/"), "ready_path"),
    (dict(ready_path="/../x"), "ready_path"),
    (dict(ready_timeout=0), "ready_timeout"),
    (dict(capture_timeout=float("inf")), "capture_timeout"),
    (dict(extra=1), "unknown fields"),
    (dict(env={"PATH": "x"}), "may not be set"),
    (dict(env={"LD_PRELOAD": "x"}), "may not be set"),
    (dict(env={"OPENAI_API_KEY": "x"}), "may not be set"),
    (dict(env={"PYTHONPATH": "x"}), "may not be set"),
    (dict(env={"lower": "x"}), "may not be set"),
    (dict(env={"APP_DIR": "/etc"}), "write a path as"),
    (dict(env={"APP_DIR": "~/x"}), "write a path as"),
    (dict(env={"APP_DIR": "{project}/../x"}), "outside the project"),
    (dict(env={"APP_DIR": "{project}/linked"}), "symlink"),
    (dict(env={"APP_X": "$(id)"}), "plain string"),
    (dict(ready_status=404), "2xx"),
    (dict(total_timeout=0), "total_timeout"),
])
def test_an_unusable_profile_is_refused(tmp_path, data, message):
    (tmp_path / "app.py").write_text("")
    (tmp_path / "real").mkdir()
    (tmp_path / "real" / "app.py").write_text("")
    (tmp_path / "linked").symlink_to(tmp_path / "real")
    full = dict(dict(preview=["python", "app.py"], origin="http://127.0.0.1:5000"), **data)
    with pytest.raises(ValueError, match=message):
        profile_from_dict(full, tmp_path)


def test_operator_env_is_resolved_and_reaches_only_the_preview(tmp_path):
    port = _free_port()
    (tmp_path / "app.py").write_text(
        "import os, http.server, socketserver\n"
        "open('seen.txt', 'w').write(os.environ['APP_PORT'] + ' ' + os.environ['APP_ROOT'])\n"
        "socketserver.TCPServer(('127.0.0.1', int(os.environ['APP_PORT'])),"
        " http.server.SimpleHTTPRequestHandler).serve_forever()\n")
    profile = _profile(tmp_path, [sys.executable, "app.py"], port,
                       env={"APP_PORT": str(port), "APP_ROOT": "{project}"})
    assert dict(profile.env) == {"APP_PORT": str(port), "APP_ROOT": str(tmp_path.resolve())}
    with running(profile, tmp_path):
        pass
    assert (tmp_path / "seen.txt").read_text() == f"{port} {tmp_path.resolve()}"
    assert "APP_PORT" not in os.environ


def test_a_usable_profile_is_accepted(tmp_path):
    (tmp_path / "serve.py").write_text("")
    profile = profile_from_dict(dict(preview=["./serve.py"], origin="http://localhost:5173",
                                     ready_path="/health", ready_timeout=5), tmp_path)
    assert profile.port == 5173 and profile.host == "localhost" and profile.preview == ("./serve.py",)


@pytest.mark.parametrize("capture", [
    {"path": "http://x/"}, {"path": "/a", "steps": [{"action": "type", "selector": "#a"}]},
    {"path": "/a", "steps": [{"action": "file", "selector": "#a"}]}, {"path": "/a", "extra": 1},
    {"path": "/a", "steps": [{"action": "click", "selector": "#a", "path": "x"}]},
])
def test_an_unusable_scope_capture_is_refused(capture):
    with pytest.raises(ValueError):
        validate_capture(capture)


# -- the preview's lifecycle ------------------------------------------------------------

def test_the_preview_starts_answers_and_is_stopped(tmp_path):
    port = _free_port()
    (tmp_path / "index.html").write_text("<p>hi</p>")
    with running(_profile(tmp_path, _server(port), port), tmp_path) as proc:
        assert b"hi" in __import__("urllib.request").request.urlopen(f"http://127.0.0.1:{port}/index.html").read()
        pid = proc.pid
    assert not _alive(pid)
    assert not preview._listening("127.0.0.1", port)


def test_a_404_on_the_ready_path_is_not_ready(tmp_path):
    port = _free_port()
    profile = _profile(tmp_path, _server(port), port, ready_path="/missing.html", ready_timeout=2)
    with pytest.raises(PreviewFailed, match="not ready"):
        with running(profile, tmp_path):
            pass
    assert not preview._listening("127.0.0.1", port)


def test_an_interrupt_lets_the_app_run_its_own_cleanup(tmp_path):
    port = _free_port()
    (tmp_path / "app.py").write_text(
        "import http.server, socketserver\n"
        f"server = socketserver.TCPServer(('127.0.0.1', {port}), http.server.SimpleHTTPRequestHandler)\n"
        "try:\n    server.serve_forever()\nfinally:\n    open('cleaned.txt', 'w').write('yes')\n")
    with running(_profile(tmp_path, [sys.executable, "app.py"], port), tmp_path):
        pass
    assert (tmp_path / "cleaned.txt").read_text() == "yes"


def test_the_capture_gets_only_what_is_left_of_one_budget(tmp_path, monkeypatch):
    import re
    port = _free_port()
    (tmp_path / "slow.py").write_text(
        "import time, http.server, socketserver\ntime.sleep(2)\n"
        f"socketserver.TCPServer(('127.0.0.1', {port}), http.server.SimpleHTTPRequestHandler).serve_forever()\n")
    monkeypatch.setattr(preview, "capture_argv",
                        lambda *a, **k: [sys.executable, "-c", "import time; time.sleep(30)"])
    profile = _profile(tmp_path, [sys.executable, "slow.py"], port, ready_timeout=10, capture_timeout=100,
                       total_timeout=4)
    started = time.monotonic()
    message = capture_task(profile, tmp_path, "t1", {"path": "/", "steps": []})
    left = int(re.search(r"within (\d+)s of the 4s budget", message).group(1))
    assert left <= 2, f"the capture was given {left}s of a 4s budget after a 2s start"
    assert time.monotonic() - started < 20

def test_a_preview_that_exits_early_is_named(tmp_path):
    port = _free_port()
    (tmp_path / "fail.py").write_text("raise SystemExit(3)\n")
    profile = _profile(tmp_path, [sys.executable, "fail.py"], port)
    with pytest.raises(PreviewFailed, match="exited with 3 before it was ready"):
        with running(profile, tmp_path):
            pass


def test_a_preview_that_never_answers_times_out_and_is_reaped(tmp_path):
    port = _free_port()
    marker = tmp_path / "pid"
    (tmp_path / "hang.py").write_text(f"import os, time\nopen({str(marker)!r}, 'w').write(str(os.getpid()))\n"
                                      "time.sleep(60)\n")
    profile = _profile(tmp_path, [sys.executable, "hang.py"], port, ready_timeout=1)
    started = time.monotonic()
    with pytest.raises(PreviewFailed, match="not ready"):
        with running(profile, tmp_path):
            pass
    assert time.monotonic() - started < 15
    assert not _alive(int(marker.read_text()))


def test_something_already_listening_is_never_taken_for_the_preview(tmp_path):
    port = _free_port()
    with socket.socket() as other:
        other.bind(("127.0.0.1", port))
        other.listen()
        with pytest.raises(PreviewFailed, match="already listening"):
            with running(_profile(tmp_path, _server(port), port), tmp_path):
                pass
        other.settimeout(0.5)
        with socket.create_connection(("127.0.0.1", port), timeout=1):
            pass   # the unrelated listener is untouched


def test_children_and_grandchildren_are_reaped_and_nothing_else_is(tmp_path):
    port = _free_port()
    pids = tmp_path / "pids.json"
    script = tmp_path / "serve.py"
    script.write_text(
        "import json, subprocess, sys, http.server, socketserver\n"
        "child = subprocess.Popen([sys.executable, '-c', 'import subprocess,sys,time;"
        "g=subprocess.Popen([sys.executable,\"-c\",\"import time; time.sleep(60)\"]);"
        "print(g.pid, flush=True); time.sleep(60)'], stdout=subprocess.PIPE, text=True)\n"
        "grand = int(child.stdout.readline())\n"
        f"json.dump([child.pid, grand], open({str(pids)!r}, 'w'))\n"
        f"socketserver.TCPServer(('127.0.0.1', {port}), http.server.SimpleHTTPRequestHandler).serve_forever()\n")
    unrelated = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)"])
    try:
        with running(_profile(tmp_path, [sys.executable, "serve.py"], port), tmp_path) as proc:
            leader = proc.pid
            child, grand = json.loads(pids.read_text())
            assert _alive(child) and _alive(grand)
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline and (_alive(child) or _alive(grand)):
            time.sleep(0.1)
        assert not _alive(leader) and not _alive(child) and not _alive(grand)
        assert unrelated.poll() is None, "a process outside the preview's group is never signalled"
    finally:
        unrelated.kill()
        unrelated.wait()


def test_a_capture_that_overruns_is_stopped_and_the_preview_reaped(tmp_path, monkeypatch):
    port = _free_port()
    monkeypatch.setattr(preview, "capture_argv",
                        lambda *a, **k: [sys.executable, "-c", "import time; time.sleep(30)"])
    profile = _profile(tmp_path, _server(port), port, capture_timeout=1)
    started = time.monotonic()
    assert "did not finish within 1s" in capture_task(profile, tmp_path, "t1", {"path": "/", "steps": []})
    assert time.monotonic() - started < 20
    assert not preview._listening("127.0.0.1", port)


@pytest.mark.skipif(not os.environ.get("QUADRATUS_CHROMIUM"), reason="needs a browser")
def test_a_zero_step_harness_capture_is_pinned_to_the_origin(tmp_path):
    from quadratus.design_evidence import check, source_fingerprint
    port, other = _free_port(), _free_port()
    (tmp_path / "index.html").write_text(
        f"<html><head><link rel=icon href='data:,'></head><body><p>x</p><script>"
        f"setTimeout(() => location.href = 'http://127.0.0.1:{other}/elsewhere', 50)</script></body></html>")
    profile = _profile(tmp_path, _server(port), port)
    assert capture_task(profile, tmp_path, "t1", {"path": "/index.html", "steps": []}) == ""
    ok, problem, _ = check(tmp_path, "t1", 0, expected_source=source_fingerprint(tmp_path))
    assert not ok and "navigation outside the preview was blocked" in problem


@pytest.mark.skipif(not os.environ.get("QUADRATUS_CHROMIUM"), reason="needs a browser")
def test_the_harness_captures_the_declared_page_with_a_real_browser(tmp_path):
    from quadratus.design_evidence import check, source_fingerprint
    port = _free_port()
    (tmp_path / "index.html").write_text("<html><body><button id=go>Go</button><p id=done hidden>ok</p>"
                                         "<script>go.onclick=()=>done.hidden=false</script></body></html>")
    profile = _profile(tmp_path, _server(port), port)
    capture = {"path": "/index.html", "steps": [{"action": "click", "selector": "#go"},
                                                {"action": "wait", "selector": "#done"}]}
    assert capture_task(profile, tmp_path, "t1", capture) == ""
    ok, problem, shots = check(tmp_path, "t1", 0, expected_source=source_fingerprint(tmp_path))
    assert ok, problem
    assert f"target: http://127.0.0.1:{port}/index.html" in shots
    assert not preview._listening("127.0.0.1", port)


# -- Codex review of 3a55d82 ------------------------------------------------------------

def _redirecting(root, port, to):
    (root / "redirect.py").write_text(
        "import http.server, socketserver\n"
        "class H(http.server.SimpleHTTPRequestHandler):\n"
        "    def do_GET(self):\n"
        "        if self.path.startswith('/go'):\n"
        f"            self.send_response(302); self.send_header('Location', {to!r}); self.end_headers(); return\n"
        "        super().do_GET()\n"
        f"socketserver.ThreadingTCPServer(('127.0.0.1', {port}), H).serve_forever()\n")
    return [sys.executable, "redirect.py"]


def test_a_redirecting_ready_path_is_not_ready(tmp_path):
    port = _free_port()
    (tmp_path / "index.html").write_text("ok")
    profile = _profile(tmp_path, _redirecting(tmp_path, port, "/index.html"), port, ready_path="/go",
                       ready_timeout=2)
    with pytest.raises(PreviewFailed, match="not ready"):
        with running(profile, tmp_path):
            pass


def _other_service(tmp_path, port):
    """A live, unrelated loopback service that logs every request it gets."""
    # Logged outside the project, so the log is never a source change.
    log = open(tmp_path.parent / f"other-{port}.log", "w")
    proc = subprocess.Popen(_server(port), cwd=tmp_path, stdout=log, stderr=subprocess.STDOUT)
    deadline = time.monotonic() + 10
    while not preview._listening("127.0.0.1", port) and time.monotonic() < deadline:
        time.sleep(0.1)
    return proc, tmp_path.parent / f"other-{port}.log"


@pytest.mark.skipif(not os.environ.get("QUADRATUS_CHROMIUM"), reason="needs a browser")
@pytest.mark.parametrize("capture_path, page", [
    ("/go", "<html><head><link rel=icon href='data:,'></head><body>ok</body></html>"),
    ("/index.html", "<html><head><link rel=icon href='data:,'></head><body>ok"
                    "<img src='http://127.0.0.1:{other}/index.html'></body></html>"),
    ("/index.html", "<html><head><link rel=icon href='data:,'></head><body>ok<script>"
                    "setTimeout(() => location.href = 'http://127.0.0.1:{other}/index.html', 50)</script></body></html>"),
], ids=["server-redirect", "subresource", "script-navigation"])
def test_nothing_reaches_another_live_loopback_service(tmp_path, capture_path, page):
    """Codex review of 7a8c432: a 302 to a live second listener reached it and
    the capture passed. Now no request leaves the origin, and the render fails."""
    from quadratus.design_evidence import check, source_fingerprint
    port, other = _free_port(), _free_port()
    (tmp_path / "index.html").write_text(page.format(other=other))
    unrelated, log = _other_service(tmp_path, other)
    try:
        profile = _profile(tmp_path, _redirecting(tmp_path, port, f"http://127.0.0.1:{other}/index.html"), port,
                           ready_path="/index.html")
        failure = capture_task(profile, tmp_path, "t1", {"path": capture_path, "steps": []})
        ok, problem, _ = check(tmp_path, "t1", 0, expected_source=source_fingerprint(tmp_path))
        # Either the capture itself stops on the blocked navigation, or the
        # render it leaves is unclean; never a passing check.
        assert not ok and "outside the preview" in (failure + " " + problem), (failure, problem)
        assert "source changed" not in problem
    finally:
        unrelated.kill()
        unrelated.wait()
    assert "GET" not in log.read_text(), "the other service was never contacted"


@pytest.mark.skipif(not os.environ.get("QUADRATUS_CHROMIUM"), reason="needs a browser")
def test_a_same_origin_redirect_is_followed_and_passes(tmp_path):
    from quadratus.design_evidence import check, source_fingerprint
    port = _free_port()
    (tmp_path / "index.html").write_text("<html><head><link rel=icon href='data:,'></head><body>ok</body></html>")
    profile = _profile(tmp_path, _redirecting(tmp_path, port, "/index.html"), port, ready_path="/index.html")
    assert capture_task(profile, tmp_path, "t1", {"path": "/go", "steps": []}) == ""
    ok, problem, _ = check(tmp_path, "t1", 0, expected_source=source_fingerprint(tmp_path))
    assert ok, problem


@pytest.mark.parametrize("path", ["/etc/DUMMY", "../x/y/z", ".quadratus/capture-fixtures/t1/a/b", "fixtures/a.csv"])
def test_a_fixture_path_is_refused_at_parse(path):
    with pytest.raises(ValueError, match="capture-fixtures"):
        validate_capture({"path": "/", "steps": [{"action": "file", "selector": "#f", "path": path}]})


@pytest.mark.parametrize("argument", ["--config=outside-config.txt", "outside-config.txt", "--config=/etc/x"])
def test_an_argument_naming_a_link_or_an_outside_path_is_refused(tmp_path, argument):
    outside = tmp_path.parent / "outside.txt"
    outside.write_text("DUMMY")
    root = tmp_path / "project"
    root.mkdir()
    (root / "outside-config.txt").symlink_to(outside)
    with pytest.raises(ValueError, match="symlink|outside the project"):
        profile_from_dict(dict(preview=["python", "-m", "app", argument], origin="http://127.0.0.1:5000"), root)


def test_the_preview_log_is_a_bounded_tail(tmp_path):
    port = _free_port()
    (tmp_path / "chatty.py").write_text(
        "import sys, threading, http.server, socketserver\n"
        "def talk():\n"
        "    while True: sys.stdout.write('x' * 65536); sys.stdout.flush()\n"
        "threading.Thread(target=talk, daemon=True).start()\n"
        f"socketserver.TCPServer(('127.0.0.1', {port}), http.server.SimpleHTTPRequestHandler).serve_forever()\n")
    captured = []
    real = preview._BoundedLog

    class Spy(real):
        def __init__(self, stream):
            super().__init__(stream)
            captured.append(self)
    import unittest.mock
    with unittest.mock.patch.object(preview, "_BoundedLog", Spy):
        with running(_profile(tmp_path, [sys.executable, "chatty.py"], port), tmp_path):
            time.sleep(1)
    assert captured and len(captured[0]._buffer) <= real.KEEP
    assert len(captured[0].tail()) <= 2_000


def test_a_capture_timeout_stops_what_the_capture_started(tmp_path, monkeypatch):
    port = _free_port()
    pid_file = tmp_path / "child.pid"
    (tmp_path / "spawn.py").write_text(
        "import subprocess, sys, time\n"
        "child = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(60)'])\n"
        f"open({str(pid_file)!r}, 'w').write(str(child.pid))\n"
        "time.sleep(60)\n")
    monkeypatch.setattr(preview, "capture_argv", lambda *a, **k: [sys.executable, str(tmp_path / "spawn.py")])
    message = capture_task(_profile(tmp_path, _server(port), port, capture_timeout=2), tmp_path, "t1",
                           {"path": "/", "steps": []})
    assert "did not finish" in message
    deadline = time.monotonic() + 5
    child = int(pid_file.read_text())
    while _alive(child) and time.monotonic() < deadline:
        time.sleep(0.1)
    assert not _alive(child), "the capture's own descendants are stopped too"


@pytest.mark.skipif(not os.environ.get("QUADRATUS_CHROMIUM"), reason="needs a browser")
def test_a_project_module_named_quadratus_never_runs_as_the_capture(tmp_path):
    port = _free_port()
    marker = tmp_path.parent / "shadow-ran"
    (tmp_path / "quadratus").mkdir()
    (tmp_path / "quadratus" / "__init__.py").write_text("")
    (tmp_path / "quadratus" / "design_evidence.py").write_text(f"open({str(marker)!r}, 'w').write('ran')\n")
    (tmp_path / "index.html").write_text("<html><head><link rel=icon href='data:,'></head><body>ok</body></html>")
    assert capture_task(_profile(tmp_path, _server(port), port), tmp_path, "t1",
                        {"path": "/index.html", "steps": []}) == ""
    assert not marker.exists()
    assert (tmp_path / ".quadratus" / "design-evidence" / "t1" / "summary.json").is_file()


def test_only_the_capture_invocation_itself_is_a_relevant_denial():
    from quadratus.runtime import _is_capture_invocation, _relevant_denials
    assert _is_capture_invocation("PYTHONPATH=/p python3 -m quadratus.design_evidence http://x t1 .")
    assert not _is_capture_invocation("printf 'quadratus.design_evidence'")
    assert not _is_capture_invocation("cat quadratus/design_evidence.py")
    denied = [dict(kind="permission_denied", command="printf 'quadratus.design_evidence'"),
              dict(kind="permission_denied", command="pytest -q")]
    assert _relevant_denials(denied, ("pytest -q",)) == ["pytest -q"]
    assert _relevant_denials(42, ()) == []


def test_a_malformed_denial_list_is_ignored_without_losing_accounting():
    from quadratus.cli_providers import _extract_claude_denials
    assert _extract_claude_denials(json.dumps({"permission_denials": 42})) == []
    assert _extract_claude_denials(json.dumps({"permission_denials": [42, {"tool_name": "Bash"}]})) == []

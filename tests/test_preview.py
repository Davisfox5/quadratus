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
    (dict(env={"A": "1"}), "unknown fields"),
])
def test_an_unusable_profile_is_refused(tmp_path, data, message):
    (tmp_path / "app.py").write_text("")
    (tmp_path / "real").mkdir()
    (tmp_path / "real" / "app.py").write_text("")
    (tmp_path / "linked").symlink_to(tmp_path / "real")
    full = dict(dict(preview=["python", "app.py"], origin="http://127.0.0.1:5000"), **data)
    with pytest.raises(ValueError, match=message):
        profile_from_dict(full, tmp_path)


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

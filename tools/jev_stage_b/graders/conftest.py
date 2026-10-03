"""Frozen Stage B graders: shared fixtures.

Every grader runs from this directory (never from the project, which the
builders edit) with ``STAGE_B_PROJECT`` naming the finished cell worktree.
Two ways to judge it, both independent of anything the builders wrote:

- ``application`` / ``client``: the cell's ``app.py`` imported fresh with its
  data directories pointed at a temp dir, driven through Flask's test client.
- ``live`` / ``page``: the same app served by ``serve.py`` (grader-owned) on a
  loopback port, driven by real Chromium through Playwright; ``page`` also
  records browser console errors and page errors.

One requirement is one test; the runner calls ``pytest -k R<n>`` per
requirement, so a requirement's grade is that test's exit status.
"""
from __future__ import annotations

import json
import os
import re
import subprocess
import sys
import time
from pathlib import Path

import pytest

HERE = Path(__file__).resolve().parent


def cell_root() -> Path:
    root = os.environ.get("STAGE_B_PROJECT")
    if not root:
        raise RuntimeError("STAGE_B_PROJECT is not set: graders run against one cell's finished worktree")
    path = Path(root).resolve()
    if not (path / "app.py").is_file():
        raise RuntimeError(f"{path} holds no app.py")
    return path


@pytest.fixture(scope="session")
def project_root() -> Path:
    return cell_root()


@pytest.fixture
def application(project_root, tmp_path, monkeypatch):
    """The cell's app module, data pointed at a fresh temp dir, imported once
    per process (a grader is one process per requirement)."""
    monkeypatch.chdir(project_root)
    if str(project_root) not in sys.path:
        sys.path.insert(0, str(project_root))
    import app as module  # noqa: WPS433 -- the cell's own module, on purpose
    data = tmp_path / "data"
    (data / "videos").mkdir(parents=True)
    (data / "recordings").mkdir(parents=True)
    monkeypatch.setattr(module, "DATA_DIR", str(data), raising=False)
    monkeypatch.setattr(module, "VIDEOS_DIR", str(data / "videos"), raising=False)
    monkeypatch.setattr(module, "RECORDINGS_DIR", str(data / "recordings"), raising=False)
    monkeypatch.setattr(module, "PROJECTS_FILE", str(data / "projects.json"), raising=False)
    module.app.config["TESTING"] = True
    module.__grader_data__ = data
    return module


@pytest.fixture
def client(application):
    with application.app.test_client() as c:
        yield c


def seed_project(client, name="Project", players=(), clips=(), tag_types=None):
    """One project through the public API, returned as the server knows it."""
    rv = client.post("/api/projects", json={"name": name})
    assert rv.status_code == 201, rv.data
    project = rv.get_json()
    pid = project["id"]
    if tag_types is not None:
        rv = client.put(f"/api/projects/{pid}/tag_types", json={"tag_types": tag_types})
        assert rv.status_code == 200, rv.data
    for player in players:
        rv = client.post(f"/api/projects/{pid}/players", json=player)
        assert rv.status_code == 201, rv.data
    for clip in clips:
        rv = client.post(f"/api/projects/{pid}/clips", json=clip)
        assert rv.status_code == 201, rv.data
    return client.get(f"/api/projects/{pid}").get_json()


class Live:
    """A grader-owned server process for the cell, plus its seed."""

    def __init__(self, project_root: Path, seed: list, tmp: Path):
        seed_path = tmp / "seed.json"
        seed_path.write_text(json.dumps(seed))
        self.proc = subprocess.Popen(
            [sys.executable, str(HERE / "serve.py"), str(project_root), str(seed_path)],
            cwd=HERE, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
        deadline = time.time() + 30
        first = ""
        while time.time() < deadline:
            first = self.proc.stdout.readline()
            if first.startswith("{"):
                break
            if self.proc.poll() is not None:
                break
        if not first.startswith("{"):
            err = self.proc.stderr.read()[-2000:] if self.proc.stderr else ""
            raise RuntimeError(f"serve.py did not start: {first!r} {err}")
        info = json.loads(first)
        self.url = f"http://127.0.0.1:{info['port']}"
        self.projects = info["projects"]
        self.data = Path(info["data"])

    def api(self, path: str):
        import urllib.request
        with urllib.request.urlopen(self.url + path, timeout=10) as resp:
            return json.loads(resp.read().decode("utf-8"))

    def stop(self):
        if self.proc.poll() is None:
            self.proc.terminate()
            try:
                self.proc.wait(timeout=10)
            except subprocess.TimeoutExpired:
                self.proc.kill()


@pytest.fixture
def live_factory(project_root, tmp_path):
    servers = []

    def start(seed=()):
        server = Live(project_root, list(seed), tmp_path)
        servers.append(server)
        return server

    yield start
    for server in servers:
        server.stop()


@pytest.fixture
def browser():
    from playwright.sync_api import sync_playwright
    with sync_playwright() as p:
        b = p.chromium.launch()
        yield b
        b.close()


IGNORED_CONSOLE = ("favicon.ico",)


@pytest.fixture
def page_factory(browser):
    """Pages that record console errors and uncaught page errors."""
    pages = []

    def make():
        context = browser.new_context(viewport={"width": 1280, "height": 800})
        page = context.new_page()
        page.set_default_timeout(5000)  # a missing element fails fast; nothing here legitimately takes longer
        errors, http_errors, responses = [], [], []

        def on_console(message):
            if message.type != "error" or any(s in message.text for s in IGNORED_CONSOLE):
                return
            location = getattr(message, "location", None) or {}
            url = location.get("url") if isinstance(location, dict) else None
            sort_console_error(message.text, url, errors, http_errors)

        page.on("console", on_console)
        page.on("pageerror", lambda exc: errors.append(f"pageerror: {exc}"))
        page.on("response", lambda r: responses.append(dict(method=r.request.method, url=r.url, status=r.status))
                if r.status >= 400 else None)
        page.grader_errors = errors
        page.grader_http_errors = http_errors
        page.grader_responses = responses
        pages.append((context, page))
        return page

    yield make
    for context, _ in pages:
        context.close()


_RESOURCE_STATUS = re.compile(r"Failed to load resource: the server responded with a status of (\d{3})")


def sort_console_error(text, url, errors, http_errors):
    """Chromium reports every HTTP error response as a console error
    ("Failed to load resource: the server responded with a status of 400").
    Those are kept apart, with the resource URL and status, so a grader that
    deliberately provokes one can excuse exactly that response and nothing
    else; every other console error stays an error."""
    match = _RESOURCE_STATUS.search(text)
    if match:
        http_errors.append(dict(text=f"console: {text}", url=url, status=int(match.group(1))))
    else:
        errors.append(f"console: {text}")


def _path(url):
    from urllib.parse import urlsplit
    return urlsplit(url or "").path


def unexpected_console_errors(errors, http_errors, responses, expected_http=()):
    """Everything that fails the page, given ``expected_http``: (method,
    exact URL path, status) triples the grader provoked on purpose, one per
    expected occurrence. Rules (Codex reviews of e3deaed P1 #3 and 3a1eef9
    P2): each expectation must have been observed as a real response, and
    is consumed by exactly one; every other error response (any method, any
    path, any status, including a different method at the same path) fails;
    Chromium's resource diagnostic is excused once per consumed response at
    that exact path and status, and any further diagnostic fails. Returns
    (missing_expected, failures)."""
    pool = list(responses)
    consumed, missing = [], []
    for method, path, status in expected_http:
        hit = next((r for r in pool if r["method"] == method and _path(r["url"]) == path and r["status"] == status), None)
        if hit is None:
            missing.append(f"{method} {path} -> {status}")
        else:
            pool.remove(hit)
            consumed.append(hit)
    failures = list(errors)
    failures += [f"unexpected HTTP error response: {r['method']} {r['url']} -> {r['status']}" for r in pool]
    allowance = [(_path(r["url"]), r["status"]) for r in consumed]
    for entry in http_errors:
        key = (_path(entry.get("url")), entry["status"])
        if key in allowance:
            allowance.remove(key)  # one diagnostic per consumed response
        else:
            failures.append(entry["text"])
    return missing, failures


def no_console_errors(page, expected_http=()):
    missing, failures = unexpected_console_errors(page.grader_errors, page.grader_http_errors,
                                                  page.grader_responses, expected_http)
    assert not missing, f"expected responses never observed: {missing}; seen {page.grader_responses}"
    assert failures == [], failures

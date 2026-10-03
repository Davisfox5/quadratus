"""The graders' console collector in real Chromium against a local HTTP
server: the positive case and Codex's two negative cases from the review of
3a1eef9, each as the browser actually produces them. Skipped where
Playwright or a Chromium build is missing."""
import importlib.util
import json
import sys
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path

import pytest

GRADERS = Path(__file__).parent / "graders"

PAGE = """<!doctype html><html><body><script>
async function go(scenario) {
  const patch = await fetch('/api/projects/one', {method: 'PATCH', headers: {'Content-Type': 'application/json'},
                                                   body: JSON.stringify({name: 'two'})});
  await patch.json();
  if (scenario === 'clips') { await (await fetch('/api/projects/one/clips')).text(); }
  if (scenario === 'get-same') { await (await fetch('/api/projects/one')).text(); }
  if (scenario === 'boom') { await (await fetch('/api/projects/boom', {method: 'PATCH'})).text(); }
  if (scenario === 'js') { undefinedFunctionCall(); }
  document.title = 'done';
}
go(new URLSearchParams(location.search).get('s') || 'clean');
</script></body></html>"""


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *args):  # quiet
        pass

    def _send(self, status, body, ctype="application/json"):
        payload = body.encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)

    def do_GET(self):
        if self.path.startswith("/page"):
            return self._send(200, PAGE, "text/html")
        if self.path in ("/api/projects/one/clips", "/api/projects/one"):
            return self._send(400, json.dumps({"error": "bad"}))
        return self._send(404, json.dumps({"error": "nope"}))

    def do_PATCH(self):
        length = int(self.headers.get("Content-Length") or 0)
        self.rfile.read(length)
        if self.path == "/api/projects/one":
            return self._send(400, json.dumps({"error": "A project named 'two' already exists"}))
        return self._send(500, json.dumps({"error": "boom"}))


@pytest.fixture(scope="module")
def server():
    httpd = HTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()
    yield f"http://127.0.0.1:{httpd.server_port}"
    httpd.shutdown()


@pytest.fixture(scope="module")
def conftest_module():
    sys.path.insert(0, str(GRADERS))
    spec = importlib.util.spec_from_file_location("stage_b_grader_conftest_browser", GRADERS / "conftest.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.fixture(scope="module")
def browser():
    playwright = pytest.importorskip("playwright.sync_api")
    with playwright.sync_playwright() as p:
        try:
            b = p.chromium.launch()
        except Exception as exc:  # noqa: BLE001 -- no Chromium build here: skip, never pass
            pytest.skip(f"no Chromium: {exc}")
        yield b
        b.close()


def _run(conftest_module, browser, server, scenario):
    """One page through the real page_factory wiring, returning (missing, failures)."""
    gen = conftest_module.page_factory.__wrapped__(browser) if hasattr(conftest_module.page_factory, "__wrapped__") \
        else conftest_module.page_factory(browser)
    make = next(gen)
    page = make()
    page.goto(f"{server}/page?s={scenario}")
    page.wait_for_function("() => document.title === 'done'", timeout=5000) if scenario != "js" else page.wait_for_timeout(500)
    page.wait_for_timeout(200)
    expected = (("PATCH", "/api/projects/one", 400),)
    result = conftest_module.unexpected_console_errors(page.grader_errors, page.grader_http_errors,
                                                       page.grader_responses, expected)
    try:
        next(gen)
    except StopIteration:
        pass
    return result


def test_positive_the_expected_duplicate_name_400_alone_passes(conftest_module, browser, server):
    assert _run(conftest_module, browser, server, "clean") == ([], [])


def test_negative_an_unrelated_400_under_the_same_project_fails(conftest_module, browser, server):
    missing, failures = _run(conftest_module, browser, server, "clips")
    assert missing == [] and any("GET " in f and "/api/projects/one/clips" in f for f in failures), failures


def test_negative_the_same_url_with_the_wrong_method_fails(conftest_module, browser, server):
    missing, failures = _run(conftest_module, browser, server, "get-same")
    assert missing == [] and any("GET " in f and "/api/projects/one" in f and "clips" not in f for f in failures), failures


def test_a_500_and_a_js_error_still_fail(conftest_module, browser, server):
    missing, failures = _run(conftest_module, browser, server, "boom")
    assert any("500" in f for f in failures), failures
    missing, failures = _run(conftest_module, browser, server, "js")
    assert any("pageerror" in f for f in failures), failures

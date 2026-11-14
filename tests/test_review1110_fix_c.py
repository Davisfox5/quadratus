"""Regression tests for the final review of the 2026-11-10 batch, gate and
browser capture (results/gate.review.json, findings 1 to 5).

Each test names the finding it pins ("gate N").
"""

from __future__ import annotations

import http.server
import json
import socket
import threading
import time

import pytest

# --------------------------------------------------------------------------
# gate 2: a pinned browser's whole network is held, not only what the page's
# own session pauses.


class _Services:
    """The preview (an HTTP server whose requests are recorded) and another
    loopback service: a raw TCP listener and a UDP socket that record
    whatever reaches them."""

    def __init__(self, pages):
        self.tcp_hits, self.udp_hits, self.preview_hits = [], [], []
        services = self

        class Handler(http.server.BaseHTTPRequestHandler):
            def do_GET(self):
                services.preview_hits.append(self.path)
                if self.headers.get("Upgrade", "").lower() == "websocket":
                    # The preview's own socket: accepted, then left idle.
                    import base64
                    import hashlib
                    key = self.headers.get("Sec-WebSocket-Key", "") + "258EAFA5-E914-47DA-95CA-C5AB0DC85B11"
                    self.send_response(101)
                    self.send_header("Upgrade", "websocket")
                    self.send_header("Connection", "Upgrade")
                    self.send_header("Sec-WebSocket-Accept", base64.b64encode(hashlib.sha1(key.encode()).digest()).decode())
                    self.end_headers()
                    return
                body, kind = services.pages.get(self.path.split("?")[0], ("", "text/plain"))
                self.send_response(200 if self.path.split("?")[0] in services.pages else 404)
                self.send_header("Content-Type", kind)
                self.end_headers()
                self.wfile.write(body.encode())

            def log_message(self, *a):
                pass
        self.preview = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.tcp = socket.socket()
        self.tcp.bind(("127.0.0.1", 0))
        self.tcp.listen(32)
        self.udp = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        self.udp.bind(("127.0.0.1", 0))
        self.url = f"http://127.0.0.1:{self.preview.server_port}"
        ports = {"{TCP}": str(self.tcp.getsockname()[1]), "{UDP}": str(self.udp.getsockname()[1])}
        self.pages = {}
        for path, (body, kind) in pages.items():
            for mark, port in ports.items():
                body = body.replace(mark, port)
            self.pages[path] = (body, kind)
        self.pages.setdefault("/favicon.ico", ("", "image/x-icon"))
        for target in (self.preview.serve_forever, self._accept, self._receive):
            threading.Thread(target=target, daemon=True).start()

    def _accept(self):
        while True:
            try:
                connection, _ = self.tcp.accept()
            except OSError:
                return
            connection.settimeout(1.0)
            try:
                first = connection.recv(200)
            except OSError:
                first = b""
            self.tcp_hits.append(first[:60])
            connection.close()

    def _receive(self):
        while True:
            try:
                data, _ = self.udp.recvfrom(2000)
            except OSError:
                return
            self.udp_hits.append(data[:20])

    def allow(self, url):
        return url == self.url or url.startswith(self.url + "/")

    def close(self):
        self.preview.shutdown()
        self.preview.server_close()
        self.tcp.close()
        self.udp.close()


_WS = "ws://127.0.0.1:{TCP}"
_RTC = ("try{const pc=new RTCPeerConnection({iceServers:[{urls:'URL',username:'u',credential:'p'}]});"
        "pc.createDataChannel('x');pc.createOffer().then(o=>pc.setLocalDescription(o))}catch(e){}")
_HTML = "text/html"
_JS = "text/javascript"
_PAGES = {
    "/plain": ("<!doctype html><title>p</title><p id=out>HELLO</p>", _HTML),
    # A dedicated worker, a worker made from a blob, a worker's own worker,
    # and a frame whose window is taken before any guard script runs in it.
    "/sockets": ("<!doctype html><title>s</title><body><script>"
                 "new Worker('/ww.js');"
                 "new Worker(URL.createObjectURL(new Blob([\"new WebSocket('" + _WS + "/blob-worker')\"],"
                 "{type:'text/javascript'})));"
                 "new Worker('/outer.js');"
                 "const f=document.createElement('iframe');document.body.appendChild(f);"
                 "try{new f.contentWindow.WebSocket('" + _WS + "/fresh-frame')}catch(e){}"
                 "</script><p>x</p>", _HTML),
    "/ww.js": ("let n=0;const go=()=>{try{new WebSocket('" + _WS + "/worker')}catch(e){};"
               "if(++n<4)setTimeout(go,150)};go()", _JS),
    "/outer.js": ("new Worker('/inner.js')", _JS),
    "/inner.js": ("try{new WebSocket('" + _WS + "/nested-worker')}catch(e){}", _JS),
    "/rtc-tcp": ("<!doctype html><title>r</title><script>"
                 + _RTC.replace("URL", "turn:127.0.0.1:{TCP}?transport=tcp") + "</script><p>x</p>", _HTML),
    "/datagrams": ("<!doctype html><title>u</title><script>"
                   + _RTC.replace("URL", "stun:127.0.0.1:{UDP}")
                   + ";" + _RTC.replace("URL", "turn:127.0.0.1:{UDP}?transport=udp")
                   + ";try{new WebTransport('https://127.0.0.1:{UDP}/page')}catch(e){};"
                   "new Worker('/wt.js')</script><p>x</p>", _HTML),
    "/wt.js": ("try{new WebTransport('https://127.0.0.1:{UDP}/worker')}catch(e){}", _JS),
    "/own": ("<!doctype html><title>o</title><script>new Worker('/own.js')</script><p>x</p>", _HTML),
    "/own.js": ("try{new WebSocket('ws://'+location.host+'/own-socket')}catch(e){}", _JS),
    # WebRTC and nothing else: no console error would make this one unclean.
    "/rtc-udp": ("<!doctype html><title>d</title><script>" + _RTC.replace("URL", "stun:127.0.0.1:{UDP}")
                 + ";" + _RTC.replace("URL", "turn:127.0.0.1:{UDP}?transport=udp") + "</script><p>x</p>", _HTML),
    # An ordinary page: everything it loads is its own, on other paths.
    "/app": ("<!doctype html><title>a</title><link rel=stylesheet href=/app.css><img src=/pixel.png>"
             "<p id=out>x</p><iframe src=/plain></iframe><script src=/app.js></script>", _HTML),
    "/app.css": ("@font-face{font-family:F;src:url(/font.woff2)}p{font-family:F,serif;"
                 "background:url(/pixel.png?bg)}", "text/css"),
    "/app.js": ("fetch('/data.json');new WebSocket('ws://'+location.host+'/page-socket');"
                "new Worker('/app-worker.js');import('/module.js')", _JS),
    "/app-worker.js": ("fetch('/data.json?worker')", _JS),
    "/module.js": ("export default 1", _JS),
    "/data.json": ("{}", "application/json"),
    "/pixel.png": ("", "image/png"),
    "/font.woff2": ("", "font/woff2"),
}


def _full_chromium():
    """A full Chromium build, when this machine has one: what an operator
    names in QUADRATUS_CHROMIUM. Playwright's own default is the headless
    shell, which is a different build of the same browser."""
    import os
    for path in (os.environ.get("QUADRATUS_CHROMIUM"), "/opt/pw-browsers/chromium"):
        if path and os.path.exists(path):
            return path
    return None


@pytest.fixture(params=["playwright's own", "a full build"])
def services(request, monkeypatch):
    """Every browser test runs in both builds. They differ where it matters
    here: the full one makes connections of its own as it starts, and it
    takes a different switch to turn WebRTC's datagrams off."""
    pytest.importorskip("playwright")
    monkeypatch.delenv("MULTI_LLM_CHROMIUM", raising=False)
    if request.param == "a full build":
        if not _full_chromium():
            pytest.skip("no full Chromium build on this machine")
        monkeypatch.setenv("QUADRATUS_CHROMIUM", _full_chromium())
    else:
        monkeypatch.delenv("QUADRATUS_CHROMIUM", raising=False)
    served = _Services(_PAGES)
    yield served
    served.close()


def _launched(call):
    try:
        result = call()
    except Exception as exc:  # noqa: BLE001
        if "Executable doesn't exist" in str(exc) or "BrowserType.launch" in str(exc):
            pytest.skip(f"headless browser unavailable: {str(exc)[:120]}")
        raise
    time.sleep(0.4)     # anything still on its way to the other service
    return result


def _render(services, path, tmp_path, **kwargs):
    from quadratus.browser import render_page
    return _launched(lambda: render_page(services.url + path, out_dir=tmp_path / "out", wait_ms=1500,
                                         allow_navigation=services.allow, pin_requests=True, **kwargs))


def test_a_workers_websocket_never_reaches_another_service_and_the_render_is_unclean(services, tmp_path):
    """gate 2: `new Worker(...)` whose script opens a WebSocket to another
    loopback port reached it while the capture read clean; so did a blob
    worker, a nested worker, and a socket taken from a new frame's window."""
    evidence = _render(services, "/sockets", tmp_path)
    assert services.tcp_hits == [], "nothing reached the other service"
    port = services.tcp.getsockname()[1]
    assert not evidence.clean
    assert f"blocked outside the preview: a connection to 127.0.0.1:{port}" in evidence.failed_requests
    saved = json.loads((tmp_path / "out" / "evidence.json").read_text())
    assert saved["clean"] is False and saved["failed_requests"] == evidence.failed_requests


def test_webrtc_over_tcp_ends_at_the_harness_and_is_recorded(services, tmp_path):
    """gate 2: a TURN allocation over TCP reached another loopback port."""
    evidence = _render(services, "/rtc-tcp", tmp_path)
    assert services.tcp_hits == [], "nothing reached the other service"
    assert not evidence.clean and any("blocked outside the preview: a connection to 127.0.0.1" in f
                                      for f in evidence.failed_requests)


def test_no_datagram_leaves_a_pinned_browser(services, tmp_path):
    """gate 2: STUN and TURN over UDP, and a WebTransport session from the
    page and from a worker, each sent datagrams to another loopback port
    (a TCP listener cannot see them, which is how WebTransport read as
    held). The session the browser refused leaves the render unclean."""
    evidence = _render(services, "/datagrams", tmp_path)
    assert services.udp_hits == [] and services.tcp_hits == [], "nothing reached the other service"
    assert not evidence.clean and any("WebTransport" in e or "ERR_TUNNEL" in e for e in evidence.console_errors)
    # WebRTC alone, where no console error stands in for the hold: the full
    # build ignored the one switch the first fix sent, and every datagram
    # left while this page read clean.
    _render(services, "/rtc-udp", tmp_path)
    assert services.udp_hits == [], "nothing reached the other service"


def test_the_preview_itself_is_still_reached_and_an_ordinary_page_reads_clean(services, tmp_path):
    """gate 2: the hold is on what is off the origin. A worker's WebSocket to
    the preview's own address still arrives there, and a page that reaches
    nothing else reads clean: the browser's own connections as it starts
    (a full build makes five) are not the page's failed requests."""
    own = _render(services, "/own", tmp_path)
    assert "/own-socket" in services.preview_hits, "the origin is reached directly"
    assert own.clean and own.failed_requests == []
    plain = _render(services, "/plain", tmp_path)
    assert plain.clean and plain.failed_requests == [] and plain.console_errors == []
    assert all("was refused at the harness's proxy" in note for note in plain.observations)
    assert services.tcp_hits == [] and services.udp_hits == []


@pytest.mark.parametrize("host", ["127.0.0.1", "localhost"])
def test_a_page_that_loads_its_own_files_reads_clean_under_either_name(services, tmp_path, host):
    """gate 2: what an ordinary preview does. A stylesheet, a font, an image,
    a script, a module, a frame, a fetch, a worker's fetch and the page's
    own WebSocket all go to the preview, by 127.0.0.1 or by localhost."""
    from quadratus.browser import render_page
    base = f"http://{host}:{services.preview.server_port}"
    evidence = _launched(lambda: render_page(
        base + "/app", out_dir=tmp_path / "out", wait_ms=1500, pin_requests=True,
        allow_navigation=lambda url: url == base or url.startswith(base + "/")))
    assert evidence.clean, (evidence.failed_requests, evidence.console_errors)
    assert {"/app", "/app.css", "/font.woff2", "/pixel.png", "/pixel.png?bg", "/app.js", "/module.js", "/plain",
            "/data.json", "/data.json?worker", "/app-worker.js", "/page-socket"} <= set(services.preview_hits)
    assert services.tcp_hits == [] and services.udp_hits == []


def _ask(sink, args, proxy):
    """Start a browser with ``args``, give a context ``proxy``, and put the
    harness's two questions to it."""
    from playwright.sync_api import sync_playwright
    with sync_playwright() as pw:
        browser = pw.chromium.launch(args=args)
        try:
            sink.prove(browser.new_context(**({"proxy": proxy} if proxy else {})))
        finally:
            browser.close()


def test_a_browser_that_is_not_shown_to_hold_is_refused(monkeypatch):
    """gate 2: which switch and which proxy setting a browser honours is
    asked of each browser (two builds of one Chromium disagree). One whose
    loopback connections go round the proxy, or whose WebRTC still sends
    datagrams, is refused before any page opens; it never reads clean."""
    pytest.importorskip("playwright")
    from quadratus import browser
    monkeypatch.setattr(browser, "_GATHER_WAIT_MS", 1200)
    sink = browser._Sink()
    try:
        held = browser.held_proxy(sink)
        _launched(lambda: _ask(sink, browser.pinned_launch_args(sink=sink), held))
        for proxy in (None, dict(held, bypass="127.0.0.1")):   # no proxy; loopback exempt from it
            with pytest.raises(RuntimeError, match="did not go through the harness's proxy"):
                _ask(sink, browser.pinned_launch_args(sink=sink), proxy)
        with pytest.raises(RuntimeError, match="WebRTC still sends datagrams.*unset QUADRATUS_CHROMIUM"):
            _ask(sink, browser.pinned_launch_args(), held)
    finally:
        assert sink.close() == [], "the harness's own questions are nobody's failed request"


def test_a_capture_in_a_browser_that_does_not_hold_fails_and_writes_no_evidence(services, tmp_path, monkeypatch):
    """gate 2, at the capture: with the switches gone the render raises; it
    does not go on to a clean page.png."""
    from quadratus import browser
    monkeypatch.setattr(browser, "_GATHER_WAIT_MS", 1200)
    monkeypatch.setattr(browser, "_NO_WEBRTC_UDP", ())
    with pytest.raises(RuntimeError, match="request pinning is unavailable in this browser"):
        _render(services, "/plain", tmp_path)
    assert not (tmp_path / "out" / "page.png").exists() and not (tmp_path / "out" / "evidence.json").exists()


def test_the_harness_capture_holds_a_workers_socket(services, tmp_path):
    """gate 2, on the capture the harness runs (design_evidence.capture)."""
    from quadratus import design_evidence
    views = _launched(lambda: design_evidence.capture(services.url + "/sockets", "t1", tmp_path, None,
                                                      pinned=True, views=["desktop"], attempt="ab" * 8))
    assert services.tcp_hits == [], "nothing reached the other service"
    assert views["desktop"]["clean"] is False
    assert any("blocked outside the preview: a connection to" in f for f in views["desktop"]["failed_requests"])


def test_the_pilot_holds_a_workers_socket_and_lists_it(services, tmp_path):
    """gate 2: the pilot shares the hold; what it stopped is in ``blocked``."""
    from quadratus import browser_pilot

    replies = [{"action": "wait", "ms": 1200}, {"action": "done", "reason": "seen"}]
    run = _launched(lambda: browser_pilot.drive(lambda model, prompt: json.dumps(replies.pop(0)), goal="look",
                                                start_url=services.url + "/sockets", out_dir=tmp_path / "pilot",
                                                model="fake:pilot"))
    assert services.tcp_hits == [], "nothing reached the other service"
    assert any("blocked outside the preview: a connection to 127.0.0.1" in b for b in run.blocked)


def test_only_the_held_origins_are_reached_directly():
    """gate 2: the proxy a pinned context is given. Loopback is not exempt
    from it, and a host that could write a rule of its own gets none. The
    proxy is the context's: the browser itself starts with none, so its own
    services are not sent to the sink."""
    from quadratus.browser import _Sink, held_proxy, pinned_launch_args
    sink = _Sink()
    try:
        proxy = held_proxy(sink, ["http://127.0.0.1:5000/a", "https://localhost", "http://[::1]:8080",
                                  "file:///x/index.html", "http://localhost;*:80/", "http://*/",
                                  "http://a,b/"])
        args = pinned_launch_args(sink=sink)
    finally:
        assert sink.close() == []
    assert proxy == {"server": f"http://127.0.0.1:{sink.port}",
                     "bypass": "<-loopback>,127.0.0.1:5000,[::1]:8080,localhost:443"}
    # Both spellings: the headless shell honours the first, a full build the second.
    assert {"--force-webrtc-ip-handling-policy=disable_non_proxied_udp",
            "--webrtc-ip-handling-policy=disable_non_proxied_udp"} <= set(args)
    assert not any(a.startswith("--proxy") for a in [*args, *pinned_launch_args()])
    assert not any("webrtc" in a for a in pinned_launch_args())


def test_the_sink_notes_each_address_once_and_forwards_nothing():
    """gate 2: a connection to a local address is the page's, and is a failed
    request. One anywhere else is refused the same way and noted apart: a
    browser makes those for itself."""
    from quadratus.browser import _HOLD_CHECK, _Sink
    sink = _Sink()
    heads = (b"CONNECT 127.0.0.1:9 HTTP/1.1\r\nHost: 127.0.0.1:9\r\n\r\n", b"CONNECT 127.0.0.1:9 HTTP/1.1\r\n\r\n",
             b"GET http://127.0.0.1:9/x HTTP/1.1\r\n\r\n", b"", b"CONNECT [::1]:8080 HTTP/1.1\r\n\r\n",
             b"CONNECT db:5432 HTTP/1.1\r\n\r\n", b"CONNECT 192.168.1.20:80 HTTP/1.1\r\n\r\n",
             b"CONNECT printer.local:631 HTTP/1.1\r\n\r\n", b"CONNECT [not-an-address HTTP/1.1\r\n\r\n",
             b"CONNECT www.google.com:443 HTTP/1.1\r\n\r\n", b"CONNECT www.google.com:443 HTTP/1.1\r\n\r\n",
             b"GET http://clients2.google.com/time HTTP/1.1\r\n\r\n",
             # The harness's own question is exempt only while it is being asked.
             f"GET http://127.0.0.1:{sink.port}/{_HOLD_CHECK} HTTP/1.1\r\n\r\n".encode())
    for head in heads:
        with socket.create_connection(("127.0.0.1", sink.port), timeout=5) as client:
            client.sendall(head)
            client.shutdown(socket.SHUT_WR)
            assert client.recv(100) == b"", "closed with nothing sent back"
    assert sink.close() == ["blocked outside the preview: a connection to 127.0.0.1:9",
                            "blocked outside the preview: http://127.0.0.1:9/x",
                            "blocked outside the preview: a connection that named no address",
                            "blocked outside the preview: a connection to [::1]:8080",
                            "blocked outside the preview: a connection to db:5432",
                            "blocked outside the preview: a connection to 192.168.1.20:80",
                            "blocked outside the preview: a connection to printer.local:631",
                            "blocked outside the preview: a connection to an address that could not be read",
                            f"blocked outside the preview: http://127.0.0.1:{sink.port}/{_HOLD_CHECK}"]
    assert sink.elsewhere == ["a connection to www.google.com:443 was refused at the harness's proxy",
                              "http://clients2.google.com/time was refused at the harness's proxy"]


# --------------------------------------------------------------------------
# gate 1: runners whose summary line does not start with a count. Every
# text below is the runner's own stdout, captured from a real run
# (cucumber-js 13.3.0, Robot Framework 7.5.1, CodeceptJS 4.2.3,
# @web/test-runner 0.20 with Chromium).

_R, _D = "=" * 78, "-" * 78
_BAR = "█" * 30
_ERASE = "\x1b[2K\x1b[1A\x1b[2K\x1b[G"


def _wtr(row):
    return (f"\nChromium: |{_BAR}| 0/1 test files | 0 passed, 0 failed\n\nRunning tests...\n\n"
            f"Running 1 test files...\n\n\n{_ERASE}{row}\n\n")


_CUCUMBER_SKIPPED = "------\n\n2 scenarios (2 skipped)\n6 steps (6 skipped)\n0m 0.010s (0m 0.000s executing your code)\n"
_CUCUMBER_MIXED = ("..-F-\n\n3 scenarios (1 passed, 1 skipped, 1 failed)\n8 steps (3 passed, 4 skipped, 1 failed)\n"
                   "0m 0.012s (0m 0.001s executing your code)\n")
_CUCUMBER_PASSED = "..\n\n1 scenario (1 passed)\n2 steps (2 passed)\n0m 0.005s (0m 0.000s executing your code)\n"
_CUCUMBER_PENDING = (".PU\n\nPending scenarios:\n  1) waits # features/pending.feature:2\n\n"
                     "2 scenarios (1 pending, 1 undefined)\n3 steps (1 passed, 1 pending, 1 undefined)\n"
                     "0m 0.010s (0m 0.000s executing your code)\n")
_ROBOT_SKIPPED = (f"{_R}\nAll Skip{' ' * 70}\n{_R}\nOne{' ' * 67}| SKIP |\nnot today\n{_D}\n"
                  f"Two{' ' * 67}| SKIP |\nnot today\n{_D}\nAll Skip{' ' * 62}| SKIP |\n"
                  f"2 tests, 0 passed, 0 failed, 2 skipped\n{_R}\nOutput:  NONE\n")
_ROBOT_PASSED = (f"{_R}\nPassing{' ' * 71}\n{_R}\nPasses{' ' * 64}| PASS |\n{_D}\nAlso{' ' * 66}| PASS |\n{_D}\n"
                 f"Passing{' ' * 63}| PASS |\n2 tests, 2 passed, 0 failed\n{_R}\nOutput:  NONE\n")
_ROBOT_MIXED = (f"{_R}\nMixed{' ' * 73}\n{_R}\nPasses{' ' * 64}| PASS |\n{_D}\nSkipped{' ' * 63}| SKIP |\nnot today\n"
                f"{_D}\nFails{' ' * 65}| FAIL |\nboom\n{_D}\nMixed{' ' * 65}| FAIL |\n"
                f"3 tests, 1 passed, 1 failed, 1 skipped\n{_R}\nOutput:  NONE\n")
_CODECEPT_SKIPPED = ("CodeceptJS v4.2.3 #StandWithUkraine\nUsing test root \"/work/cc\"\n\nall skipped --\n\n  S one\n\n"
                     "  S two\n\n  OK  | 0 passed, 2 skipped   // 3ms\n")
_CODECEPT_MIXED = ("CodeceptJS v4.2.3 #StandWithUkraine\nUsing test root \"/work/cc\"\n\nmixed --\n\n  ✔ one in 7ms\n\n"
                   "  ✔ two in 1ms\n\n  ✔ three in 0ms\n\n  S skipped\n\n  OK  | 3 passed, 1 skipped   // 20ms\n")
_CODECEPT_FAILED = ("\n  FAIL  | 1 passed, 1 failed, 1 skipped   // 15ms\n"
                    "Run with --verbose flag to see complete NodeJS stacktrace\n")
_WTR_SKIPPED = _wtr(f"Chromium: |{_BAR}| 1/1 test files | 0 passed, 0 failed, 2 skipped") + (
    "Finished running tests in 0.3s, all tests passed! \U0001f389\n\n\n")
_WTR_MIXED = _wtr(f"Chromium: |{_BAR}| 1/1 test files | 3 passed, 0 failed, 1 skipped") + (
    "Finished running tests in 0.3s, all tests passed! \U0001f389\n\n\n")
_WTR_FAILED = _wtr(f"Chromium: |{_BAR}| 1/1 test files | 1 passed, 1 failed") + (
    "Finished running tests in 0.3s with 1 failed tests.\n\n")

# behave 1.3.3, captured the same way (found in the review of the fix: its
# all-skipped run exits 0 and read as no summary at all).
_BEHAVE_HEAD = "USING RUNNER: behave.runner:Runner\nfeatures/f.feature  "
_BEHAVE_SKIPPED = _BEHAVE_HEAD + ("SS\n\n0 features passed, 0 failed, 1 skipped\n"
                                  "0 scenarios passed, 0 failed, 2 skipped\n0 steps passed, 0 failed, 4 skipped\n"
                                  "Took 0min 0.000s\n")
_BEHAVE_PASSED = _BEHAVE_HEAD + ("..\n\n1 feature passed, 0 failed, 0 skipped\n"
                                 "2 scenarios passed, 0 failed, 0 skipped\n4 steps passed, 0 failed, 0 skipped\n"
                                 "Took 0min 0.000s\n")
_BEHAVE_MIXED = _BEHAVE_HEAD + (".SF\n\n\nFailing scenarios:\n  features/f.feature:9  three\n\n"
                                "0 features passed, 1 failed, 0 skipped\n1 scenario passed, 1 failed, 1 skipped\n"
                                "3 steps passed, 1 failed, 2 skipped\nTook 0min 0.000s\n")
_BEHAVE_UNDEFINED = _BEHAVE_HEAD + ("E\n\n\nErrored scenarios:\n  features/f.feature:2  one\n\n"
                                    "0 features passed, 0 failed, 1 error, 0 skipped\n"
                                    "0 scenarios passed, 0 failed, 1 error, 0 skipped\n"
                                    "1 step passed, 0 failed, 0 skipped, 1 undefined\nTook 0min 0.000s\n")

#: name: (argv, stdout, cases executed, cases skipped)
_REAL_SUMMARIES = {
    "behave, all skipped": (("behave",), _BEHAVE_SKIPPED, 0, 2),
    "behave, passed": (("behave",), _BEHAVE_PASSED, 2, 0),
    "behave, mixed": (("behave",), _BEHAVE_MIXED, 2, 1),
    "behave, an undefined step": (("behave",), _BEHAVE_UNDEFINED, 0, 1),
    "cucumber-js, all skipped": (("npx", "cucumber-js"), _CUCUMBER_SKIPPED, 0, 2),
    "cucumber-js, mixed": (("npx", "cucumber-js"), _CUCUMBER_MIXED, 2, 1),
    "cucumber-js, passed": (("npx", "cucumber-js"), _CUCUMBER_PASSED, 1, 0),
    "cucumber-js, pending and undefined": (("npx", "cucumber-js"), _CUCUMBER_PENDING, 0, 2),
    "robot, all skipped": (("robot", "tests"), _ROBOT_SKIPPED, 0, 2),
    "robot, passed": (("robot", "tests"), _ROBOT_PASSED, 2, 0),
    "robot, mixed": (("robot", "tests"), _ROBOT_MIXED, 2, 1),
    "codeceptjs, all skipped": (("npx", "codeceptjs", "run"), _CODECEPT_SKIPPED, 0, 2),
    "codeceptjs, mixed": (("npx", "codeceptjs", "run"), _CODECEPT_MIXED, 3, 1),
    "codeceptjs, failed": (("npx", "codeceptjs", "run"), _CODECEPT_FAILED, 2, 1),
    "web-test-runner, all skipped": (("npx", "wtr"), _WTR_SKIPPED, 0, 2),
    "web-test-runner, mixed": (("npx", "wtr"), _WTR_MIXED, 3, 1),
    "web-test-runner, failed": (("npx", "wtr"), _WTR_FAILED, 2, 0),
}


def _read(stdout, argv=("npx", "runner"), stderr=""):
    from types import SimpleNamespace

    from quadratus.integration import _executed, _kept, _Output, runner_of, skipped_count
    output = _Output(stdout, stderr, None)
    return (_executed(output, runner_of(list(argv)), None),
            skipped_count(SimpleNamespace(output=_kept(output), report=None)))


@pytest.mark.parametrize("name", sorted(_REAL_SUMMARIES))
def test_a_runners_own_row_is_read_for_what_ran_and_what_was_skipped(name):
    """gate 1: these summaries read as no summary at all, so the count and
    the skips were both unknown."""
    argv, stdout, executed, skipped = _REAL_SUMMARIES[name]
    assert _read(stdout, argv) == (executed, skipped)


def _stand_in(tmp_path, monkeypatch, program, stdout, code=0):
    """A project and a PATH whose ``program`` prints ``stdout`` and exits ``code``."""
    project, bindir = tmp_path / "project", tmp_path / "bin"
    project.mkdir()
    bindir.mkdir()
    (project / "package.json").write_text('{"scripts": {"test": "runner"}}')
    (bindir / "out.txt").write_text(stdout, encoding="utf-8")
    fake = bindir / program
    fake.write_text(f"#!/bin/sh\ncat '{bindir}/out.txt'\nexit {code}\n")
    fake.chmod(0o755)
    monkeypatch.setenv("PATH", f"{bindir}:/usr/bin:/bin")
    return project


@pytest.mark.parametrize("name", sorted(n for n, row in _REAL_SUMMARIES.items() if row[2] == 0))
def test_a_run_in_which_nothing_executed_fails_the_gate(name, tmp_path, monkeypatch):
    """gate 1, a false success: the runner exits 0 with every case skipped
    (or pending), and the gate passed with its count unknown."""
    from quadratus.integration import GateCommand, GateSuite, skipped_count
    argv, stdout, _, skipped = _REAL_SUMMARIES[name]
    project = _stand_in(tmp_path, monkeypatch, argv[0], stdout)
    result = GateSuite([GateCommand(id="check", argv=argv)], cwd=project).run()
    receipt = result.receipts[0]
    assert not result.passed and (receipt.status, receipt.reason) == ("failed", "zero tests executed")
    assert receipt.tests == 0 and skipped_count(receipt) == skipped


@pytest.mark.parametrize("name", ["cucumber-js, passed", "robot, passed", "codeceptjs, mixed",
                                  "web-test-runner, mixed", "behave, passed"])
def test_the_same_suites_under_npm_test_pass_with_their_count(name, tmp_path, monkeypatch):
    """gate 1: `npm test` needs a count (minimum 1), and these runners' rows
    gave none, so a passing suite was blocked at its first gate."""
    from quadratus.integration import GateCommand, GateSuite, skipped_count
    _, stdout, executed, skipped = _REAL_SUMMARIES[name]
    project = _stand_in(tmp_path, monkeypatch, "npm", stdout)
    result = GateSuite([GateCommand(id="check", argv=("npm", "test"), minimum_tests=1)], cwd=project).run()
    receipt = result.receipts[0]
    assert result.passed and (receipt.status, receipt.tests, skipped_count(receipt)) == ("passed", executed, skipped)


@pytest.mark.parametrize("line", [
    "✖ 5 problems (0 errors, 5 warnings)",                    # eslint
    "Found 0 errors. Watching for file changes.",                  # tsc
    "8 steps (3 passed, 4 skipped, 1 failed)",                     # cucumber's steps are not its cases
    "3 steps (1 passed, 1 pending, 1 undefined)",
    "Spec Files:\t 1 passed, 1 total (100% completed) in 00:00:02",  # WebdriverIO counts files
    "checked 3 scenarios (3 passed) in the plan",                  # a sentence, not a row
    "3 tests, 3 passed, 0 failed, see the report",
    "  ✔ OK  | 3 passed, 1 skipped   // 20ms",                # a case's own line
    "loaded 0 scenarios",                                          # a log line, not a run of nothing
    "the plan has 2 scenarios (2 skipped)",
    "0 features passed, 0 failed, 1 skipped",                      # behave counts features and steps too
    "4 steps passed, 0 failed, 0 skipped",
    "2 scenarios passed, see the report",
])
def test_a_count_inside_another_line_is_still_no_summary(line):
    """gate 1: the rows are read whole. A count behind "(" is admitted for
    cucumber's scenario row alone, and a row that counts files or steps is
    no count of cases: unknown, never executed."""
    assert _read(line + "\n") == (None, None)
    assert _read("noise\n" + line + "\nmore\n", ("npm", "test")) == (None, None)


def test_a_count_that_is_unknown_blocks_a_command_that_needs_one(tmp_path, monkeypatch):
    """gate 1: WebdriverIO's own row counts spec files, and a file whose
    every case was skipped passes. It is not read as cases, so `npm test`
    stays blocked for want of a count rather than passing on a file count."""
    from quadratus.integration import GateCommand, GateSuite
    project = _stand_in(tmp_path, monkeypatch, "npm",
                        "Spec Files:\t 1 passed, 1 total (100% completed) in 00:00:02\n")
    result = GateSuite([GateCommand(id="check", argv=("npm", "test"), minimum_tests=1)], cwd=project).run()
    assert not result.passed
    assert (result.receipts[0].status, result.receipts[0].reason, result.receipts[0].tests) == (
        "blocked", "test count unavailable", None)


def test_a_row_behind_a_wrappers_label_is_read_and_browsers_are_summed():
    """gate 1: the rows are read behind a label as the count lines are, and
    @web/test-runner's row for each browser counts."""
    assert _read("web:test: 2 scenarios (2 skipped)\nweb:test: 6 steps (6 skipped)\n") == (0, 2)
    assert _read("svc-1  | 3 tests, 1 passed, 1 failed, 1 skipped\n") == (2, 1)
    assert _read("e2e:test:   OK  | 3 passed, 1 skipped   // 20ms\n") == (3, 1)
    assert _read("0 scenarios\n0 steps\n0m00.000s\n")[0] == 0
    assert _read("api:test: 0 scenarios passed, 0 failed, 2 skipped\napi:test: 0 steps passed, 0 failed, 4 skipped\n"
                 ) == (0, 2)
    rows = (f"Chromium: |{_BAR}| 2/2 test files | 3 passed, 0 failed, 1 skipped\n"
            f"Firefox:  |{_BAR}| 2/2 test files | 0 passed, 0 failed, 4 skipped\n")
    done = "\nFinished running tests in 1.2s, all tests passed!\n"
    assert _read(rows + done) == (3, 5)
    # Behind a wrapper's label each browser still counts: read as the last
    # row alone, this passing run had executed nothing.
    assert _read("".join("web:test: " + row + "\n" for row in rows.splitlines()) + done) == (3, 5)
    # The same browser's row printed again is the same cases, not more.
    again = f"Chromium: |{_BAR}| 1/2 test files | 2 passed, 0 failed\n" + rows.splitlines()[0] + "\n"
    assert _read(again + done) == (3, 1)


def test_an_unknown_count_is_never_a_complete_or_verified_run():
    """gate 1, the downstream half: a check whose count is unknown is not a
    run that executed its cases, wherever that is decided."""
    from quadratus import session
    assert session._executed_clean(None, None) == "its counts are unknown"
    run = dict(passed=True, required=True, filtered=False, tests=None, skipped=None)
    assert not session._complete_run(run)
    assert not session._complete_run(dict(run, tests=3)), "skips unknown"
    assert not session._complete_run(dict(run, skipped=0)), "count unknown"
    assert session._complete_run(dict(run, tests=3, skipped=0))


# --------------------------------------------------------------------------
# gate 3: the contest's gate copy holds the withheld files for the gate run
# only, as the original-tests copy does.

_NEEDS_ENV = ("import pathlib\nimport sys\n"
              "ok = pathlib.Path('.env').read_text() == 'X=1\\n' and pathlib.Path('conf/.env.test').exists()\n"
              "sys.exit(0 if ok and not pathlib.Path('private/.env').exists() else 1)\n")


def _listing(root):
    import os
    return sorted(os.path.join(folder, name)[len(str(root)):] for folder, dirs, files in os.walk(root)
                  for name in [*dirs, *files])


def _contest_project(tmp_path):
    project, copy = tmp_path / "project", tmp_path / "copy"
    for root in (project, copy):
        root.mkdir()
        (root / "check.py").write_text(_NEEDS_ENV)
        (root / "app.py").write_text("X = 1\n")
    (project / ".env").write_text("X=1\n")
    for folder in ("conf", "private"):
        (project / folder).mkdir()
    (project / "conf" / ".env.test").write_text("T=1\n")
    (project / "private" / ".env").write_text("SECRET=1\n")
    return project, copy


def test_the_gate_on_a_contest_copy_reads_the_withheld_files_and_leaves_none_behind(tmp_path):
    """gate 3: a suite that reads .env failed in both drafts and passed in
    the project, so the drafts were ranked on noise."""
    import sys

    from quadratus.integration import IntegrationGate, gate_in_copy
    project, copy = _contest_project(tmp_path)
    gate = IntegrationGate([sys.executable, "check.py"], cwd=project)
    before = _listing(copy)
    with gate_in_copy(gate, project, copy, exclude=[project / "private"]) as bound:
        assert (copy / ".env").read_text() == "X=1\n" and (copy / "conf" / ".env.test").exists()
        assert not (copy / "private").exists(), "the operator's excluded paths stay out"
        result = bound.run()
    assert result.passed, result.output
    assert _listing(copy) == before, "nothing placed for the run is left in the copy"
    assert (project / ".env").read_text() == "X=1\n", "the project is not touched"


def test_a_drafts_own_file_and_a_link_in_the_copy_are_left_alone(tmp_path):
    """gate 3: the copy is a draft a model wrote. A file it already has
    under a withheld name stays the draft's, before and after, and nothing
    is written through a link it holds."""
    import sys

    from quadratus.integration import IntegrationGate, gate_in_copy
    project, copy = _contest_project(tmp_path)
    outside = tmp_path / "outside"
    outside.mkdir()
    (copy / ".env").write_text("DRAFT=1\n")
    (copy / "conf").symlink_to(outside, target_is_directory=True)
    gate = IntegrationGate([sys.executable, "check.py"], cwd=project)
    with gate_in_copy(gate, project, copy) as bound:
        assert (copy / ".env").read_text() == "DRAFT=1\n"
        assert list(outside.iterdir()) == [], "nothing was placed behind the link"
        assert (copy / "private" / ".env").exists(), "no exclusion was given, so this one is placed"
        assert not bound.run().passed
    assert (copy / ".env").read_text() == "DRAFT=1\n" and (copy / "conf").is_symlink()
    assert not (copy / "private").exists() and list(outside.iterdir()) == []


def test_the_contest_passes_the_operators_exclusions_to_the_copys_gate(tmp_path):
    """gate 3, at the session's call: the copy's gate is given what the
    original-tests copy is given, the operator's excluded paths included."""
    import sys
    from types import SimpleNamespace

    from quadratus import session
    from quadratus.integration import IntegrationGate
    project, copy = _contest_project(tmp_path)
    gate = IntegrationGate([sys.executable, "check.py"], cwd=project)
    stub = SimpleNamespace(config=SimpleNamespace(integration_gate=gate, project_excludes=(project / "private",)),
                           project=project, _current_gate=lambda: gate, _required=lambda *a, **k: True,
                           _activity=lambda *a, **k: None, _verify_dependencies=lambda *a, **k: None)
    entries = {"routed": {}}
    fatal = session.Session._contest_gates(stub, SimpleNamespace(task_id="t1"), entries,
                                           {"routed": SimpleNamespace(project=copy)})
    assert fatal is None and entries["routed"]["gate"]["status"] == "passed", entries
    assert not (copy / ".env").exists() and not (copy / "conf").exists() and not (copy / "private").exists()


# --------------------------------------------------------------------------
# gate 4: the names of a quiet run's passed cases are kept on the run's
# newest check only.

def _recorder():
    """What ``Session._record_check_attempt`` reads of a session. Since the
    merge that includes the run's order counter (``_next_run_seq``), which
    numbers each check attempt."""
    import itertools
    from types import SimpleNamespace
    outcome = SimpleNamespace(checks=[], note=lambda *a, **k: "noted")
    return SimpleNamespace(_outcome=outcome, checks=[], review_checks=[], run_outcome=outcome,
                           store=SimpleNamespace(put=lambda *a, **k: SimpleNamespace(id="a1")),
                           _source_identity=lambda: "source", _next_run_seq=itertools.count(1).__next__)


def _record_as_the_session_does(stub, result):
    """One check: the recorder, then the caller's own append (session.py)."""
    import dataclasses

    from quadratus import session
    session.Session._record_check_attempt(stub, result)
    stub.checks.append({"passed": result.passed, "command": result.command, "cases": result.cases,
                        "report": result.report, "source": "source",
                        "receipts": [dataclasses.asdict(r) for r in result.receipts]})


def _named_report(names):
    return dict(state="parsed", producer="quadratus-pytest/5", exitstatus=0, collected=len(names), truncated=False,
                collect_errors=0, counts=dict(passed=len(names), failed=0, errors=0, skipped=0),
                passed_cases=list(names), passed_truncated=False)


def _suite_result(reports):
    from quadratus.integration import GateReceipt, GateResult
    receipts = tuple(GateReceipt(id=f"check-{n}", status="passed", reason="exit 0", required=True,
                                 command="pytest -q", returncode=0, output="5000 passed in 9.1s", tests=len(r["passed_cases"]),
                                 report=r) for n, r in enumerate(reports))
    return GateResult(True, "gate suite", 0, "check: passed", receipts)


def test_a_quiet_suites_passed_names_are_on_the_newest_check_only(tmp_path):
    """gate 4, on a real quiet pytest run with the harness report: each
    check's record carried every name, and the task outcome carried them
    again."""
    import sys

    from quadratus.integration import GateCommand, GateSuite, _named_passes
    project = tmp_path / "project"
    project.mkdir()
    (project / "test_many.py").write_text(
        "import pytest\n\n@pytest.mark.parametrize('case', range(120))\n"
        "def test_the_feature_keeps_its_documented_behaviour(case):\n    assert case >= 0\n")
    suite = GateSuite([GateCommand(id="check", argv=(sys.executable, "-m", "pytest", "-q", "-p", "no:cacheprovider",
                                                     "--quadratus-report={report}"), minimum_tests=1)], cwd=project)
    stub = _recorder()
    for _ in range(3):
        result = suite.run()
        assert result.passed and len(_named_passes(result.receipts[0].report)) == 120, result.output
        _record_as_the_session_does(stub, result)
    written = json.dumps({"checks": stub.checks, "outcome": stub._outcome.checks})
    name = "test_many.py::test_the_feature_keeps_its_documented_behaviour[77]"
    assert written.count(json.dumps(name)) == 1, "one copy in the whole record"
    newest = stub.checks[-1]["receipts"][0]["report"]
    assert len(_named_passes(newest)) == 120, "the readers of names still find them on the newest check"
    for record in [*stub.checks[:-1], *stub._outcome.checks]:
        report = record["receipts"][0]["report"]
        assert "passed_cases" not in report and report["passed_cases_omitted"] == 120
        assert report["counts"]["passed"] == 120 and report["state"] == "parsed", "the counts stay"


def test_forty_checks_of_a_large_quiet_suite_stay_inside_the_monitors_read_bound():
    """gate 4: nine checks of a 5,000-case quiet suite took result.json past
    monitor.MAX_JSON_BYTES, and the monitor then showed the finished run's
    status as unknown."""
    from quadratus import monitor
    names = [f"tests/test_project_feature_number_{n % 10}.py::test_the_feature_keeps_its_documented_behaviour[{n}]"
             for n in range(5000)]
    stub = _recorder()
    for _ in range(40):
        _record_as_the_session_does(stub, _suite_result([_named_report(names)]))
    written = json.dumps({"checks": stub.checks, "outcomes": stub._outcome.checks})
    one = len(json.dumps(names))
    assert one > 400_000, "the suite is a large one"
    assert len(written) < 2 * one < monitor.MAX_JSON_BYTES
    assert written.count(json.dumps(names[4999])) == 1


def test_the_test_change_rule_still_reads_a_quiet_runs_names_from_the_newest_check():
    """gate 4: the readers keep working. The recorded form of the newest
    check shows a retained case executing; an earlier check's no longer
    does, and says nothing either way."""
    from quadratus.integration import _named_passes, repair_problems
    names = ["tests/test_cart.py::test_total", "tests/test_cart.py::test_empty"]
    stub = _recorder()
    for _ in range(2):
        _record_as_the_session_does(stub, _suite_result([_named_report(names)]))
    original = b"def test_total():\n    assert 1\n\n\ndef test_empty():\n    assert 1\n"
    current = {"tests/test_cart.py": original + b"\n# repaired\n"}
    assert repair_problems("tests/test_cart.py", original, current, stub.checks[-1]["receipts"]) == []
    assert _named_passes(stub.checks[0]["receipts"][0]["report"]) == set()
    assert repair_problems("tests/test_cart.py", original, current, stub.checks[0]["receipts"]) != []


def test_one_gate_runs_names_are_bounded_across_its_commands():
    """gate 4: the producer bounds one report. A run of many reporting
    commands is bounded too, and a report past the bound says its names
    are incomplete rather than naming none silently."""
    from quadratus import integration
    from quadratus.integration import _named_passes, _within_name_bound
    names = [f"tests/test_module_{n:05}.py::test_case_with_a_long_descriptive_name[{n}]" for n in range(11_000)]
    receipts = _within_name_bound(list(_suite_result([_named_report(names) for _ in range(4)]).receipts))
    kept = [r for r in receipts if isinstance(r.report.get("passed_cases"), list)]
    assert 1 <= len(kept) < 4
    assert sum(len(n) + 4 for r in kept for n in r.report["passed_cases"]) <= integration._MAX_RUN_NAME_CHARS
    for receipt in receipts[len(kept):]:
        assert receipt.report["passed_truncated"] is True and receipt.report["passed_cases_omitted"] == 11_000
        assert receipt.report["counts"]["passed"] == 11_000 and _named_passes(receipt.report) == set()
    assert len(_named_passes(receipts[0].report)) == 11_000


def test_an_observed_check_keeps_counts_and_no_names(tmp_path):
    """gate 4: an observation before review is read for its counts. It went
    to result.json with every name as well."""
    from quadratus.artifacts import ArtifactStore
    from quadratus.integration import GateCommand, GateSuite
    from quadratus.session import Session, SessionConfig, TaskSpec
    project = tmp_path / "project"
    project.mkdir()
    (project / "app.py").write_text("X = 1\n")
    names = [f"tests/test_a.py::test_case[{n}]" for n in range(50)]
    gate = GateSuite([GateCommand(id="check", argv=("pytest", "-q"))], cwd=project)
    gate.run = lambda: _suite_result([_named_report(names)])
    # A real session, not a stub of one: since the merge an observation also
    # records the identity it ran under, as an operation of the session.
    session = Session("goal", ArtifactStore(tmp_path / "artifacts"), lambda *a, **k: "",
                      config=SessionConfig(project=project, integration_gate=gate))
    session._observe_checks(TaskSpec("t1", "work"), "review")
    (observed,) = session.review_checks
    assert observed["passed"] and names[7] not in json.dumps(session.review_checks)
    assert observed["receipts"][0]["report"]["passed_cases_omitted"] == 50
    assert observed["receipts"][0]["report"]["counts"]["passed"] == 50 and observed["receipts"][0]["tests"] == 50


# --------------------------------------------------------------------------
# gate 5: a failing check that reports on stderr keeps the failure.

_COVERAGE = "> p@1.0.0 test\n> jest --coverage\n\n" + "".join(
    f"src/module_{n:03}.js      |     100 |      100 |     100 |     100 |\n" for n in range(60))
_JEST_FAILURE = ("FAIL ./cart.test.js\n  x totals the cart (5 ms)\n\n  * totals the cart\n\n"
                 "    expect(received).toBe(expected)\n\n    Expected: 42\n    Received: 41\n\n"
                 "      at Object.<anonymous> (cart.test.js:12:25)\n\n" + "    at node:internal frame\n" * 30)
_JEST_SUMMARY = ("Test Suites: 1 failed, 1 total\nTests:       1 failed, 2 passed, 1 skipped, 4 total\n"
                 "Snapshots:   0 total\nTime:        0.6 s\nRan all test suites.\n"
                 "npm error Lifecycle script `test` failed with error:\nnpm error code 1\nnpm error path /p\n"
                 "npm error workspace p@1.0.0\nnpm error location /p\nnpm error command failed\n"
                 "npm error command sh -c jest --coverage\n")


def test_a_failing_run_that_reports_on_stderr_keeps_its_assertion_and_its_summary(tmp_path, monkeypatch):
    """gate 5: jest through npm test, a coverage table on stdout and the
    failure on stderr. The receipt kept 600 characters of stderr, which was
    npm's own error lines: the assertion was gone from the only copy kept."""
    from quadratus.integration import _TAIL_CHARS, GateCommand, GateSuite, skipped_count
    project = _stand_in(tmp_path, monkeypatch, "npm", _COVERAGE, code=1)
    (tmp_path / "bin" / "err.txt").write_text(_JEST_FAILURE + _JEST_SUMMARY)
    fake = tmp_path / "bin" / "npm"
    fake.write_text(fake.read_text().replace("exit 1", f"cat '{tmp_path}/bin/err.txt' >&2\nexit 1"))
    result = GateSuite([GateCommand(id="check", argv=("npm", "test"), minimum_tests=1)], cwd=project).run()
    receipt = result.receipts[0]
    assert (receipt.status, receipt.reason) == ("failed", "nonzero exit") and len(receipt.output) <= _TAIL_CHARS
    assert "Expected: 42" in receipt.output and "Received: 41" in receipt.output, "the failure itself"
    assert "npm error code 1" in receipt.output
    # The summary readers still read the runner's own line from what is kept.
    assert receipt.tests == 3 and skipped_count(receipt) == 1
    assert "src/module_059.js" in receipt.output, "stdout keeps its end"


def test_the_shares_turn_only_for_a_failure_off_a_stdout_runner():
    """gate 5: a passing command and a pytest or node --test command keep
    stdout's share as before (checks 15), and stdout is still last, so its
    own summary is the one the readers meet."""
    from quadratus.integration import _kept, _Output, _reports_on_stderr, _test_count
    assert _reports_on_stderr(None, False) and not _reports_on_stderr(None, True)
    assert not _reports_on_stderr("pytest", False) and not _reports_on_stderr("node", False)
    stdout = "x" * 3000 + "\n===== 1 failed, 2 passed in 0.10s =====\n"
    stderr = "E" * 3000 + "\nAssertionError: boom\n5 passed\n"
    plain, turned = _kept(_Output(stdout, stderr, None)), _kept(_Output(stdout, stderr, None), favour_stderr=True)
    for kept in (plain, turned):
        assert len(kept) <= 2000 and kept.endswith("1 failed, 2 passed in 0.10s =====")
        assert _test_count(kept) == 3, "stdout's summary, not the line an exit handler wrote to stderr"
        assert "AssertionError: boom" in kept
    assert plain.count("E") < 700 < turned.count("E"), "which stream keeps the larger share"
    short = _Output("ok\n", "Traceback\nValueError: boom\n", None)
    assert _kept(short, favour_stderr=True) == _kept(short) == "Traceback\nValueError: boom\nok"

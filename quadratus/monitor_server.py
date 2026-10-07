"""A monitor-only web page, so the run status opens on a phone.

``quadratus --monitor --serve PORT`` runs this. It is the same bounded,
read-only reader as the GUI tab and the text screen (``quadratus.monitor``),
rendered as one mobile-sized HTML page that reloads itself every few
seconds, plus the same dict as JSON. Nothing else: no form that starts a
run, no file browser, no command. The Gradio GUI stays on loopback because
its Project tab can run check commands; this page has no controls at all,
which is what makes it safe to reach from another device.

It binds to 127.0.0.1 only, on purpose, and there is no flag to change
that. The way onto a phone is Tailscale Serve on the Mac
(``tailscale serve --bg PORT``), which proxies the loopback port to an
HTTPS address only the operator's own devices can open. Safari's "Add to
Home Screen" then makes it an icon; the page declares itself a standalone
web app so it opens without browser chrome.

The page accepts ``?project=`` and ``?series=`` so one server covers any
project or series without a restart; the values given on the command line
are the defaults. Reading another path's run directory is the same read the
command line would make, bounded the same way.
"""

from __future__ import annotations

import html
import json
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any, Dict, List, Optional
from urllib.parse import parse_qs, urlsplit

from .monitor import UNKNOWN, read_status, run_history

__all__ = ["MonitorServer", "render_html", "serve", "DEFAULT_PORT", "REFRESH_SECONDS"]

DEFAULT_PORT = 7861
#: The page reloads itself this often. Every read is bounded, so it is cheap.
REFRESH_SECONDS = 5
#: Loopback only; Tailscale Serve is the way off this machine.
BIND = "127.0.0.1"

_MANIFEST = {
    "name": "Quadratus monitor",
    "short_name": "Quadratus",
    "start_url": "/",
    "display": "standalone",
    "background_color": "#111318",
    "theme_color": "#111318",
}

_CSS = """
:root { color-scheme: light dark; }
body { margin: 0; padding: 16px; font: 16px/1.45 -apple-system, system-ui, sans-serif;
       background: #111318; color: #e6e6e6; max-width: 720px; margin-inline: auto; }
h1 { font-size: 22px; margin: 0 0 4px; letter-spacing: 2px; }
.live { font-size: 28px; font-weight: 700; margin: 8px 0 2px; }
.live.yes { color: #4ade80; } .live.no { color: #f87171; } .live.unknown { color: #fbbf24; }
.why { color: #9aa0a6; font-size: 14px; margin-bottom: 14px; }
dl { display: grid; grid-template-columns: 7.5em 1fr; gap: 6px 10px; margin: 0; }
dt { color: #9aa0a6; } dd { margin: 0; word-break: break-word; }
.bar { height: 8px; background: #2a2e37; border-radius: 4px; overflow: hidden; margin: 6px 0 2px; }
.bar > div { height: 100%; background: #60a5fa; }
.ended { margin-top: 14px; padding: 10px; border: 1px solid #2a2e37; border-radius: 8px; }
table { width: 100%; border-collapse: collapse; margin-top: 16px; font-size: 14px; }
th, td { text-align: left; padding: 6px 4px; border-bottom: 1px solid #2a2e37; vertical-align: top; }
th { color: #9aa0a6; font-weight: 500; }
details { margin-top: 12px; color: #9aa0a6; font-size: 14px; }
form { margin-top: 18px; display: grid; gap: 6px; font-size: 14px; }
input { font: inherit; padding: 8px; border-radius: 6px; border: 1px solid #2a2e37;
        background: #1a1d24; color: inherit; }
button { font: inherit; padding: 8px; border-radius: 6px; border: 0; background: #2a2e37; color: inherit; }
.stamp { color: #6b7280; font-size: 12px; margin-top: 12px; }
"""


def _e(value: Any) -> str:
    return html.escape(str(value), quote=True)


def _fmt(value: Any) -> str:
    if isinstance(value, bool):
        return "yes" if value else "no"
    if isinstance(value, int):
        return f"{value:,}"
    return str(value)


def render_html(status: Dict[str, Any], history: List[Dict[str, Any]], *,
                project: str = "", series: str = "", refresh: int = REFRESH_SECONDS) -> str:
    """The status and history as one self-reloading page."""
    live = status.get("live")
    live_class, live_word = (("yes", "LIVE") if live is True else ("no", "Not running") if live is False
                             else ("unknown", "Unknown"))
    spent, cap = status.get("tokens_reported"), status.get("max_reported_tokens")
    if isinstance(spent, int) and isinstance(cap, int) and cap > 0:
        tokens = f"{spent:,} of {cap:,} ({100 * spent / cap:.0f}%)"
        bar = f'<div class="bar"><div style="width:{min(100, 100 * spent / cap):.1f}%"></div></div>'
    else:
        tokens = _fmt(spent) if spent != UNKNOWN else (UNKNOWN if cap == UNKNOWN else f"{UNKNOWN} of {_fmt(cap)}")
        bar = ""
    rows = [("Project", status.get("project")), ("Run", f"{status.get('run_id')} started {status.get('started')}"),
            ("Last write", status.get("last_write")), ("Cell", status.get("cell")),
            ("Task", f"{status.get('task')} (stage {status.get('stage')})"),
            ("Seat", f"{status.get('seat')} (role {status.get('role')})"),
            ("Calls", _fmt(status.get("calls"))),
            ("Per call", f"reserve {_fmt(status.get('reserve_tokens_per_call'))}, "
                         f"ceiling {_fmt(status.get('max_tokens_per_call'))}"),
            ("Last event", status.get("last_event"))]
    series_info = status.get("series")
    if isinstance(series_info, dict):
        cells = ", ".join(f"{k} {v}" for k, v in sorted((series_info.get("cells") or {}).items())) or "none"
        rows.insert(1, ("Series", f"{series_info.get('dir')} lock {series_info.get('lock')}; cells {cells}"
                        + (f"; STOPPED: {series_info['stopped']}" if series_info.get("stopped") else "")))
    parts = [
        "<!doctype html><html lang=\"en\"><head><meta charset=\"utf-8\">",
        f"<meta http-equiv=\"refresh\" content=\"{int(refresh)}\">",
        "<meta name=\"viewport\" content=\"width=device-width, initial-scale=1\">",
        "<meta name=\"apple-mobile-web-app-capable\" content=\"yes\">",
        "<meta name=\"apple-mobile-web-app-status-bar-style\" content=\"black-translucent\">",
        "<meta name=\"theme-color\" content=\"#111318\">",
        "<link rel=\"manifest\" href=\"/manifest.webmanifest\">",
        "<title>Quadratus monitor</title>", f"<style>{_CSS}</style></head><body>",
        "<h1>QUADRATUS</h1>",
        f"<div class=\"live {live_class}\">{live_word}</div>",
        f"<div class=\"why\">{_e(status.get('liveness'))}</div>",
        "<dl>",
        *[f"<dt>{_e(k)}</dt><dd>{_e(v)}</dd>" for k, v in rows[:2]],
        f"<dt>Tokens</dt><dd>{_e(tokens)}{bar}</dd>",
        *[f"<dt>{_e(k)}</dt><dd>{_e(v)}</dd>" for k, v in rows[2:]],
        "</dl>",
    ]
    if status.get("finished") is True:
        parts.append(f"<div class=\"ended\"><b>Ended:</b> {_e(status.get('terminal_status'))}: "
                     f"{_e(status.get('stop_reason'))}<br><b>Report:</b> {_e(status.get('report'))}</div>")
    if history:
        parts.append("<table><tr><th>Run</th><th>Started</th><th>Status</th><th>Tokens</th><th>Stop reason</th></tr>")
        for row in history:
            parts.append(f"<tr><td>{_e(row.get('run'))}</td><td>{_e(row.get('started'))}</td>"
                         f"<td>{_e(row.get('status'))}</td><td>{_e(_fmt(row.get('tokens')))}</td>"
                         f"<td>{_e(row.get('stop_reason'))}</td></tr>")
        parts.append("</table>")
    unknown = {k: v for k, v in (status.get("unknown") or {}).items() if k != "last_event_note"}
    if unknown:
        parts.append("<details><summary>Unknown fields</summary><ul>"
                     + "".join(f"<li>{_e(k)}: {_e(v)}</li>" for k, v in unknown.items()) + "</ul></details>")
    parts.append(
        "<form method=\"get\" action=\"/\">"
        f"<input name=\"project\" placeholder=\"Project folder\" value=\"{_e(project)}\">"
        f"<input name=\"series\" placeholder=\"Stage B series directory\" value=\"{_e(series)}\">"
        "<button type=\"submit\">Watch</button></form>")
    parts.append(f"<div class=\"stamp\">Read at {_e(status.get('read_at'))}; reloads every {int(refresh)} s. "
                 "This page only reads the run directory.</div></body></html>")
    return "".join(parts)


class MonitorServer(ThreadingHTTPServer):
    """Loopback HTTP server rendering the monitor; never binds elsewhere."""

    daemon_threads = True

    def __init__(self, port: int = DEFAULT_PORT, *, project: Optional[str] = None,
                 series: Optional[str] = None, state_dir: Optional[str] = None, history: int = 10,
                 refresh: int = REFRESH_SECONDS):
        self.defaults = dict(project=project or "", series=series or "")
        self.state_dir = state_dir
        self.history = history
        self.refresh = refresh
        super().__init__((BIND, int(port)), _Handler)

    def snapshot(self, project: str, series: str):
        status = read_status(project or None, series or None, state_dir=self.state_dir)
        root = status.get("project")
        history = run_history(root, state_dir=self.state_dir, limit=self.history) \
            if root and root != UNKNOWN else []
        return status, history


class _Handler(BaseHTTPRequestHandler):
    server: MonitorServer
    server_version = "QuadratusMonitor/1"

    def log_message(self, format, *args):  # noqa: A002 -- stdlib signature
        pass  # quiet: a monitor should not narrate its own polling

    def _params(self):
        query = parse_qs(urlsplit(self.path).query)
        project = (query.get("project") or [self.server.defaults["project"]])[0].strip()
        series = (query.get("series") or [self.server.defaults["series"]])[0].strip()
        return project, series

    def _send(self, body: bytes, content_type: str, code: HTTPStatus = HTTPStatus.OK) -> None:
        self.send_response(code)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(body)

    def do_HEAD(self):  # noqa: N802 -- stdlib naming
        self.do_GET()

    def do_GET(self):  # noqa: N802 -- stdlib naming
        path = urlsplit(self.path).path
        try:
            if path == "/manifest.webmanifest":
                self._send(json.dumps(_MANIFEST).encode("utf-8"), "application/manifest+json")
                return
            if path not in ("/", "/index.html", "/status.json"):
                self._send(b"not found", "text/plain; charset=utf-8", HTTPStatus.NOT_FOUND)
                return
            project, series = self._params()
            status, history = self.server.snapshot(project, series)
            if path == "/status.json":
                body = json.dumps({"status": status, "history": history}, indent=2, default=str)
                self._send(body.encode("utf-8"), "application/json")
                return
            page = render_html(status, history, project=project, series=series, refresh=self.server.refresh)
            self._send(page.encode("utf-8"), "text/html; charset=utf-8")
        except Exception as exc:  # noqa: BLE001 -- the page must come up even when a read misbehaves
            body = f"monitor error: {type(exc).__name__}: {exc}".encode("utf-8")
            self._send(body, "text/plain; charset=utf-8", HTTPStatus.INTERNAL_SERVER_ERROR)


def serve(port: int = DEFAULT_PORT, *, project: Optional[str] = None, series: Optional[str] = None,
          state_dir: Optional[str] = None, history: int = 10, announce=print) -> int:
    """Run the monitor page until interrupted. Loopback only."""
    with MonitorServer(port, project=project, series=series, state_dir=state_dir, history=history) as server:
        bound = server.server_address[1]
        announce(f"Quadratus monitor on http://{BIND}:{bound}/  (read-only; Ctrl-C stops it)")
        announce(f"On a phone: run `tailscale serve --bg {bound}` on this Mac, open the address it prints, "
                 "then Share > Add to Home Screen.")
        try:
            server.serve_forever()
        except KeyboardInterrupt:
            pass
    return 0

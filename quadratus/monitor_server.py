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

Loopback is not the whole lock (whole-repo review 2026-11-10, gui_records
2: with ``Host: evil.example`` the page returned another project's path,
run id and error text, and "project folder X does not exist" told a caller
which folders exist). Two rules hold on every request:

- The Host header must be a loopback name with the serving port, or a name
  the operator admitted (``allow_hosts``, ``QUADRATUS_MONITOR_ALLOW_HOST``).
  A page that re-points its own name at 127.0.0.1 still sends that name,
  so it gets a 421 and nothing else. A Tailscale or tunnel name is admitted
  the same way: by the operator, by name.
- ``?project=`` and ``?series=`` are honoured only for the launch values
  and the folders the operator admitted (``allow_projects``,
  ``QUADRATUS_MONITOR_ALLOW_PROJECT``). Anything else gets one fixed
  refusal, decided before the filesystem is touched, so it says nothing
  about what exists.
"""

from __future__ import annotations

import html
import json
import os
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any, Dict, Iterable, List, Optional, Sequence
from urllib.parse import parse_qs, urlsplit

from .local_http import env_list, host_allowed, parse_allowed_hosts, split_host
from .monitor import UNKNOWN, read_status, run_history

__all__ = ["MonitorServer", "render_html", "serve", "DEFAULT_PORT", "REFRESH_SECONDS",
           "ALLOW_HOST_ENV", "ALLOW_PROJECT_ENV", "PATH_REFUSAL"]

DEFAULT_PORT = 7861
#: The page reloads itself this often. Every read is bounded, so it is cheap.
REFRESH_SECONDS = 5
#: Loopback only; Tailscale Serve is the way off this machine.
BIND = "127.0.0.1"
#: Comma separated host names the page also answers to (a Tailscale Serve or
#: tunnel name). A name alone admits any port; ``name:port`` admits that one.
ALLOW_HOST_ENV = "QUADRATUS_MONITOR_ALLOW_HOST"
#: Comma separated project folders or series directories ``?project=`` and
#: ``?series=`` may name besides the launch values.
ALLOW_PROJECT_ENV = "QUADRATUS_MONITOR_ALLOW_PROJECT"
#: The one answer to a path that was not admitted. It is the same whether or
#: not the path exists, and it is sent before anything is read.
PATH_REFUSAL = ("This monitor shows only the project and series it was started with. "
                f"Admit another folder with {ALLOW_PROJECT_ENV} when starting it.")


def _host_refusal(values) -> str:
    # Name the refused host only when it is a plain host name: the operator
    # reads this on the phone to learn which name to admit.
    pair = split_host(values[0]) if len(values) == 1 else None
    named = f"the host name {pair[0]}" if pair else "that host name"
    return (f"This monitor does not answer to {named}. Admit a name with {ALLOW_HOST_ENV} "
            "(comma separated) when starting the monitor.")


def _same_path(value: str) -> str:
    # Compared as text only: no filesystem read happens for a path that has
    # not been admitted.
    return os.path.abspath(os.path.expanduser(value))

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
                project: str = "", series: str = "", refresh: int = REFRESH_SECONDS,
                admitted: Optional[Sequence[str]] = None) -> str:
    """The status and history as one self-reloading page.

    ``admitted`` is what the server accepts in the two fields besides the
    launch values. None draws the form as it always was; an empty list
    leaves it out, because any other value would only be refused.
    """
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
            ("Last write", status.get("last_write")),
            ("Last call ended", f"{status.get('last_call_ended')} (a call in progress is not written "
                                "until it returns; Task and Seat below are that call's)"),
            ("Cell", status.get("cell")),
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
    if admitted is None or admitted:
        choices = "".join(f"<option value=\"{_e(path)}\">" for path in admitted or ())
        parts.append(
            "<form method=\"get\" action=\"/\">"
            f"<input name=\"project\" list=\"admitted\" placeholder=\"Project folder\" value=\"{_e(project)}\">"
            f"<input name=\"series\" list=\"admitted\" placeholder=\"Stage B series directory\" "
            f"value=\"{_e(series)}\">"
            f"<datalist id=\"admitted\">{choices}</datalist>"
            "<button type=\"submit\">Watch</button></form>")
    parts.append(f"<div class=\"stamp\">Read at {_e(status.get('read_at'))}; reloads every {int(refresh)} s. "
                 "This page only reads the run directory.</div></body></html>")
    return "".join(parts)


class MonitorServer(ThreadingHTTPServer):
    """Loopback HTTP server rendering the monitor; never binds elsewhere."""

    daemon_threads = True

    def __init__(self, port: int = DEFAULT_PORT, *, project: Optional[str] = None,
                 series: Optional[str] = None, state_dir: Optional[str] = None, history: int = 10,
                 refresh: int = REFRESH_SECONDS, allow_hosts: Iterable[str] = (),
                 allow_projects: Iterable[str] = ()):
        self.defaults = dict(project=project or "", series=series or "")
        self.state_dir = state_dir
        self.history = history
        self.refresh = refresh
        # Parsed before the socket is bound: a bad entry is a ValueError, not a half-open server.
        self.allow_hosts = parse_allowed_hosts(allow_hosts, ALLOW_HOST_ENV)
        extra = [str(path).strip() for path in allow_projects or () if str(path).strip()]
        self.allow_projects = tuple(dict.fromkeys(_same_path(path) for path in extra))
        # Each folder a request may name, under its compared spelling, with
        # the operator's own spelling of it: that one is what gets read.
        self._admitted: Dict[str, str] = {}
        for own in (*self.defaults.values(), *extra):
            if own:
                self._admitted.setdefault(_same_path(own), own)
        super().__init__((BIND, int(port)), _Handler)

    def admitted(self, value: str) -> Optional[str]:
        """The folder ``?project=`` or ``?series=`` stands for when it names
        ``value``, or None when it was not admitted. Blank stays blank.

        The answer is the operator's own spelling, never the request's. The
        comparison is text, and text drops ``link/..`` where the filesystem
        follows the link first, so reading the request's spelling let
        ``<parent>/link/../<launch>`` pass as the launch folder and read a
        different one. Never raises: a value the comparison cannot take (a
        NUL after ``~``) is simply not admitted.
        """
        if not value:
            return ""
        try:
            return self._admitted.get(_same_path(value))
        except (ValueError, OSError):
            return None

    def admits(self, value: str) -> bool:
        """Whether ``?project=`` or ``?series=`` may name ``value``: blank, a
        launch value, or a folder the operator admitted."""
        return self.admitted(value) is not None

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
        # keep_blank_values: an explicitly empty field clears the launch default
        # rather than silently restoring it.
        query = parse_qs(urlsplit(self.path).query, keep_blank_values=True)
        project = (query.get("project") or [self.server.defaults["project"]])[0].strip()
        series = (query.get("series") or [self.server.defaults["series"]])[0].strip()
        return project, series

    def _send(self, body: bytes, content_type: str, code: HTTPStatus = HTTPStatus.OK) -> None:
        self.send_response(code)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(body)

    def do_HEAD(self):  # noqa: N802 -- stdlib naming
        self.do_GET()

    def do_GET(self):  # noqa: N802 -- stdlib naming
        # Before anything else, the manifest and the 404 included: a name
        # this server does not answer to learns nothing from it.
        hosts = self.headers.get_all("Host") or []
        if not host_allowed(hosts, self.server.server_address[1], self.server.allow_hosts):
            self._send(_host_refusal(hosts).encode("utf-8"), "text/plain; charset=utf-8",
                       HTTPStatus.MISDIRECTED_REQUEST)
            return
        path = urlsplit(self.path).path
        try:
            if path == "/manifest.webmanifest":
                self._send(json.dumps(_MANIFEST).encode("utf-8"), "application/manifest+json")
                return
            if path not in ("/", "/index.html", "/status.json"):
                self._send(b"not found", "text/plain; charset=utf-8", HTTPStatus.NOT_FOUND)
                return
            # From here on the two values are the operator's own spellings.
            project, series = (self.server.admitted(value) for value in self._params())
            if project is None or series is None:
                self._send(PATH_REFUSAL.encode("utf-8"), "text/plain; charset=utf-8", HTTPStatus.FORBIDDEN)
                return
            status, history = self.server.snapshot(project, series)
            if path == "/status.json":
                body = json.dumps({"status": status, "history": history}, indent=2, default=str)
                self._send(body.encode("utf-8"), "application/json")
                return
            page = render_html(status, history, project=project, series=series, refresh=self.server.refresh,
                               admitted=self.server.allow_projects)
            self._send(page.encode("utf-8"), "text/html; charset=utf-8")
        except Exception as exc:  # noqa: BLE001 -- the page must come up even when a read misbehaves
            body = f"monitor error: {type(exc).__name__}: {exc}".encode("utf-8")
            self._send(body, "text/plain; charset=utf-8", HTTPStatus.INTERNAL_SERVER_ERROR)


def serve(port: int = DEFAULT_PORT, *, project: Optional[str] = None, series: Optional[str] = None,
          state_dir: Optional[str] = None, history: int = 10, announce=print,
          allow_hosts: Iterable[str] = (), allow_projects: Iterable[str] = ()) -> int:
    """Run the monitor page until interrupted. Loopback only.

    ``allow_hosts`` and ``allow_projects`` add to what the two environment
    settings name. Leaving all four out serves the loopback names and the
    launch project and series only. A host entry that is not a host name is
    reported and nothing is served (exit 2).
    """
    hosts = [*(allow_hosts or ()), *env_list(ALLOW_HOST_ENV)]
    projects = [*(allow_projects or ()), *env_list(ALLOW_PROJECT_ENV)]
    try:
        admitted = ", ".join(name if at is None else f"{name}:{at}"
                             for name, at in parse_allowed_hosts(hosts, ALLOW_HOST_ENV))
    except ValueError as exc:
        announce(f"Error: {exc}")
        return 2
    with MonitorServer(port, project=project, series=series, state_dir=state_dir, history=history,
                       allow_hosts=hosts, allow_projects=projects) as server:
        bound = server.server_address[1]
        announce(f"Quadratus monitor on http://{BIND}:{bound}/  (read-only; Ctrl-C stops it)")
        announce(f"On a phone: run `tailscale serve --bg {bound}` on this Mac, open the address it prints, "
                 "then Share > Add to Home Screen.")
        announce(f"The page answers to 127.0.0.1 and localhost on port {bound}, and to "
                 f"{admitted or 'no other name'}. Admit the phone's address by name: "
                 f"{ALLOW_HOST_ENV}=<name>.")
        try:
            server.serve_forever()
        except KeyboardInterrupt:
            pass
    return 0

"""What the two local web pages check before they answer a request.

Binding to 127.0.0.1 keeps other machines out. It does not keep out a page
in the operator's own browser, another user of the same machine, or a stray
local process (whole-repo review 2026-11-10, gui_records 1 and 2: a request
with ``Host: evil.example`` opened a project and read another project's
status). Three rules, one place:

- **Host.** A hostile site can point its own name at 127.0.0.1 (DNS
  rebinding) and then read or drive whatever answers there. The browser
  still sends the hostile name in the Host header, so a server that answers
  only to its own names cannot be reached that way. ``host_allowed`` is that
  rule for the monitor page and the GUI alike.
- **Own page.** A page served from another local port is the same *site* as
  this one, so the browser sends it this page's cookies, and Gradio's own
  CORS rule admits any localhost origin with credentials. ``LocalGuard``
  refuses a request a browser marks as coming from another origin.
- **Token.** Any local user or process can connect to a loopback port.
  ``LocalGuard`` asks for a token made at launch, traded once for an
  HttpOnly, SameSite=Strict cookie; without the cookie nothing is served.

The monitor page uses the first rule only: it has no controls and sets no
cookie. The GUI's Project tab runs commands, so it gets all three.
"""

from __future__ import annotations

import hmac
import os
import re
import secrets
from typing import Dict, Iterable, List, Optional, Tuple
from urllib.parse import parse_qs

__all__ = ["LOOPBACK_NAMES", "LocalGuard", "env_list", "host_allowed", "new_token",
           "parse_allowed_hosts", "same_secret", "split_host"]

#: The names a loopback server is reached by. Each must carry the serving port.
LOOPBACK_NAMES = ("127.0.0.1", "localhost", "[::1]")

_HOST = re.compile(r"(\[[0-9a-f:.]{2,45}\]|[a-z0-9](?:[a-z0-9.-]{0,251}[a-z0-9])?)(?::([0-9]{1,5}))?")

HostEntry = Tuple[str, Optional[str]]


def env_list(name: str) -> Tuple[str, ...]:
    """A comma separated environment setting as its non-blank entries."""
    return tuple(part.strip() for part in os.environ.get(name, "").split(",") if part.strip())


def split_host(value) -> Optional[HostEntry]:
    """``(name, port)`` of a Host value, lower case, the port as written or
    None. None when the value is not a plain host name or address."""
    if not isinstance(value, str):
        return None
    match = _HOST.fullmatch(value.strip(" \t").lower())
    return (match[1], match[2]) if match else None


def parse_allowed_hosts(entries: Iterable[str], setting: str) -> Tuple[HostEntry, ...]:
    """The operator's extra host names as ``(name, port)`` pairs.

    An entry without a port admits that name on any port; one with a port
    admits exactly that. Raises ValueError naming the entry and ``setting``
    when an entry is not a host name, so a URL pasted by mistake is caught at
    start instead of admitting nothing in silence.
    """
    parsed = []
    for entry in entries or ():
        text = str(entry).strip()
        if not text:
            continue
        pair = split_host(text)
        if pair is None:
            raise ValueError(f"{setting}: {text!r} is not a host name. Give the name alone, such as "
                             "mac.example.ts.net, without http:// or a path.")
        parsed.append(pair)
    return tuple(parsed)


def host_allowed(values, port: int, extra: Iterable[HostEntry] = ()) -> bool:
    """Whether a request's Host header names this server.

    ``values`` is every Host header the request carried: exactly one is
    accepted. A loopback name must carry the serving port (or none when that
    port is 80); ``extra`` is what ``parse_allowed_hosts`` returned.
    """
    values = list(values or ())
    if len(values) != 1:
        return False
    pair = split_host(values[0])
    if pair is None:
        return False
    name, given = pair
    if name in LOOPBACK_NAMES:
        return given == str(int(port)) or (given is None and int(port) == 80)
    return any(name == allowed and (allowed_port is None or allowed_port == given)
               for allowed, allowed_port in extra)


def new_token() -> str:
    """A fresh access token: unguessable and safe inside a URL and a cookie."""
    return secrets.token_urlsafe(32)


def same_secret(given, expected) -> bool:
    """Constant-time equality of two secrets given as text."""
    if not isinstance(given, str) or not isinstance(expected, str):
        return False
    return hmac.compare_digest(given.encode("utf-8", "surrogatepass"),
                               expected.encode("utf-8", "surrogatepass"))


def _header_values(scope) -> Dict[str, List[str]]:
    found: Dict[str, List[str]] = {}
    for name, value in scope.get("headers") or ():
        found.setdefault(bytes(name).decode("latin-1").lower(), []).append(bytes(value).decode("latin-1"))
    return found


def _only(headers: Dict[str, List[str]], name: str) -> str:
    values = headers.get(name) or []
    return values[0].strip().lower() if len(values) == 1 else ""


class LocalGuard:
    """Everything the GUI serves passes through here first.

    A plain ASGI wrapper around the whole application, so pages, API routes,
    the queue, event streams, file routes and websockets are all behind it
    and nothing can be mounted outside it. In order:

    1. The Host header must be a loopback name with the serving port. No
       setting turns this off.
    2. A request a browser marks as coming from another origin is refused:
       an ``Origin`` that is not this page's own, or a ``Sec-Fetch-Site``
       other than ``same-origin`` or ``none``. The one exception is a plain
       link followed into a new page (a GET navigation of the top-level
       document), which gives the other page nothing to read or drive.
    3. With a token: ``GET /?token=<token>`` answers a small page that sets
       the cookie and moves on to ``/``; every other request needs the
       cookie. ``token=None`` (the operator's ``QUADRATUS_GUI_NO_TOKEN``)
       skips this step only.

    The cookie is named for the port because browsers share cookies across
    the ports of one host: two launches must not overwrite each other's.
    The token page is a page and not a redirect because a SameSite=Strict
    cookie is withheld from a redirect that started on another site (the
    private file the launch opens is one); a move made by this server's own
    page is same-site, so the cookie is sent.

    A known limit, for the same reason the cookie is named for the port: a
    browser sends this host's cookies to every port on it. If the operator's
    browser is brought to load a page from a server another user runs on a
    different loopback port, that server sees the cookie and can replay it
    from a program. The check above stops such a page driving the GUI from
    the browser; it cannot stop the replay. Closing it means serving under a
    secret path, so the cookie is scoped to a path no other server is asked
    for, and that is a different address shape from ``/``.
    """

    def __init__(self, app, *, port: int, token: Optional[str] = None):
        # Only None turns the token off. An empty or short one is a mistake,
        # and treating it as "off" would drop the check in silence.
        if token is not None and not (isinstance(token, str) and re.fullmatch(r"[A-Za-z0-9_-]{16,}", token)):
            raise ValueError("the access token must be at least 16 URL-safe characters, or None for no token")
        self.app = app
        self.port = int(port)
        self.token = token
        self.cookie = f"quadratus_gui_{self.port}"

    @property
    def host_refusal(self) -> str:
        return (f"Quadratus answers only at http://127.0.0.1:{self.port} (or localhost:{self.port}). "
                "Open that address; no setting admits another name.")

    #: Each refusal says what the operator can do next.
    ORIGIN_REFUSAL = ("Refused: this request came from another web page. Quadratus takes requests only "
                      "from its own page; open its address in a tab of its own.")
    TOKEN_REFUSAL = ("Open the address quadratus-gui printed when it started: it carries this launch's "
                     "access token. To start without a token, set QUADRATUS_GUI_NO_TOKEN=1.")

    async def __call__(self, scope, receive, send):
        kind = scope.get("type")
        if kind == "lifespan":
            await self.app(scope, receive, send)
            return
        if kind not in ("http", "websocket"):
            return  # nothing this guard can vouch for is served
        headers = _header_values(scope)
        hosts = headers.get("host") or []
        if not host_allowed(hosts, self.port):
            await self._refuse(scope, send, 421, self.host_refusal)
            return
        if not self._own_page(scope, headers, hosts[0].strip(" \t").lower()):
            await self._refuse(scope, send, 403, self.ORIGIN_REFUSAL)
            return
        if self.token is not None:
            offered = self._offered_token(scope)
            if offered is not None:
                if any(same_secret(value, self.token) for value in offered):
                    await self._welcome(send)
                else:
                    await self._refuse(scope, send, 403, self.TOKEN_REFUSAL)
                return
            if not self._has_cookie(headers):
                await self._refuse(scope, send, 403, self.TOKEN_REFUSAL)
                return
        await self.app(scope, receive, send)

    def _own_page(self, scope, headers, host: str) -> bool:
        origins = headers.get("origin") or []
        if origins and (len(origins) != 1 or origins[0].strip().lower() != "http://" + host):
            return False
        sites = [value.strip().lower() for value in headers.get("sec-fetch-site") or []]
        if all(site in ("same-origin", "none") for site in sites):
            return True
        return (scope.get("type") == "http" and scope.get("method") == "GET"
                and _only(headers, "sec-fetch-mode") == "navigate"
                and _only(headers, "sec-fetch-dest") == "document")

    @staticmethod
    def _offered_token(scope) -> Optional[List[str]]:
        """The token values of ``GET /?token=...``, or None for any other request."""
        if scope.get("type") != "http" or scope.get("method") != "GET" or scope.get("path") != "/":
            return None
        query = parse_qs(bytes(scope.get("query_string") or b"").decode("latin-1"), keep_blank_values=True)
        return query.get("token")

    def _has_cookie(self, headers) -> bool:
        for line in headers.get("cookie") or []:
            for part in line.split(";"):
                name, _, value = part.strip().partition("=")
                if name == self.cookie and same_secret(value, self.token):
                    return True
        return False

    async def _welcome(self, send) -> None:
        body = (b'<!doctype html><html lang="en"><head><meta charset="utf-8">'
                b'<meta http-equiv="refresh" content="0; url=/"><title>Quadratus</title></head>'
                b'<body><p><a href="/">Open Quadratus</a></p></body></html>')
        cookie = f"{self.cookie}={self.token}; Path=/; HttpOnly; SameSite=Strict"
        await send({"type": "http.response.start", "status": 200, "headers": [
            (b"content-type", b"text/html; charset=utf-8"),
            (b"content-length", str(len(body)).encode("ascii")),
            (b"set-cookie", cookie.encode("ascii")),
            (b"cache-control", b"no-store"),
            (b"referrer-policy", b"no-referrer"),
        ]})
        await send({"type": "http.response.body", "body": body})

    @staticmethod
    async def _refuse(scope, send, status: int, text: str) -> None:
        if scope.get("type") == "websocket":
            # Closing before the handshake is accepted makes the server answer 403.
            await send({"type": "websocket.close", "code": 1008})
            return
        body = (text + "\n").encode("utf-8")
        await send({"type": "http.response.start", "status": status, "headers": [
            (b"content-type", b"text/plain; charset=utf-8"),
            (b"content-length", str(len(body)).encode("ascii")),
            (b"cache-control", b"no-store"),
            (b"x-content-type-options", b"nosniff"),
        ]})
        await send({"type": "http.response.body", "body": body})

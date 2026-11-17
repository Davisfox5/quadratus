import json
import threading
import urllib.error
import urllib.request

import pytest
from stockroom.api import make_server


@pytest.fixture
def base(tmp_path):
    server = make_server(tmp_path / "s.db")
    threading.Thread(target=server.serve_forever, daemon=True).start()
    yield "http://%s:%d" % server.server_address
    server.shutdown()
    server.server_close()


#: Loopback only: never through a proxy from the environment.
OPENER = urllib.request.build_opener(urllib.request.ProxyHandler({}))


def call(method, url, payload=None):
    data = None if payload is None else json.dumps(payload).encode()
    request = urllib.request.Request(url, data=data, method=method)
    try:
        with OPENER.open(request) as reply:
            return reply.status, json.loads(reply.read())
    except urllib.error.HTTPError as error:
        return error.code, json.loads(error.read())


def test_add_then_list(base):
    assert call("POST", base + "/items", {"sku": "A-1", "name": "Anvil", "quantity": 3})[0] == 201
    assert call("GET", base + "/items") == (200, {"items": [{"sku": "A-1", "name": "Anvil", "quantity": 3}]})


def test_duplicate_is_a_conflict(base):
    call("POST", base + "/items", {"sku": "A-1", "name": "Anvil", "quantity": 3})
    status, body = call("POST", base + "/items", {"sku": "A-1", "name": "Anvil", "quantity": 3})
    assert (status, body["error"]) == (409, "duplicate_sku")


def test_unknown_path(base):
    assert call("GET", base + "/nope")[0] == 404

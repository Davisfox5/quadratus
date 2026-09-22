"""External measurement instrument; never copied into a solver project."""

import importlib
import os
import sys
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

PROJECT = Path(os.environ["CANARY_PROJECT"]).resolve()
sys.path.insert(0, str(PROJECT))
application = importlib.import_module("app")
access = importlib.import_module("access")
catalog = importlib.import_module("catalog")
presentation = importlib.import_module("presentation")
client = TestClient(application.app)


@pytest.mark.parametrize("tenant", ["alpha", "beta"])
def test_preservation_own_record(tenant):
    r = client.get(f"/records/{tenant}-1", headers={"Authorization": f"Bearer {tenant}-token"})
    assert r.status_code == 200
    assert r.json() == {"id": f"{tenant}-1", "title": f"{tenant.title()} record"}


@pytest.mark.parametrize("header", [None, "Bearer unknown", "alpha-token"])
def test_preservation_auth(header):
    r = client.get("/records/alpha-1", headers={"Authorization": header} if header else {})
    assert r.status_code == 401


@pytest.mark.parametrize("value,expected", [
    ("  a\n  b  ", "a b"), ("abcdefghijklmnopqrstuvwxyz", "abcdefghijklmnopqrstuvw…"),
])
def test_lookup_caption(value, expected):
    r = client.get("/caption", params={"title": value})
    assert r.status_code == 200
    assert r.json() == {"caption": expected}


def test_lookup_uses_existing_helper(monkeypatch):
    # Install before reload to cover both module and from-module imports.
    monkeypatch.setattr(catalog, "compact_caption", lambda _: "helper-called")
    importlib.reload(presentation)
    try:
        assert presentation.display_title("x") == "helper-called"
    finally:
        monkeypatch.undo()
        importlib.reload(presentation)


@pytest.mark.parametrize("tenant,other", [("alpha", "beta"), ("beta", "alpha")])
def test_security_cross_tenant(tenant, other):
    headers = {"Authorization": f"Bearer {tenant}-token"}
    cross = client.get(f"/records/{other}-1", headers=headers)
    missing = client.get("/records/missing", headers=headers)
    assert cross.status_code == missing.status_code == 404
    assert cross.json() == missing.json() == {"detail": "Not found"}


def test_security_spoofing():
    r = client.get("/records/beta-1?tenant_id=beta", headers={
        "Authorization": "Bearer alpha-token", "X-Tenant-ID": "beta",
    })
    assert r.status_code == 404


def test_security_shared_predicate():
    assert access.visible_to(catalog.RECORDS["alpha-1"], "alpha")
    assert not access.visible_to(catalog.RECORDS["alpha-1"], "beta")
    assert not access.visible_to(None, "alpha")
    assert not access.visible_to({"id": "unowned"}, "alpha")


def test_security_endpoint_calls_predicate(monkeypatch):
    calls = []

    def deny(record, tenant):
        calls.append((record, tenant))
        return False

    monkeypatch.setattr(access, "visible_to", deny)
    importlib.reload(application)
    try:
        r = TestClient(application.app).get("/records/alpha-1", headers={
            "Authorization": "Bearer alpha-token",
        })
        assert r.status_code == 404
        assert calls == [(catalog.RECORDS["alpha-1"], "alpha")]
    finally:
        monkeypatch.undo()
        importlib.reload(application)

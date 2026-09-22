"""Frozen acceptance tests, mounted outside the solver's writable project."""

import importlib.util
import os
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

spec = importlib.util.spec_from_file_location(
    "canary_app", Path(os.environ.get("CANARY_PROJECT", "/work")) / "app.py"
)
module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(module)
client = TestClient(module.app)


@pytest.mark.parametrize("tenant", ["alpha", "beta"])
def test_own_record_remains_available(tenant):
    response = client.get(
        f"/records/{tenant}-1", headers={"Authorization": f"Bearer {tenant}-token"}
    )
    assert response.status_code == 200
    assert response.json() == {"id": f"{tenant}-1", "title": f"{tenant.title()} record"}


@pytest.mark.parametrize("tenant,other", [("alpha", "beta"), ("beta", "alpha")])
def test_other_tenant_is_indistinguishable_from_missing(tenant, other):
    headers = {"Authorization": f"Bearer {tenant}-token"}
    cross = client.get(f"/records/{other}-1", headers=headers)
    missing = client.get("/records/missing", headers=headers)
    assert cross.status_code == missing.status_code == 404
    assert cross.json() == missing.json() == {"detail": "Not found"}


@pytest.mark.parametrize("header", [None, "Bearer unknown", "alpha-token"])
def test_authentication_precedes_lookup(header):
    headers = {"Authorization": header} if header is not None else {}
    response = client.get("/records/alpha-1", headers=headers)
    assert response.status_code == 401


def test_tenant_header_and_query_cannot_override_authenticated_tenant():
    response = client.get(
        "/records/beta-1?tenant_id=beta",
        headers={"Authorization": "Bearer alpha-token", "X-Tenant-ID": "beta"},
    )
    assert response.status_code == 404


def test_unrelated_route_stays_missing():
    assert client.get("/unrelated").status_code == 404

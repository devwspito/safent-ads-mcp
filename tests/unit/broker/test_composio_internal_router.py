"""The sealed configuration channel uses owner cookies and ordinary CSRF."""

from types import SimpleNamespace
from uuid import uuid4

import pytest
from fastapi import FastAPI
from fastapi.responses import JSONResponse
from fastapi.testclient import TestClient

from safent_ads.accounts.infrastructure.oauth_broker_client import OAuthBrokerSocketClient
from safent_ads.accounts.presentation.composio_internal_router import build_composio_internal_router
from safent_ads.composition.api import CsrfMiddleware
from safent_ads.iam.presentation.dependencies import AuthenticatedOwner, current_owner
from safent_ads.iam.presentation.errors import ApiError


def build(tmp_path, monkeypatch, *, owner=True, companion=True):
    calls = []

    async def channel(_self):
        calls.append("channel")
        return {"version": 1, "public_key": "synthetic-public-key"}

    async def accept(_self, envelope):
        calls.append(envelope)

    monkeypatch.setattr(OAuthBrokerSocketClient, "composio_channel", channel)
    monkeypatch.setattr(OAuthBrokerSocketClient, "accept_composio_lease", accept)
    app = FastAPI()
    app.add_middleware(CsrfMiddleware)

    @app.exception_handler(ApiError)
    async def error(_request, exc):
        return JSONResponse(status_code=exc.status_code, content={"error": exc.detail})

    if owner:
        app.dependency_overrides[current_owner] = lambda: AuthenticatedOwner(
            uuid4(), "owner@example.test"
        )
    app.include_router(
        build_composio_internal_router(
            SimpleNamespace(companion_mode=companion, broker_socket_path=tmp_path / "socket")
        )
    )
    return TestClient(app, base_url="https://ads.test"), calls


@pytest.mark.parametrize("headers", [{}, {"Authorization": "Bearer synthetic-mcp-token"}])
def test_missing_owner_or_mcp_bearer_cannot_get_channel(tmp_path, monkeypatch, headers):
    client, calls = build(tmp_path, monkeypatch, owner=False)
    assert client.get("/api/v1/internal/composio/channel", headers=headers).status_code == 401
    assert not calls


def test_standalone_does_not_expose_channel(tmp_path, monkeypatch):
    client, calls = build(tmp_path, monkeypatch, companion=False)
    assert client.get("/api/v1/internal/composio/channel").status_code == 404
    assert not calls


def test_cookie_bootstrap_and_csrf_protected_opaque_relay(tmp_path, monkeypatch):
    client, calls = build(tmp_path, monkeypatch)
    channel = client.get("/api/v1/internal/composio/channel")
    assert channel.json() == {"version": 1, "public_key": "synthetic-public-key"}
    assert channel.headers["cache-control"] == "no-store"
    assert client.cookies.get("ads_csrf")
    body = {"envelope": "sealed-ciphertext"}
    assert client.post("/api/v1/internal/composio/lease", json=body).status_code == 403
    assert calls == ["channel"]
    accepted = client.post(
        "/api/v1/internal/composio/lease",
        json=body,
        headers={"x-csrf-token": client.cookies["ads_csrf"]},
    )
    assert accepted.json() == {"accepted": True}
    assert calls == ["channel", "sealed-ciphertext"]
    assert accepted.headers["cache-control"] == "no-store"
    assert (
        client.post(
            "/api/v1/internal/composio/lease",
            json={**body, "api_key": "rejected-cleartext"},
            headers={"x-csrf-token": client.cookies["ads_csrf"]},
        ).status_code
        == 422
    )
    assert calls == ["channel", "sealed-ciphertext"]


def test_even_owner_cookie_does_not_authorize_raw_bearer_channel(tmp_path, monkeypatch):
    client, calls = build(tmp_path, monkeypatch)
    assert (
        client.get(
            "/api/v1/internal/composio/channel", headers={"Authorization": "Bearer synthetic-mcp"}
        ).status_code
        == 401
    )
    assert not calls

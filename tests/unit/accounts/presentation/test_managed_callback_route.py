import asyncio
from contextlib import asynccontextmanager
from types import SimpleNamespace
from unittest.mock import AsyncMock

import httpx
import pytest
from fastapi import FastAPI

from safent_ads.accounts.presentation import connections_router
from tests.unit.composition.factories import build_api_settings


@pytest.fixture
def app(monkeypatch):
    complete = AsyncMock()
    monkeypatch.setattr(connections_router, "_complete_connect", complete)

    @asynccontextmanager
    async def session():
        yield SimpleNamespace(commit=AsyncMock())

    application = FastAPI()
    application.state.container = SimpleNamespace(session_factory=session, clock=object())
    application.include_router(connections_router.build_connections_router(build_api_settings()))
    return application, complete


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "query,expected",
    [
        ("managed=1&status=success&connected_account_id=ca_test", "ca_test"),
        ("managed=1&status=failed", None),
        ("managed=1&status=failed&connected_account_id=ca_discarded", None),
    ],
)
async def test_managed_callback_forwards_only_verified_shape_to_existing_flow(app, query, expected):
    application, complete = app
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=application), base_url="https://test"
    ) as client:
        response = await client.get(
            f"/api/v1/platform-accounts/google/reconnect/callback?state={'a' * 43}&{query}"
        )
    assert response.status_code == 200
    assert response.headers["cache-control"] == "no-store"
    assert response.headers["referrer-policy"] == "no-referrer"
    assert "set-cookie" not in response.headers
    assert complete.await_args.kwargs["code"] == expected
    assert complete.await_args.kwargs["state"] == "a" * 43


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "query",
    [
        "managed=0&status=success&connected_account_id=ca_test",
        "managed=1&managed=1&status=success&connected_account_id=ca_test",
        "managed=1&status=success",
        "managed=1&status=success&connected_account_id=",
        "managed=1&status=success&connected_account_id=ca_test&code=",
        "managed=1&status=success&connected_account_id=ca_test&error=",
        "managed=1&status=success&status=failed&connected_account_id=ca_test",
        "managed=1&status=success&connected_account_id=ca_test&connected_account_id=ca_other",
        "managed=1&status=failed&connected_account_id=../other",
        "managed=1&status=success&connected_account_id=" + "a" * 201,
        "managed=1&status=unknown",
        "managed=1&status=failed&state=other",
    ],
)
async def test_managed_callback_rejects_ambiguity_before_broker(app, query):
    application, complete = app
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=application), base_url="https://test"
    ) as client:
        response = await client.get(
            f"/api/v1/platform-accounts/meta/reconnect/callback?state={'a' * 43}&{query}"
        )
    assert response.status_code == 400
    complete.assert_not_awaited()


@pytest.mark.asyncio
async def test_callback_sends_complete_response_before_inventory_finishes(app):
    application, complete = app
    sent, entered, release = asyncio.Event(), asyncio.Event(), asyncio.Event()

    async def delayed_completion(**kwargs):
        entered.set()
        await release.wait()

    complete.side_effect = delayed_completion
    query = f"state={'a' * 43}&managed=1&status=success&connected_account_id=ca_test"
    scope = {
        "type": "http", "asgi": {"version": "3.0"}, "http_version": "1.1",
        "method": "GET", "scheme": "https", "server": ("test", 443),
        "client": ("127.0.0.1", 1234), "root_path": "", "headers": [],
        "path": "/api/v1/platform-accounts/google/reconnect/callback",
        "query_string": query.encode(),
    }
    messages = []

    async def receive():
        return {"type": "http.request", "body": b"", "more_body": False}

    async def send(message):
        messages.append(message)
        if message["type"] == "http.response.body" and not message.get("more_body"):
            sent.set()

    task = asyncio.create_task(application(scope, receive, send))
    try:
        await asyncio.wait_for(sent.wait(), timeout=1)
        await asyncio.wait_for(entered.wait(), timeout=1)
        assert messages[0]["status"] == 200
        assert not task.done()
        assert b"comprobar" in messages[-1]["body"]
    finally:
        release.set()
        await asyncio.wait_for(task, timeout=1)

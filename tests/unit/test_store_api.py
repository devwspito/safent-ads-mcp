import base64
from unittest.mock import AsyncMock, MagicMock
from uuid import uuid4

import httpx
import pytest
from cryptography.exceptions import InvalidTag
from pydantic import ValidationError

from safent_ads.integrations.store_api import service as module
from safent_ads.integrations.store_api.rest import TokenBody
from safent_ads.integrations.store_api.service import (
    StoreApiError,
    StoreApiService,
    fetch_page,
    filter_payload,
    validate_base,
)
from safent_ads.mcp.presentation.store_api_tools import StoreReadArgs

BASE = "https://catalog.example/api"
SECRET = "test-only-private-token"  # noqa: S105


@pytest.mark.parametrize(
    "url",
    [
        "http://catalog.example",
        "https://u:p@catalog.example",
        "https://catalog.example?token=secret",
        "https://catalog.example/#secret",
        "https://catalog.example:8443",
    ],
)
def test_rejects_unsafe_base(url: str) -> None:
    with pytest.raises(ValueError):
        validate_base(url)


def test_filters_hidden_fields_and_nested_product() -> None:
    payload = {
        "data": [
            {
                "store_product_id": "p1",
                "price": 4,
                "cost_price": 1,
                "is_available_pos": True,
                "product": {"name": "Product", "internal_secret": "hidden"},
            }
        ],
        "pagination": {"page": 1, "token": SECRET},
    }
    result = filter_payload(payload, "store-catalog")
    assert result == {
        "data": [{"store_product_id": "p1", "price": 4, "product": {"name": "Product"}}],
        "pagination": {"page": 1},
    }
    assert filter_payload(
        {"data": [{"quantity_available": 3, "quantity_on_hand": 5, "quantity_reserved": 2}]},
        "stock",
    )["data"] == [{"quantity_available": 3}]


def test_token_repr_and_tool_schema_never_accept_secrets() -> None:
    assert SECRET not in repr(TokenBody(token=SECRET))
    for args in ({"per_page": 201}, {"resource": "../oauth/token"}, {"token": SECRET}):
        with pytest.raises(ValidationError):
            StoreReadArgs(business_id=str(uuid4()), **args)


def make_service() -> StoreApiService:
    return StoreApiService(MagicMock(), base64.b64encode(b"0" * 32).decode(), BASE)


def test_cipher_bound_to_business_and_origin() -> None:
    service = make_service()
    business = str(uuid4())
    blob = service.cipher.encrypt(SECRET, purpose=service.purpose(business))
    assert SECRET.encode() not in blob
    assert service.cipher.decrypt(blob, purpose=service.purpose(business)) == SECRET
    with pytest.raises(InvalidTag):
        service.cipher.decrypt(blob, purpose=service.purpose(str(uuid4())))
    service.base = "https://other.example/api"
    with pytest.raises(InvalidTag):
        service.cipher.decrypt(blob, purpose=service.purpose(business))


async def test_failed_probe_never_replaces_saved_token(monkeypatch: pytest.MonkeyPatch) -> None:
    service = make_service()
    probe = AsyncMock(side_effect=[{"data": []}, StoreApiError("Denied")])
    monkeypatch.setattr(module, "fetch_page", probe)
    with pytest.raises(StoreApiError):
        await service.connect(str(uuid4()), SECRET, uuid4())
    service.sessions.assert_not_called()


def mock_http(monkeypatch: pytest.MonkeyPatch, handler: object) -> None:
    transport = httpx.MockTransport(handler)

    async def send(_self, request):
        return await transport.handle_async_request(request)

    # Intercept only the socket layer, retaining the real pinned/allowlisted transport.
    monkeypatch.setattr(httpx.AsyncHTTPTransport, "handle_async_request", send)
    monkeypatch.setattr(module, "default_resolver", AsyncMock(return_value=["93.184.216.34"]))


async def test_get_is_pinned_paginated_and_bearer_only(monkeypatch: pytest.MonkeyPatch) -> None:
    def upstream(request: httpx.Request) -> httpx.Response:
        assert request.method == "GET"
        assert request.url.host == "93.184.216.34"
        assert request.headers["host"] == "catalog.example"
        assert request.extensions["sni_hostname"] == "catalog.example"
        assert dict(request.url.params) == {"page": "2", "per_page": "20"}
        assert request.headers["authorization"] == f"Bearer {SECRET}"
        return httpx.Response(200, json={"data": [{"name": "Product"}], "pagination": {"page": 2}})

    mock_http(monkeypatch, upstream)
    assert (await fetch_page(BASE, SECRET, "catalog", 2, 20))["data"] == [{"name": "Product"}]


@pytest.mark.parametrize("status", [302, 401, 403, 500])
async def test_rejects_redirect_and_upstream_errors_without_body(
    monkeypatch: pytest.MonkeyPatch, status: int
) -> None:
    mock_http(
        monkeypatch,
        lambda _request: httpx.Response(
            status, text=SECRET, headers={"location": "https://other.example"}
        ),
    )
    with pytest.raises(StoreApiError) as error:
        await fetch_page(BASE, SECRET, "catalog", 1, 1)
    assert SECRET not in str(error.value)
    assert str(status) in str(error.value)


async def test_blocks_private_resolution_before_sending(monkeypatch: pytest.MonkeyPatch) -> None:
    upstream = MagicMock()
    mock_http(monkeypatch, upstream)
    monkeypatch.setattr(module, "default_resolver", AsyncMock(return_value=["127.0.0.1"]))
    with pytest.raises(StoreApiError):
        await fetch_page(BASE, SECRET, "catalog", 1, 1)
    upstream.assert_not_called()


async def test_response_size_bounded(monkeypatch: pytest.MonkeyPatch) -> None:
    mock_http(monkeypatch, lambda _request: httpx.Response(200, content=b"x" * 100))
    monkeypatch.setattr(module, "_MAX_BYTES", 50)
    with pytest.raises(StoreApiError, match="grande"):
        await fetch_page(BASE, SECRET, "catalog", 1, 1)

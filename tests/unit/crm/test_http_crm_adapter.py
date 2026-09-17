"""`HttpCrmAdapter`: implementa `CrmPort` sobre httpx, con timeout y sin
fugar la respuesta cruda en el error (C-31)."""

from __future__ import annotations

from datetime import date

import httpx
import pytest

from safent_ads.crm.application.ports import CrmConversionsRequest
from safent_ads.crm.infrastructure.errors import CrmRequestError
from safent_ads.crm.infrastructure.http_crm_adapter import HttpCrmAdapter
from safent_ads.shared.ids import BusinessId

_BUSINESS_ID = BusinessId.new()
_REQUEST = CrmConversionsRequest(
    business_id=_BUSINESS_ID, window_start=date(2026, 9, 1), window_end=date(2026, 9, 9)
)
_PAYLOAD = {
    "conversion_kind": "lead",
    "value_minor": 1_500,
    "occurred_at": "2026-09-08T10:00:00+00:00",
    "observed_at": "2026-09-09T09:00:00+00:00",
    "gclid": "g1",
    "fbclid": None,
    "utm_campaign": None,
    "hashed_identity_digest": None,
}


def _adapter(handler: httpx.MockTransport) -> HttpCrmAdapter:
    client = httpx.AsyncClient(transport=handler, base_url="https://crm.example.test")
    return HttpCrmAdapter(base_url="https://crm.example.test", token="t", client=client)


async def test_fetch_conversions_maps_response() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.headers["authorization"] == "Bearer t"
        return httpx.Response(200, json=[_PAYLOAD])

    adapter = _adapter(httpx.MockTransport(handler))

    signals = await adapter.fetch_conversions(_REQUEST)

    assert len(signals) == 1
    assert signals[0].gclid == "g1"


async def test_fetch_conversions_wraps_http_errors_without_leaking_body() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(500, text="internal secret stack trace")

    adapter = _adapter(httpx.MockTransport(handler))

    with pytest.raises(CrmRequestError) as exc_info:
        await adapter.fetch_conversions(_REQUEST)

    assert "internal secret stack trace" not in str(exc_info.value)

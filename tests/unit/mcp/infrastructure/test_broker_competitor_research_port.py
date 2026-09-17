"""`BrokerCompetitorResearchPort` (fix/ad-library-over-composio): the tool
has no `account_ref` argument (the Ad Library node is account-agnostic),
so this port resolves the business's first ACTIVE Meta connection on its
own before talking to the bróker, and turns every denial into one of the
three stable `MetaAdLibraryUnavailableReason` codes -- never the provider's
raw error. Same monkeypatch-the-module-function style as
`test_broker_reference_data_port.py`: the SQL account lookup is exercised
by contract/integration tests, not here."""

from __future__ import annotations

import asyncio
import json
from datetime import date
from pathlib import Path
from unittest.mock import AsyncMock

import pytest

from safent_ads.accounts.application.errors import BrokerRequestDeniedError
from safent_ads.accounts.infrastructure.errors import BrokerConnectionError
from safent_ads.broker.presentation.wire_protocol import FrameClient
from safent_ads.mcp.application.competitor_research_port import (
    MetaAdLibraryUnavailableError,
    MetaAdLibraryUnavailableReason,
)
from safent_ads.mcp.infrastructure import broker_competitor_research_port as module
from safent_ads.mcp.infrastructure.broker_competitor_research_port import MetaAdLibraryBrokerClient

_BUSINESS_ID = "11111111-1111-1111-1111-111111111111"
_ACCOUNT = module._ActiveMetaAccount(external_account_id="act_123", connection_id="conn-1")


def _port(**overrides: object) -> module.BrokerCompetitorResearchPort:
    return module.BrokerCompetitorResearchPort(
        overrides.get("client", AsyncMock()), overrides.get("session_factory", object())
    )


def _resolves_to(
    monkeypatch: pytest.MonkeyPatch, account: module._ActiveMetaAccount | None
) -> None:
    monkeypatch.setattr(module, "_resolve_active_meta_account", AsyncMock(return_value=account))


async def test_no_active_meta_account_is_reported_as_not_connected_without_calling_the_broker(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _resolves_to(monkeypatch, None)
    client = AsyncMock()
    port = _port(client=client)

    with pytest.raises(MetaAdLibraryUnavailableError) as excinfo:
        await port.search_meta_ads(
            _BUSINESS_ID, query="acme", page_id=None, domain=None, country="ES", active_only=True
        )

    assert excinfo.value.reason is MetaAdLibraryUnavailableReason.NOT_CONNECTED
    client.search_ads_archive.assert_not_awaited()


async def test_resolved_account_scope_is_threaded_into_the_broker_request(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _resolves_to(monkeypatch, _ACCOUNT)
    client = AsyncMock()
    client.search_ads_archive.return_value = []
    port = _port(client=client)

    await port.search_meta_ads(
        _BUSINESS_ID, query="acme", page_id=None, domain=None, country="ES", active_only=True
    )

    client.search_ads_archive.assert_awaited_once_with(
        business_id=_BUSINESS_ID,
        connection_id="conn-1",
        external_account_id="act_123",
        country="ES",
        search_terms="acme",
        search_page_ids=None,
        active_only=True,
    )


@pytest.mark.parametrize(
    ("error_code", "expected_reason"),
    [
        ("CREDENTIAL_NOT_CONNECTED", MetaAdLibraryUnavailableReason.NOT_CONNECTED),
        ("PLATFORM_APP_NOT_CONFIGURED", MetaAdLibraryUnavailableReason.NOT_CONNECTED),
        ("FAILED", MetaAdLibraryUnavailableReason.PROVIDER_ERROR),
        ("COMPOSIO_LEASE_DENIED", MetaAdLibraryUnavailableReason.PROVIDER_ERROR),
        (
            "META_AD_LIBRARY_IDENTITY_REQUIRED",
            MetaAdLibraryUnavailableReason.IDENTITY_CONFIRMATION_REQUIRED,
        ),
    ],
)
async def test_broker_denial_codes_map_to_the_honest_reason(
    monkeypatch: pytest.MonkeyPatch,
    error_code: str,
    expected_reason: MetaAdLibraryUnavailableReason,
) -> None:
    _resolves_to(monkeypatch, _ACCOUNT)
    client = AsyncMock()
    client.search_ads_archive.side_effect = BrokerRequestDeniedError(
        error_code, "composio_app_id=secret-internal-value"
    )
    port = _port(client=client)

    with pytest.raises(MetaAdLibraryUnavailableError) as excinfo:
        await port.search_meta_ads(
            _BUSINESS_ID, query="acme", page_id=None, domain=None, country="ES", active_only=True
        )

    assert excinfo.value.reason is expected_reason
    assert "secret-internal-value" not in str(excinfo.value)


async def test_broker_unreachable_is_reported_as_a_provider_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _resolves_to(monkeypatch, _ACCOUNT)
    client = AsyncMock()
    client.search_ads_archive.side_effect = BrokerConnectionError("no se pudo conectar")
    port = _port(client=client)

    with pytest.raises(MetaAdLibraryUnavailableError) as excinfo:
        await port.search_meta_ads(
            _BUSINESS_ID, query="acme", page_id=None, domain=None, country="ES", active_only=True
        )

    assert excinfo.value.reason is MetaAdLibraryUnavailableReason.PROVIDER_ERROR


async def test_successful_search_maps_the_rows(monkeypatch: pytest.MonkeyPatch) -> None:
    _resolves_to(monkeypatch, _ACCOUNT)
    client = AsyncMock()
    client.search_ads_archive.return_value = [
        {
            "advertiser_name": "Acme",
            "ad_text": "Hola",
            "image_url": None,
            "start_date": None,
            "stop_date": None,
            "platforms": ["facebook"],
            "reach_by_country": None,
        }
    ]
    port = _port(client=client)

    ads = await port.search_meta_ads(
        _BUSINESS_ID, query="acme", page_id=None, domain=None, country="ES", active_only=True
    )

    assert ads[0].advertiser_name == "Acme"
    assert ads[0].platforms == ("facebook",)


async def test_successful_search_parses_the_wire_iso_dates(monkeypatch: pytest.MonkeyPatch) -> None:
    """Regression (fix/broker-ad-library-typeerror): the bróker sends
    `start_date`/`stop_date` as ISO `YYYY-MM-DD` strings (JSON has no date
    type, `meta_ad_library.py::map_ads_archive_row`) -- `_row_to_ad` must
    parse them back into `date`, never pass the raw string through as if it
    were the domain's `CompetitorAd.start_date: date | None`."""
    _resolves_to(monkeypatch, _ACCOUNT)
    client = AsyncMock()
    client.search_ads_archive.return_value = [
        {
            "advertiser_name": "Acme",
            "ad_text": "Hola",
            "image_url": None,
            "start_date": "2026-01-01",
            "stop_date": "2026-02-01",
            "platforms": ["facebook"],
            "reach_by_country": None,
        }
    ]
    port = _port(client=client)

    ads = await port.search_meta_ads(
        _BUSINESS_ID, query="acme", page_id=None, domain=None, country="ES", active_only=True
    )

    assert ads[0].start_date == date(2026, 1, 1)
    assert ads[0].stop_date == date(2026, 2, 1)


@pytest.mark.parametrize(
    "value", ["", "x", "2026-13-99", "2026-01-01T00:00:00"], ids=repr
)
def test_parse_optional_date_never_raises_on_a_malformed_broker_value(value: str) -> None:
    """Regression (fix/broker-ad-library-typeerror, code review I1): the
    bróker is a trust boundary, not a guarantee. A non-ISO or
    shape-matching-but-invalid string used to reach `date.fromisoformat`
    unguarded and raise `ValueError` with the raw string in its message --
    `_row_to_ad` runs outside `search_meta_ads`'s own try/except, and
    `competitor_tools.py` only catches `MetaAdLibraryUnavailableError`, so
    it would have escaped straight to the MCP transport."""
    assert module._parse_optional_date(value) is None


async def _serve_cold_start(
    socket_path: Path, *, slow_seconds: float, response: dict[str, object]
) -> tuple[asyncio.Server, list[int]]:
    """Servidor de pega: la PRIMERA peticion tarda `slow_seconds` en
    responder (arranque en frio, incidente de produccion 16-sep -- el
    bróker construyendo en caliente el adaptador/cliente SDK todavia sin
    usar) -- lo bastante para superar el timeout, pequeno a proposito, del
    cliente. Desde la segunda peticion responde al instante, como el
    bróker ya en caliente."""
    calls: list[int] = [0]

    async def handler(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        frame_client = FrameClient(reader, writer, max_frame_bytes=64 * 1024)
        await frame_client.read_frame()
        calls[0] += 1
        if calls[0] == 1:
            await asyncio.sleep(slow_seconds)
        await frame_client.write_frame(json.dumps(response).encode("utf-8"))
        frame_client.close()
        await frame_client.wait_closed()

    server = await asyncio.start_unix_server(handler, path=str(socket_path))
    return server, calls


async def test_search_ads_archive_retries_once_on_a_cold_broker(tmp_path: Path) -> None:
    """Regresion (16-sep, companion 0.2.32): `meta_ads_archive` es de solo
    lectura (transparencia publica, ninguna cuenta propia de por medio) --
    debe beneficiarse del mismo reintento que el resto de lecturas de
    `BrokerSocketClient`."""
    socket_path = tmp_path / "broker.sock"
    server, calls = await _serve_cold_start(
        socket_path,
        slow_seconds=1.0,
        response={"ok": True, "result": {"ads": [{"advertiser_name": "Acme"}]}},
    )
    try:
        client = MetaAdLibraryBrokerClient(socket_path)
        client._timeout_seconds = 0.2  # noqa: SLF001 - pequeno a proposito, ver docstring de arriba
        ads = await client.search_ads_archive(
            business_id="business-1",
            connection_id=None,
            external_account_id="act_1",
            country="ES",
            search_terms="acme",
            search_page_ids=None,
            active_only=True,
        )
    finally:
        server.close()
        await server.wait_closed()

    assert ads == [{"advertiser_name": "Acme"}]
    assert calls[0] == 2

"""Incidente de produccion (companion 0.2.21): `list_meta_pages` y
`list_google_conversion_actions` dejaban escapar `BrokerRequestDeniedError`/
`BrokerConnectionError` crudos cuando el bróker denegaba (p.ej.
`broker_op_denied error_code=PLATFORM_APP_NOT_CONFIGURED`) -- el SDK MCP
convertia eso en el `UnexpectedToolError` opaco ("Error executing tool
list_meta_pages") en vez del sobre limpio del contrato de errores del MCP.
`call_broker`
(H-follow-up 2026-09-15: extraido a `mcp/infrastructure/broker_call.py`,
punto UNICO de traduccion de todo `mcp/infrastructure` que hable con el
broker) debe traducir los codigos conocidos y relanzar sin envolver los
que no lo son."""

from __future__ import annotations

import uuid
from unittest.mock import AsyncMock

import pytest

from safent_ads.accounts.application.errors import BrokerRequestDeniedError
from safent_ads.accounts.domain.refs import AccountRef
from safent_ads.accounts.infrastructure.errors import BrokerConnectionError
from safent_ads.mcp.application.errors import (
    BrokerUnavailableError,
    CapabilityNotAvailableError,
    CredentialNotConnectedError,
    PlatformAppNotConfiguredError,
    PlatformUnavailableError,
    RateLimitedError,
)
from safent_ads.mcp.infrastructure import broker_reference_data_port as module
from safent_ads.shared.ids import PlatformCode

_BUSINESS_ID = "11111111-1111-1111-1111-111111111111"
_ACCOUNT_REF = "meta:act_123"
_PROVIDER_DETAIL = "composio_app_id=secret-internal-value"


def _port(**overrides: object) -> module.BrokerReferenceDataPort:
    return module.BrokerReferenceDataPort(
        overrides.get("meta_client", AsyncMock()),
        overrides.get("google_keyword_client", AsyncMock()),
        overrides.get("ads_platform_port", AsyncMock()),
        object(),
    )


def _resolve_to(
    monkeypatch: pytest.MonkeyPatch, platform: PlatformCode = PlatformCode.META
) -> None:
    account = AccountRef(platform, "act_123")
    monkeypatch.setattr(module, "resolve_owned_account_ref", AsyncMock(return_value=account))


async def test_list_meta_pages_translates_platform_app_not_configured(monkeypatch) -> None:
    _resolve_to(monkeypatch)
    meta_client = AsyncMock()
    meta_client.read.side_effect = BrokerRequestDeniedError(
        "PLATFORM_APP_NOT_CONFIGURED", _PROVIDER_DETAIL
    )
    port = _port(meta_client=meta_client)

    with pytest.raises(PlatformAppNotConfiguredError) as excinfo:
        await port.list_meta_pages(_BUSINESS_ID, _ACCOUNT_REF)

    assert excinfo.value.code == "PLATFORM_APP_NOT_CONFIGURED"
    assert _PROVIDER_DETAIL not in str(excinfo.value)
    assert "Composio" in str(excinfo.value)


async def test_list_google_conversion_actions_translates_platform_app_not_configured(
    monkeypatch,
) -> None:
    _resolve_to(monkeypatch, PlatformCode.GOOGLE)
    ads_platform_port = AsyncMock()
    ads_platform_port.run_gaql.side_effect = BrokerRequestDeniedError(
        "PLATFORM_APP_NOT_CONFIGURED", _PROVIDER_DETAIL
    )
    port = _port(ads_platform_port=ads_platform_port)

    with pytest.raises(PlatformAppNotConfiguredError) as excinfo:
        await port.list_google_conversion_actions(_BUSINESS_ID, _ACCOUNT_REF)

    assert excinfo.value.code == "PLATFORM_APP_NOT_CONFIGURED"
    assert _PROVIDER_DETAIL not in str(excinfo.value)


@pytest.mark.parametrize(
    ("error_code", "expected_type"),
    [
        ("CREDENTIAL_NOT_CONNECTED", CredentialNotConnectedError),
        ("PLATFORM_UNAVAILABLE", PlatformUnavailableError),
        ("RATE_LIMITED", RateLimitedError),
    ],
)
async def test_get_google_keyword_ideas_translates_other_known_denial_codes(
    monkeypatch, error_code, expected_type
) -> None:
    _resolve_to(monkeypatch, PlatformCode.GOOGLE)
    google_keyword_client = AsyncMock()
    google_keyword_client.read.side_effect = BrokerRequestDeniedError(error_code, _PROVIDER_DETAIL)
    port = _port(google_keyword_client=google_keyword_client)

    with pytest.raises(expected_type) as excinfo:
        await port.get_google_keyword_ideas(
            _BUSINESS_ID,
            _ACCOUNT_REF,
            seed_keywords=("zapatillas",),
            geo_target="2724",
            language="1003",
        )

    assert excinfo.value.code == error_code
    assert _PROVIDER_DETAIL not in str(excinfo.value)


async def test_an_unmapped_denial_code_is_reraised_unwrapped(monkeypatch) -> None:
    """Ningun codigo de bróker inventado se traduce por adivinanza -- se
    relanza tal cual: la red de seguridad generica de `mount.py` (nunca este
    modulo) lo convierte en `TOOL_FAILED`."""
    _resolve_to(monkeypatch)
    meta_client = AsyncMock()
    error = BrokerRequestDeniedError("SOME_FUTURE_BROKER_CODE", _PROVIDER_DETAIL)
    meta_client.read.side_effect = error
    port = _port(meta_client=meta_client)

    with pytest.raises(BrokerRequestDeniedError) as excinfo:
        await port.list_meta_pages(_BUSINESS_ID, _ACCOUNT_REF)

    assert excinfo.value is error


async def test_a_transport_failure_becomes_broker_unavailable(monkeypatch) -> None:
    _resolve_to(monkeypatch)
    meta_client = AsyncMock()
    meta_client.read.side_effect = BrokerConnectionError("no se pudo conectar al socket")
    port = _port(meta_client=meta_client)

    with pytest.raises(BrokerUnavailableError) as excinfo:
        await port.list_meta_pages(_BUSINESS_ID, _ACCOUNT_REF)

    assert excinfo.value.code == "BROKER_UNAVAILABLE"
    assert "socket" not in str(excinfo.value)


# ── Incidente 2026-09-15 (companion 0.2.25): scope de conexion ──────────────
# `meta_reference_read`/`google_reference_read` salian SIN `connection_id`; el
# dispatcher del broker (`_request_scope`) dejaba el scope en None y el store
# denegaba CREDENTIAL_NOT_CONNECTED con la conexion de Composio ACTIVA. El
# scope viaja en el `account_ref` resuelto y debe llegar al broker.

_CONNECTION_ID = "22222222-2222-2222-2222-222222222222"


def _resolve_to_connected(monkeypatch: pytest.MonkeyPatch, platform: PlatformCode) -> None:
    account = AccountRef(platform, "act_123", uuid.UUID(_BUSINESS_ID), uuid.UUID(_CONNECTION_ID))
    monkeypatch.setattr(module, "resolve_owned_account_ref", AsyncMock(return_value=account))


async def test_meta_reference_read_forwards_connection_id_to_broker(monkeypatch) -> None:
    _resolve_to_connected(monkeypatch, PlatformCode.META)
    meta_client = AsyncMock()
    meta_client.read.return_value = []
    port = _port(meta_client=meta_client)

    await port.list_meta_pages(_BUSINESS_ID, _ACCOUNT_REF)

    kwargs = meta_client.read.await_args.kwargs
    assert kwargs["business_id"] == _BUSINESS_ID
    assert kwargs["connection_id"] == _CONNECTION_ID
    assert kwargs["external_account_id"] == "act_123"


async def test_google_keyword_ideas_forwards_connection_id_to_broker(monkeypatch) -> None:
    _resolve_to_connected(monkeypatch, PlatformCode.GOOGLE)
    google_client = AsyncMock()
    google_client.read.return_value = []
    port = _port(google_keyword_client=google_client)

    await port.get_google_keyword_ideas(
        _BUSINESS_ID, _ACCOUNT_REF, seed_keywords=("perro",), geo_target="2724", language="1003"
    )

    kwargs = google_client.read.await_args.kwargs
    assert kwargs["connection_id"] == _CONNECTION_ID
    assert kwargs["external_account_id"] == "act_123"


async def test_list_meta_catalogs_translates_capability_not_implemented(monkeypatch) -> None:
    """Incidente 2026-09-15: los catalogos no cuelgan de la cuenta de anuncios;
    el broker deniega con un codigo estable y el detalle del proveedor no se filtra."""
    _resolve_to(monkeypatch)
    meta_client = AsyncMock()
    meta_client.read.side_effect = BrokerRequestDeniedError(
        "PLATFORM_CAPABILITY_NOT_IMPLEMENTED", _PROVIDER_DETAIL
    )
    port = _port(meta_client=meta_client)

    with pytest.raises(CapabilityNotAvailableError) as caught:
        await port.list_meta_catalogs(_BUSINESS_ID, _ACCOUNT_REF)

    assert _PROVIDER_DETAIL not in str(caught.value)

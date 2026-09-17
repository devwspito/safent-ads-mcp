"""H-follow-up (revision de codigo 2026-09-15, mismo incidente de
produccion que `test_broker_reference_data_port.py`, companion 0.2.21):
`BrokerGaqlReadPort.run_gaql` debe traducir `BrokerRequestDeniedError`/
`BrokerConnectionError` via `call_broker`, nunca dejarlas escapar crudas
hacia el SDK MCP."""

from __future__ import annotations

from unittest.mock import AsyncMock

import pytest

from safent_ads.accounts.application.errors import BrokerRequestDeniedError
from safent_ads.accounts.domain.refs import AccountRef
from safent_ads.accounts.infrastructure.errors import BrokerConnectionError
from safent_ads.mcp.application.errors import BrokerUnavailableError, PlatformAppNotConfiguredError
from safent_ads.mcp.infrastructure import broker_gaql_read_port as module
from safent_ads.shared.ids import PlatformCode

_BUSINESS_ID = "11111111-1111-1111-1111-111111111111"
_ACCOUNT_REF = "google:1234567890"
_PROVIDER_DETAIL = "composio_app_id=secret-internal-value"


def _resolve_to(monkeypatch: pytest.MonkeyPatch) -> None:
    account = AccountRef(PlatformCode.GOOGLE, "1234567890")
    monkeypatch.setattr(module, "resolve_owned_account_ref", AsyncMock(return_value=account))


async def test_run_gaql_translates_a_known_denial_code(monkeypatch: pytest.MonkeyPatch) -> None:
    _resolve_to(monkeypatch)
    ads_platform_port = AsyncMock()
    ads_platform_port.run_gaql.side_effect = BrokerRequestDeniedError(
        "PLATFORM_APP_NOT_CONFIGURED", _PROVIDER_DETAIL
    )
    port = module.BrokerGaqlReadPort(ads_platform_port, object())

    with pytest.raises(PlatformAppNotConfiguredError) as excinfo:
        await port.run_gaql(_BUSINESS_ID, _ACCOUNT_REF, "SELECT campaign.id FROM campaign")

    assert excinfo.value.code == "PLATFORM_APP_NOT_CONFIGURED"
    assert _PROVIDER_DETAIL not in str(excinfo.value)


async def test_run_gaql_reraises_an_unmapped_denial_code_unwrapped(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _resolve_to(monkeypatch)
    ads_platform_port = AsyncMock()
    error = BrokerRequestDeniedError("SOME_FUTURE_BROKER_CODE", _PROVIDER_DETAIL)
    ads_platform_port.run_gaql.side_effect = error
    port = module.BrokerGaqlReadPort(ads_platform_port, object())

    with pytest.raises(BrokerRequestDeniedError) as excinfo:
        await port.run_gaql(_BUSINESS_ID, _ACCOUNT_REF, "SELECT campaign.id FROM campaign")

    assert excinfo.value is error


async def test_run_gaql_translates_a_transport_failure(monkeypatch: pytest.MonkeyPatch) -> None:
    _resolve_to(monkeypatch)
    ads_platform_port = AsyncMock()
    ads_platform_port.run_gaql.side_effect = BrokerConnectionError("no se pudo conectar al socket")
    port = module.BrokerGaqlReadPort(ads_platform_port, object())

    with pytest.raises(BrokerUnavailableError) as excinfo:
        await port.run_gaql(_BUSINESS_ID, _ACCOUNT_REF, "SELECT campaign.id FROM campaign")

    assert excinfo.value.code == "BROKER_UNAVAILABLE"
    assert "socket" not in str(excinfo.value)

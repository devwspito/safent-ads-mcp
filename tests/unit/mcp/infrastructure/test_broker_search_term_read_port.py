"""Piezas puras de `BrokerSearchTermReadPort` (sin DB, sin broker):
construccion de la consulta a partir de la plantilla + ventana, y el
mapeo de una fila `search_term_view` a `SearchTermSummary`. El camino
completo (IDOR + Google/Meta + broker falso) va en
`tests/integration/mcp/test_search_term_read_port_contract.py`.

Las pruebas de `call_broker` (H-follow-up, revision de codigo 2026-09-15,
mismo incidente de produccion que `test_broker_reference_data_port.py`) si
viven aqui: son unitarias (dobles, sin DB), mismo criterio que el resto de
este fichero."""

from __future__ import annotations

from datetime import UTC, date, datetime
from decimal import Decimal
from unittest.mock import AsyncMock

import pytest

from safent_ads.accounts.application.errors import BrokerRequestDeniedError
from safent_ads.accounts.domain.refs import AccountRef
from safent_ads.accounts.infrastructure.errors import BrokerConnectionError
from safent_ads.mcp.application.dto import Window, WindowPreset
from safent_ads.mcp.application.errors import BrokerUnavailableError, PlatformAppNotConfiguredError
from safent_ads.mcp.infrastructure import broker_search_term_read_port as module
from safent_ads.mcp.infrastructure.broker_search_term_read_port import (
    _MAX_ROWS,
    _row_to_summary,
    _with_window_and_limit,
)
from safent_ads.shared.clock import FixedClock
from safent_ads.shared.ids import PlatformCode

_ROW = {
    "search_term_view.search_term": "zapatillas running baratas",
    "segments.keyword.info.text": "zapatillas running",
    "campaign.resource_name": "customers/111/campaigns/1",
    "ad_group.resource_name": "customers/111/adGroups/2",
    "metrics.cost_micros": 4_500_000,
    "metrics.conversions": 2.0,
    "customer.currency_code": "EUR",
}


def test_with_window_and_limit_appends_a_safe_where_and_limit_clause() -> None:
    query = _with_window_and_limit(
        "SELECT search_term_view.search_term FROM search_term_view",
        date(2026, 9, 1),
        date(2026, 9, 7),
    )

    assert query == (
        "SELECT search_term_view.search_term FROM search_term_view WHERE "
        "segments.date BETWEEN '2026-09-01' AND '2026-09-07' LIMIT 500"
    )
    assert f"LIMIT {_MAX_ROWS}" in query


def test_row_to_summary_maps_micros_to_major_currency_units() -> None:
    summary = _row_to_summary(_ROW)

    assert summary.term == "zapatillas running baratas"
    assert summary.matched_keyword == "zapatillas running"
    assert summary.campaign_ref == "customers/111/campaigns/1"
    assert summary.ad_group_ref == "customers/111/adGroups/2"
    assert summary.cost.amount == Decimal("4.5")
    assert summary.cost.currency == "EUR"
    assert summary.conversions == 2


def test_row_to_summary_treats_a_missing_matched_keyword_as_none() -> None:
    row = dict(_ROW)
    del row["segments.keyword.info.text"]

    summary = _row_to_summary(row)

    assert summary.matched_keyword is None


_BUSINESS_ID = "11111111-1111-1111-1111-111111111111"
_ACCOUNT_REF = "google:1234567890"
_PROVIDER_DETAIL = "composio_app_id=secret-internal-value"
_WINDOW = Window(preset=WindowPreset.SEVEN_DAYS, lag_days=0, date_from=None, date_to=None)


def _google_port(
    ads_platform_port: AsyncMock, monkeypatch: pytest.MonkeyPatch
) -> module.BrokerSearchTermReadPort:
    account = AccountRef(PlatformCode.GOOGLE, "1234567890")
    monkeypatch.setattr(module, "resolve_owned_account_ref", AsyncMock(return_value=account))
    return module.BrokerSearchTermReadPort(
        ads_platform_port,
        object(),
        FixedClock(datetime(2026, 9, 15, 12, tzinfo=UTC)),
        query_template="SELECT search_term_view.search_term FROM search_term_view",
    )


async def test_list_search_terms_translates_a_known_denial_code(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    ads_platform_port = AsyncMock()
    ads_platform_port.run_gaql.side_effect = BrokerRequestDeniedError(
        "PLATFORM_APP_NOT_CONFIGURED", _PROVIDER_DETAIL
    )
    port = _google_port(ads_platform_port, monkeypatch)

    with pytest.raises(PlatformAppNotConfiguredError) as excinfo:
        await port.list_search_terms(_BUSINESS_ID, _ACCOUNT_REF, window=_WINDOW)

    assert excinfo.value.code == "PLATFORM_APP_NOT_CONFIGURED"
    assert _PROVIDER_DETAIL not in str(excinfo.value)


async def test_list_search_terms_reraises_an_unmapped_denial_code_unwrapped(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    ads_platform_port = AsyncMock()
    error = BrokerRequestDeniedError("SOME_FUTURE_BROKER_CODE", _PROVIDER_DETAIL)
    ads_platform_port.run_gaql.side_effect = error
    port = _google_port(ads_platform_port, monkeypatch)

    with pytest.raises(BrokerRequestDeniedError) as excinfo:
        await port.list_search_terms(_BUSINESS_ID, _ACCOUNT_REF, window=_WINDOW)

    assert excinfo.value is error


async def test_list_search_terms_translates_a_transport_failure(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    ads_platform_port = AsyncMock()
    ads_platform_port.run_gaql.side_effect = BrokerConnectionError("no se pudo conectar al socket")
    port = _google_port(ads_platform_port, monkeypatch)

    with pytest.raises(BrokerUnavailableError) as excinfo:
        await port.list_search_terms(_BUSINESS_ID, _ACCOUNT_REF, window=_WINDOW)

    assert excinfo.value.code == "BROKER_UNAVAILABLE"
    assert "socket" not in str(excinfo.value)

"""`SearchTermReadPort` real: delega en `AdsPlatformPort.run_gaql` con la
plantilla versionada `broker/platforms/gaql/search_terms.gaql` (`composition.
gaql_templates.load_gaql_template`, T162 follow-up), tras la misma
comprobacion IDOR que `BrokerGaqlReadPort`
(`mcp.infrastructure.account_ownership.resolve_owned_account_ref`).

Meta no expone `search_term_view` (contracts/platform-port.md: `run_gaql`
es especifico de Google, `MetaAdsAdapter.run_gaql` lanza
`PlatformCapabilityNotImplementedError`) -- aqui se detecta ANTES de tocar
el broker, a partir del propio `account_ref` (el prefijo de plataforma no
es secreto, `google:.../meta:...`), para que la respuesta tipada de "no
soportado" no dependa de que la llamada real fallara ni añada una vuelta
de red innecesaria.

`run_gaql` pasa por `call_broker` (H-follow-up, revision de codigo
2026-09-15, mismo incidente de produccion que `broker_reference_data_port
.py`): sin esto, un `BrokerRequestDeniedError`/`BrokerConnectionError`
crudo escapaba como el `UnexpectedToolError` opaco del SDK MCP en vez del
sobre limpio del contrato."""

from __future__ import annotations

from collections.abc import Mapping
from datetime import date
from decimal import Decimal
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from safent_ads.accounts.application.ports import AdsPlatformPort
from safent_ads.mcp.application.dto import Window
from safent_ads.mcp.application.search_terms_ports import SearchTermsResult, SearchTermSummary
from safent_ads.mcp.infrastructure.account_ownership import resolve_owned_account_ref
from safent_ads.mcp.infrastructure.broker_call import call_broker
from safent_ads.mcp.infrastructure.window_bounds import window_bounds
from safent_ads.shared.clock import Clock
from safent_ads.shared.ids import PlatformCode
from safent_ads.shared.read_models.dto import Money

__all__ = ["BrokerSearchTermReadPort"]

_MAX_ROWS = 500  # tool-surface.md P2 (`list_search_terms`): tope de filas del contrato
_MICROS_PER_UNIT = Decimal(1_000_000)
_REASON_PLATFORM_NOT_SUPPORTED = "platform_not_supported"


class BrokerSearchTermReadPort:
    def __init__(
        self,
        ads_platform_port: AdsPlatformPort,
        session_factory: async_sessionmaker[AsyncSession],
        clock: Clock,
        *,
        query_template: str,
    ) -> None:
        self._ads_platform_port = ads_platform_port
        self._session_factory = session_factory
        self._clock = clock
        self._query_template = query_template

    async def list_search_terms(
        self, business_id: str, account_ref: str, *, window: Window
    ) -> SearchTermsResult:
        ref = await resolve_owned_account_ref(self._session_factory, business_id, account_ref)
        if ref.platform is not PlatformCode.GOOGLE:
            return SearchTermsResult(
                account_ref=account_ref,
                is_supported=False,
                reason=_REASON_PLATFORM_NOT_SUPPORTED,
                terms=[],
            )
        start, end = window_bounds(window, self._clock.now().date())
        query = _with_window_and_limit(self._query_template, start, end)
        rows = await call_broker(self._ads_platform_port.run_gaql(ref, query, max_rows=_MAX_ROWS))
        return SearchTermsResult(
            account_ref=account_ref,
            is_supported=True,
            reason=None,
            terms=[_row_to_summary(row) for row in rows],
        )


def _with_window_and_limit(template: str, start: date, end: date) -> str:
    return (
        f"{template} WHERE segments.date BETWEEN '{start.isoformat()}' AND '{end.isoformat()}' "
        f"LIMIT {_MAX_ROWS}"
    )


def _row_to_summary(row: Mapping[str, Any]) -> SearchTermSummary:
    currency = str(row["customer.currency_code"])
    cost_micros = Decimal(int(row["metrics.cost_micros"]))
    matched_keyword = row.get("segments.keyword.info.text")
    return SearchTermSummary(
        term=str(row["search_term_view.search_term"]),
        cost=Money(cost_micros / _MICROS_PER_UNIT, currency),
        conversions=int(float(row["metrics.conversions"])),
        matched_keyword=str(matched_keyword) if matched_keyword else None,
        campaign_ref=str(row["campaign.resource_name"]),
        ad_group_ref=str(row["ad_group.resource_name"]),
    )

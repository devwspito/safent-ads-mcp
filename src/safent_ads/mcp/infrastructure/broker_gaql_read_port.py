"""`GaqlPort` real (integracion, lane `gaql`): delega en `AdsPlatformPort.
run_gaql` (`BrokerSocketClient`, sin SDKs en `ads-api`/`ads-worker`) tras
comprobar que `account_ref` es del `business_id` que llama.

El socket del broker no conoce negocios (`AccountRef` es solo `(platform,
external_account_id)`, contracts/platform-port.md): la comprobacion IDOR
vive en `mcp.infrastructure.account_ownership.resolve_owned_account_ref`
(compartida con `broker_search_term_read_port.py`), contra
`platform_accounts` via `SqlAccountRepository.get_by_ref` -- mismo
principio que `mcp.infrastructure.sql_rule_read_port.list_guardrails`
("una cuenta ajena nunca debe responder"). Cuenta inexistente o de otro
negocio devuelven el mismo `EntityNotFoundError` (IDOR-safe: no se filtra
cual de los dos casos es).

`composition/app.py` sigue cableando `NotYetWiredGaqlPort` (fuera de
alcance de este lane, `composition/*` es de la lane "surface"): la
integracion de una linea documentada en el informe de este lane sustituye
`gaql=NotYetWiredGaqlPort()` por `gaql=BrokerGaqlReadPort(container.
ads_platform_port, database.session_factory)` en `composition/container.py`
y `composition/app.py`.

`run_gaql` pasa por `call_broker` (H-follow-up, revision de codigo
2026-09-15, mismo incidente de produccion que `broker_reference_data_port
.py`): sin esto, un `BrokerRequestDeniedError`/`BrokerConnectionError`
crudo escapaba como el `UnexpectedToolError` opaco del SDK MCP en vez del
sobre limpio del contrato."""

from __future__ import annotations

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from safent_ads.accounts.application.ports import AdsPlatformPort
from safent_ads.mcp.application.dto import GaqlResult
from safent_ads.mcp.infrastructure.account_ownership import resolve_owned_account_ref
from safent_ads.mcp.infrastructure.broker_call import call_broker

_MAX_ROWS = 500  # techo de la herramienta MCP (contracts/mcp-tools.md); el broker admite mas


class BrokerGaqlReadPort:
    def __init__(
        self,
        ads_platform_port: AdsPlatformPort,
        session_factory: async_sessionmaker[AsyncSession],
    ) -> None:
        self._ads_platform_port = ads_platform_port
        self._session_factory = session_factory

    async def run_gaql(self, business_id: str, account_ref: str, query: str) -> GaqlResult:
        ref = await resolve_owned_account_ref(self._session_factory, business_id, account_ref)
        rows = await call_broker(self._ads_platform_port.run_gaql(ref, query, max_rows=_MAX_ROWS))
        stringified_rows = [dict(row) for row in rows]
        return GaqlResult(
            account_ref=account_ref, rows=stringified_rows, row_count=len(stringified_rows)
        )

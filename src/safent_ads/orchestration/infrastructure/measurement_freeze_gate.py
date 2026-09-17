"""`MeasurementFreezeGate` (T158, profitability-engine.md §5 nodo 1):
"cuando la medicion esta rota... el ciclo de reglas debe marcar la cuenta
MEASUREMENT_FROZEN de forma que ningun AUTO actue [...] (defensivo: las
reglas mantienen pause/lower solo en senales duras)". Reutiliza
`optimization.domain.diagnosis.is_measurement_broken` (el nodo 1 extraido a
funcion publica) sobre `unattributed_share`/`delta_hat` reales -- las
mismas fuentes que `SqlDiagnosisMetricsPort` (T156/T158), pero a nivel de
CUENTA (una vez por cuenta y vuelta de `RuleCycle`), no por entidad: el
freno de BUY es de cuenta, no de campana individual.

`bridge_has_recent_events_24h` (spec 027 T017, contracts/crm-link.md §2
'PUT /crm/bridge-health'): lee `crm_bridge_health` SOLO cuando el negocio
tiene un puente CRM configurado -- un negocio sin puente configurado
jamas se congela por este motivo (defecto = sano, igual que antes de esta
tarea, cuando el parametro estaba fijo a `True`). Con puente configurado,
la lectura es real y solo puede empeorar, nunca mejorar, la decision."""

from __future__ import annotations

from datetime import timedelta
from typing import Final

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from safent_ads.accounts.domain.platform_account import PlatformAccount
from safent_ads.crm.infrastructure.sql_crm_bridge_health_repository import (
    SqlCrmBridgeHealthRepository,
)
from safent_ads.optimization.domain.diagnosis import is_measurement_broken
from safent_ads.shared.clock import Clock
from safent_ads.shared.ids import BusinessId

__all__ = ["SqlMeasurementFreezeGate"]

# Misma ventana que `SqlDiagnosisMetricsPort._UNATTRIBUTED_SHARE_WINDOW_DAYS`:
# lo bastante amplia para no saltar por un dia sin CRM, lo bastante corta
# para reflejar un puente roto reciente.
_UNATTRIBUTED_SHARE_WINDOW_DAYS: Final = 30

_PLATFORM_ACCOUNT_ID = text("""
    SELECT id FROM platform_accounts
     WHERE business_id = :business_id AND platform = :platform
       AND external_account_id = :external_account_id
""")

_LATEST_DIVERGENCE = text("""
    SELECT value FROM platform_divergence_snapshots
     WHERE business_id = :business_id AND platform_account_id = :platform_account_id
     ORDER BY computed_at DESC
     LIMIT 1
""")

_UNATTRIBUTED_SHARE = text("""
    SELECT count(*) FILTER (WHERE attribution_rung = 'aggregate')::float
           / NULLIF(count(*), 0) AS share
      FROM lead_attributions
     WHERE business_id = :business_id
       AND occurred_at >= :start_date AND occurred_at < :end_date
""")


class SqlMeasurementFreezeGate:
    def __init__(self, session: AsyncSession, clock: Clock) -> None:
        self._session = session
        self._clock = clock
        self._crm_bridge_health = SqlCrmBridgeHealthRepository(session)

    async def is_frozen(self, business_id: BusinessId, account: PlatformAccount) -> bool:
        platform_account_id = await self._platform_account_id(business_id, account)
        delta_hat = (
            None
            if platform_account_id is None
            else await self._latest_delta_hat(business_id, platform_account_id)
        )
        unattributed_share = await self._unattributed_share(business_id)
        return is_measurement_broken(
            unattributed_share=unattributed_share,
            delta_hat=delta_hat,
            utm_valid=True,
            bridge_has_recent_events_24h=await self._bridge_has_recent_events_24h(business_id),
        )

    async def _bridge_has_recent_events_24h(self, business_id: BusinessId) -> bool:
        health = await self._crm_bridge_health.get_for_business(business_id=business_id)
        # Sin puente configurado: no hay nada que congelar por este motivo
        # (spec 027 T017, "un puente sin configurar no congela nada").
        return True if health is None else health.has_recent_events_24h

    async def _platform_account_id(
        self, business_id: BusinessId, account: PlatformAccount
    ) -> str | None:
        result = await self._session.execute(
            _PLATFORM_ACCOUNT_ID,
            {
                "business_id": business_id.value,
                "platform": account.account_ref.platform.value,
                "external_account_id": account.account_ref.external_account_id,
            },
        )
        row = result.mappings().one_or_none()
        return None if row is None else str(row["id"])

    async def _latest_delta_hat(
        self, business_id: BusinessId, platform_account_id: str
    ) -> float | None:
        result = await self._session.execute(
            _LATEST_DIVERGENCE,
            {"business_id": business_id.value, "platform_account_id": platform_account_id},
        )
        row = result.mappings().one_or_none()
        return None if row is None else float(row["value"])

    async def _unattributed_share(self, business_id: BusinessId) -> float:
        as_of = self._clock.now().date()
        result = await self._session.execute(
            _UNATTRIBUTED_SHARE,
            {
                "business_id": business_id.value,
                "start_date": as_of - timedelta(days=_UNATTRIBUTED_SHARE_WINDOW_DAYS),
                "end_date": as_of + timedelta(days=1),
            },
        )
        share = result.scalar_one_or_none()
        return 0.0 if share is None else float(share)

"""`CrmBridgeHealth` (entidad, data-model.md §CrmBridgeHealth, spec 027 A-3):
la costura que gobierna si se puede subir gasto. `has_recent_events_24h` se
CALCULA desde el ultimo `RevenueEvent` ingerido, nunca se declara; el
estado de conector que reporta el runtime solo puede EMPEORARLO, nunca
mejorarlo -- un runtime comprometido no puede descongelar el gasto
mintiendo (contracts/crm-link.md §2 `PUT /crm/bridge-health`)."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta
from enum import StrEnum

from safent_ads.shared.ids import BusinessId

_HEALTHY_WITHIN = timedelta(hours=24)


class ConnectorBridgeState(StrEnum):
    """Subconjunto de `Connector.state` (data-model.md, repo runtime) que
    este puente acepta -- `configurando`/`esperando_autorizacion`/
    `sin_verificar` no aplican aqui: ese conector todavia no envia hechos."""

    READY = "listo"
    DEGRADED = "degradado"
    SUSPENDED = "suspendido"


@dataclass(frozen=True, kw_only=True, slots=True)
class CrmBridgeHealth:
    business_id: BusinessId
    connector_id: str
    connector_state: ConnectorBridgeState
    last_event_at: datetime | None
    has_recent_events_24h: bool
    cause: str | None
    updated_at: datetime

    @classmethod
    def evaluate(
        cls,
        *,
        business_id: BusinessId,
        connector_id: str,
        connector_state: ConnectorBridgeState,
        last_event_at: datetime | None,
        as_of: datetime,
        cause: str | None,
    ) -> CrmBridgeHealth:
        events_are_fresh = last_event_at is not None and (as_of - last_event_at) <= _HEALTHY_WITHIN
        # "el estado del conector solo puede empeorarlo, nunca mejorarlo":
        # un conector degradado/suspendido nunca reporta sano, aunque los
        # hechos sean recientes; un conector listo con hechos viejos sigue
        # sin serlo.
        has_recent_events_24h = events_are_fresh and connector_state is ConnectorBridgeState.READY
        return cls(
            business_id=business_id,
            connector_id=connector_id,
            connector_state=connector_state,
            last_event_at=last_event_at,
            has_recent_events_24h=has_recent_events_24h,
            cause=cause,
            updated_at=as_of,
        )

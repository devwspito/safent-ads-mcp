"""Puerto de lectura del panel (T046/T110). Un unico Protocol: todas las
rutas de esta lane (`/portfolio`, `/entities/*`, `/signals*`, `/anomalies`,
`/pacing`, `/freshness`, `/badges`) comparten el mismo origen de datos de
solo lectura (plan.md §4: leido, nunca importado; la integracion cablea el
adaptador real)."""

from __future__ import annotations

from datetime import datetime
from typing import Protocol

from safent_ads.panel.application.cockpit_dto import ChangeStrip, CockpitView
from safent_ads.panel.application.dto import (
    AnomalyView,
    Badges,
    EntityChild,
    EntityDetail,
    EntityHistoryEntry,
    EntityMetrics,
    PacingView,
    PortfolioView,
    SignalDetailView,
    SignalsPage,
)
from safent_ads.shared.read_models.dto import Freshness


class PanelReadPort(Protocol):
    async def get_portfolio(self, business_id: str, *, window: str) -> PortfolioView: ...

    async def get_entity(self, entity_ref: str) -> EntityDetail: ...

    async def get_entity_children(self, entity_ref: str) -> list[EntityChild]: ...

    async def get_entity_metrics(
        self, entity_ref: str, *, window: str, granularity: str
    ) -> EntityMetrics: ...

    async def get_entity_history(self, entity_ref: str) -> list[EntityHistoryEntry]: ...

    async def get_freshness(self, business_id: str) -> list[Freshness]: ...

    async def list_signals(
        self,
        business_id: str,
        *,
        kind: str | None,
        min_strength: int | None,
        since: datetime | None,
        platform: str | None,
        entity_ref: str | None,
        limit: int | None,
        cursor: str | None,
    ) -> SignalsPage: ...

    async def get_signal(self, signal_id: str) -> SignalDetailView: ...

    async def list_anomalies(self, business_id: str, *, since: datetime) -> list[AnomalyView]: ...

    async def get_pacing(self, entity_ref: str) -> PacingView: ...

    async def get_badges(self, business_id: str, *, signals_since: datetime | None) -> Badges: ...


class CockpitReadPort(Protocol):
    """026, contracts/cockpit-read-model.md §6: Protocol propio (ISP) --
    `panel` sigue sin importar `signals`/`accounts`/`proposals`/`execution`
    (plan.md §4); `sql_cockpit_read_model.py` compone los read models
    existentes detras de este puerto."""

    async def get_cockpit(self, business_id: str, *, window: str) -> CockpitView: ...

    async def get_changes_since(self, business_id: str, *, since: datetime) -> ChangeStrip: ...

"""Rutas REST de lectura (T046/T110, contracts/rest-api.md): `/portfolio`,
`/entities/*`, `/signals*`, `/anomalies`, `/pacing`, `/freshness`,
`/badges`. Sin logica de negocio: validan, llaman el puerto, mapean a JSON
compacto."""

from __future__ import annotations

from collections.abc import Awaitable
from datetime import UTC, datetime
from typing import Annotated, Any, Literal

from fastapi import APIRouter, Depends, HTTPException, Query

from safent_ads.panel.application.errors import PanelEntityNotFoundError
from safent_ads.panel.application.ports import PanelReadPort
from safent_ads.panel.presentation.deps import (
    CallerDep,
    ensure_business_access,
    require_business_access,
)
from safent_ads.shared.read_models.serialization import to_json_dict, to_json_value

BusinessIdDep = Annotated[str, Depends(require_business_access)]
PortfolioWindow = Literal["7D", "14D", "30D"]


def build_panel_router(port: PanelReadPort) -> APIRouter:
    router = APIRouter(prefix="/api/v1", tags=["panel"])

    @router.get("/portfolio")
    async def get_portfolio(
        business_id: BusinessIdDep, window: PortfolioWindow = "7D"
    ) -> dict[str, Any]:
        view = await port.get_portfolio(business_id, window=window)
        return to_json_dict(view)

    @router.get("/freshness")
    async def get_freshness(business_id: BusinessIdDep) -> dict[str, Any]:
        return {"items": to_json_value(await port.get_freshness(business_id))}

    @router.get("/entities/{entity_ref:path}/children")
    async def get_entity_children(entity_ref: str, caller: CallerDep) -> dict[str, Any]:
        detail = await _fetch_or_404(port.get_entity(entity_ref))
        ensure_business_access(detail.business_id, caller)
        items = await port.get_entity_children(entity_ref)
        next_level = {"campaign": "ad_set", "ad_set": "ad", "ad": "creative"}
        return {
            "parent_ref": detail.entity_ref,
            "parent_name": detail.name,
            "level": items[0].level if items else next_level.get(detail.level, "creative"),
            "items": to_json_value(items),
        }

    @router.get("/entities/{entity_ref:path}/metrics")
    async def get_entity_metrics(
        entity_ref: str, caller: CallerDep, window: str = "7D", granularity: str = "daily"
    ) -> dict[str, Any]:
        detail = await _fetch_or_404(port.get_entity(entity_ref))
        ensure_business_access(detail.business_id, caller)
        metrics = await port.get_entity_metrics(entity_ref, window=window, granularity=granularity)
        return to_json_dict(metrics)

    @router.get("/entities/{entity_ref:path}/history")
    async def get_entity_history(entity_ref: str, caller: CallerDep) -> dict[str, Any]:
        detail = await _fetch_or_404(port.get_entity(entity_ref))
        ensure_business_access(detail.business_id, caller)
        return {"items": to_json_value(await port.get_entity_history(entity_ref))}

    # Keep the greedy entity reference after its suffix routes. Scoped and
    # encoded references still resolve without swallowing /children, /metrics
    # or /history as part of the external identifier.
    @router.get("/entities/{entity_ref:path}")
    async def get_entity(entity_ref: str, caller: CallerDep) -> dict[str, Any]:
        detail = await _fetch_or_404(port.get_entity(entity_ref))
        ensure_business_access(detail.business_id, caller)
        return to_json_dict(detail)

    @router.get("/signals")
    async def list_signals(
        business_id: BusinessIdDep,
        *,
        kind: str | None = None,
        min_strength: Annotated[int | None, Query(ge=0, le=100)] = None,
        since: datetime | None = None,
        platform: str | None = None,
        entity_ref: str | None = None,
        limit: Annotated[int | None, Query(ge=1, le=200)] = None,
        cursor: str | None = None,
    ) -> dict[str, Any]:
        page = await port.list_signals(
            business_id,
            kind=kind,
            min_strength=min_strength,
            since=since,
            platform=platform,
            entity_ref=entity_ref,
            limit=limit,
            cursor=cursor,
        )
        return to_json_dict(page)

    @router.get("/signals/{signal_id}")
    async def get_signal(signal_id: str, caller: CallerDep) -> dict[str, Any]:
        detail = await _fetch_or_404(port.get_signal(signal_id))
        ensure_business_access(detail.signal.business_id, caller)
        return to_json_dict(detail)

    @router.get("/signals/{signal_id}/outcome")
    async def get_signal_outcome(signal_id: str, caller: CallerDep) -> dict[str, Any]:
        # Alias del `outcome` embebido en `/signals` (contracts/rest-api.md
        # §Senales): mismo contraste a 14 dias, misma fuente de verdad, sin
        # el antiguo booleano `matched`.
        detail = await _fetch_or_404(port.get_signal(signal_id))
        ensure_business_access(detail.signal.business_id, caller)
        return to_json_dict(detail.signal.outcome)

    @router.get("/anomalies")
    async def list_anomalies(
        business_id: BusinessIdDep, since: datetime | None = None
    ) -> dict[str, Any]:
        anomalies = await port.list_anomalies(business_id, since=since or _epoch())
        return {"items": to_json_value(anomalies)}

    @router.get("/pacing")
    async def get_pacing(entity_ref: str, caller: CallerDep) -> dict[str, Any]:
        view = await _fetch_or_404(port.get_pacing(entity_ref))
        ensure_business_access(view.business_id, caller)
        return to_json_dict(view)

    @router.get("/badges")
    async def get_badges(
        business_id: BusinessIdDep, signals_since: datetime | None = None
    ) -> dict[str, Any]:
        badges = await port.get_badges(business_id, signals_since=signals_since)
        return to_json_dict(badges)

    return router


async def _fetch_or_404[T](awaitable: Awaitable[T]) -> T:
    try:
        return await awaitable
    except PanelEntityNotFoundError as exc:
        raise HTTPException(status_code=404, detail="No encontrado") from exc


def _epoch() -> datetime:
    return datetime(1970, 1, 1, tzinfo=UTC)

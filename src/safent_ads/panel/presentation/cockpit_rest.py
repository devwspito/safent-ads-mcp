"""`GET /api/v1/cockpit` (026, tasks.md T007, contracts/cockpit-read-model.md
§1): una instantanea por `(business_id, window)`, memoizada con TTL = mitad
de la ventana de frescura (`NFR-001`, 60 min), tope 60s -- el tope domina
siempre, asi que el TTL efectivo es 60s. `ETag`/`If-None-Match` -> `304`
sobre esa misma instantanea; sin parametros de filtro/orden (SC-003: el
cliente filtra, la consulta nunca diverge de `GET /portfolio`)."""

from __future__ import annotations

import asyncio
import hashlib
import json
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Annotated

from fastapi import APIRouter, Depends, Request, Response
from fastapi.responses import JSONResponse

from safent_ads.panel.application.cockpit_dto import CockpitView, CockpitWindow
from safent_ads.panel.application.ports import CockpitReadPort
from safent_ads.panel.presentation.deps import require_business_access
from safent_ads.shared.clock import Clock, SystemClock
from safent_ads.shared.read_models.serialization import to_cockpit_json_dict

_CACHE_TTL = timedelta(seconds=60)
BusinessIdDep = Annotated[str, Depends(require_business_access)]


@dataclass(frozen=True, slots=True)
class _CacheEntry:
    view: CockpitView
    etag: str
    expires_at: datetime


def _compute_etag(business_id: str, window: str, view: CockpitView) -> str:
    body = to_cockpit_json_dict(view)
    canonical = json.dumps(body, sort_keys=True, default=str, separators=(",", ":"))
    digest = hashlib.sha256(canonical.encode("utf-8")).hexdigest()[:32]
    return f'"{business_id}:{window}:{digest}"'


class InMemoryCockpitCache:
    """Memoizacion en proceso, sin Redis (mismo criterio que
    `TokenBucketRateLimiter` en `composition/api.py`): un unico `ads-api`,
    modelo de un unico propietario -- una cerradura global basta."""

    def __init__(self, *, clock: Clock | None = None, ttl: timedelta = _CACHE_TTL) -> None:
        self._clock = clock or SystemClock()
        self._ttl = ttl
        self._entries: dict[tuple[str, str], _CacheEntry] = {}
        self._lock = asyncio.Lock()

    async def get_or_fetch(
        self,
        business_id: str,
        window: str,
        fetch: Callable[[], Awaitable[CockpitView]],
    ) -> _CacheEntry:
        key = (business_id, window)
        async with self._lock:
            now = self._clock.now()
            cached = self._entries.get(key)
            if cached is not None and now < cached.expires_at:
                return cached
            view = await fetch()
            entry = _CacheEntry(
                view=view,
                etag=_compute_etag(business_id, window, view),
                expires_at=now + self._ttl,
            )
            self._entries[key] = entry
            return entry


def build_cockpit_router(port: CockpitReadPort, *, clock: Clock | None = None) -> APIRouter:
    cache = InMemoryCockpitCache(clock=clock)
    router = APIRouter(prefix="/api/v1", tags=["cockpit"])

    @router.get("/cockpit")
    async def get_cockpit(
        business_id: BusinessIdDep,
        request: Request,
        response: Response,
        window: CockpitWindow = CockpitWindow.SEVEN_DAYS,
    ) -> Response:
        entry = await cache.get_or_fetch(
            business_id, window.value, lambda: port.get_cockpit(business_id, window=window.value)
        )
        response.headers["cache-control"] = "private, no-store"
        response.headers["etag"] = entry.etag
        if request.headers.get("if-none-match") == entry.etag:
            return Response(status_code=304, headers=dict(response.headers))
        return JSONResponse(to_cockpit_json_dict(entry.view), headers=dict(response.headers))

    return router

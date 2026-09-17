"""`InMemoryQuota` (T044, threat-model.md C-23): ventana fija de 60s por
`(caller_id, tool_name)`. En memoria a proposito: un unico proceso `ads-api`
sirve `/mcp` (plan.md §3); si eso deja de ser cierto, este puerto se
respalda en Redis sin tocar `ToolDispatcher`.

004 tasks-2.md Q1 (contracts/mcp.md §6): un segundo cubo agregado por
`(caller_id, "escrituras")` a 10/min cubre `PROPOSAL`/`CATALOG_WRITE`/
`CONNECTION_WRITE` y, en cuanto `registry.ToolClass` gane `CREATIVE_WRITE`
(carril integrador I1), tambien esa clase -- el cubo se activa para
"cualquier clase que no sea `read`", nunca enumerando cada nombre, asi
que no hace falta volver a tocar este fichero cuando llegue esa clase.
Cuatro herramientas de lectura/propuesta caras tienen su propio limite por
nombre dentro del cubo de siempre, mas estrecho que el default de 60;
los valores por defecto viven aqui en el codigo, `composition/settings.py`
los deja configurables para quien cablee el puerto (I1)."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass

from safent_ads.shared.clock import Clock

_DEFAULT_LIMIT_PER_MINUTE = 60
_WINDOW_SECONDS = 60

# ToolClass.READ.value (mcp/presentation/registry.py): la unica clase que
# NUNCA entra en el cubo de escrituras. No se importa el enum de
# `presentation` desde `infrastructure` (SOLID/DDD: la capa de abajo no
# depende de la de arriba) -- el dispatcher pasa el `.value`, ya un `str`.
_READ_TOOL_CLASS = "read"
_WRITES_BUCKET_KEY = "escrituras"
_DEFAULT_WRITES_LIMIT_PER_MINUTE = 10

_DEFAULT_PER_TOOL_LIMITS: Mapping[str, int] = {
    "get_meta_graph": 30,
    "search_competitor_ads": 10,
    "get_google_keyword_ideas": 10,
    "upload_creative_asset": 3,
}


@dataclass(slots=True)
class _Window:
    started_at: float
    count: int


class InMemoryQuota:
    def __init__(
        self,
        *,
        clock: Clock,
        limit_per_minute: int = _DEFAULT_LIMIT_PER_MINUTE,
        writes_limit_per_minute: int = _DEFAULT_WRITES_LIMIT_PER_MINUTE,
        per_tool_limits: Mapping[str, int] | None = None,
    ) -> None:
        self._clock = clock
        self._limit = limit_per_minute
        self._writes_limit = writes_limit_per_minute
        self._per_tool_limits = dict(per_tool_limits or _DEFAULT_PER_TOOL_LIMITS)
        self._windows: dict[str, _Window] = {}

    async def check_and_consume(
        self, *, caller_id: str, tool_name: str, tool_class: str
    ) -> bool:
        now = self._clock.now().timestamp()
        keys = self._keys_for(caller_id, tool_name, tool_class)
        if not all(self._within_limit(key, limit, now) for key, limit in keys):
            return False
        for key, _limit in keys:
            self._consume(key, now)
        return True

    def _keys_for(
        self, caller_id: str, tool_name: str, tool_class: str
    ) -> list[tuple[str, int]]:
        tool_limit = self._per_tool_limits.get(tool_name, self._limit)
        keys = [(f"{caller_id}:{tool_name}", tool_limit)]
        if tool_class != _READ_TOOL_CLASS:
            keys.append((f"{caller_id}:{_WRITES_BUCKET_KEY}", self._writes_limit))
        return keys

    def _within_limit(self, key: str, limit: int, now: float) -> bool:
        window = self._windows.get(key)
        if window is None or now - window.started_at >= _WINDOW_SECONDS:
            return True
        return window.count < limit

    def _consume(self, key: str, now: float) -> None:
        window = self._windows.get(key)
        if window is None or now - window.started_at >= _WINDOW_SECONDS:
            self._windows[key] = _Window(started_at=now, count=1)
            return
        window.count += 1

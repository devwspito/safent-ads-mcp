"""T127 "credential health counts": recuento de `credential_refs`
(0013_oauth_connect) por `(platform, status)`, sin tocar `accounts/` (fuera
de este carril, tasks.md). Consulta de solo lectura, agregada: nunca sale
un `id`/`alias`/token, solo un entero por combinacion de las dos
plataformas soportadas y los cuatro estados de salud posibles -- misma
rejilla acotada que `accounts.domain.platform_credential.CredentialStatus`,
sin importar ese modulo (evita cualquier dependencia nueva hacia un
paquete fuera de este carril)."""

from __future__ import annotations

import asyncio

import structlog
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from safent_ads.observability.metrics import set_credential_health

logger = structlog.get_logger(__name__)

_KNOWN_PLATFORMS: tuple[str, ...] = ("google", "meta")
_KNOWN_STATUSES: tuple[str, ...] = ("connected", "expired", "revoked", "invalid")
_DEFAULT_REFRESH_INTERVAL_SECONDS = 60.0

_COUNT_SQL = text(
    "SELECT platform, lower(status) AS status, count(*) AS total "
    "FROM credential_refs GROUP BY platform, status"
)


async def _refresh_once(session_factory: async_sessionmaker[AsyncSession]) -> None:
    async with session_factory() as session:
        rows = (await session.execute(_COUNT_SQL)).all()
    counts = {(row.platform, row.status): row.total for row in rows}
    set_credential_health(
        counts, known_platforms=_KNOWN_PLATFORMS, known_statuses=_KNOWN_STATUSES
    )


async def run_credential_health_refresh_forever(
    session_factory: async_sessionmaker[AsyncSession],
    *,
    stop_event: asyncio.Event,
    interval_seconds: float = _DEFAULT_REFRESH_INTERVAL_SECONDS,
) -> None:
    """Bucle de fondo de `ads-api` (`composition/app.py`, lifespan): mismo
    patron de `stop_event` que `composition/worker.py::run` usa para drenar
    limpio en SIGTERM/SIGINT, sin un `asyncio.sleep` ciego que alargue el
    apagado."""
    while not stop_event.is_set():
        try:
            await _refresh_once(session_factory)
        except Exception as exc:  # noqa: BLE001 - una metrica caida no debe tumbar ads-api
            logger.warning("credential_health_refresh_failed", error=str(exc))
        try:
            await asyncio.wait_for(stop_event.wait(), timeout=interval_seconds)
        except TimeoutError:
            pass


__all__ = ["run_credential_health_refresh_forever"]

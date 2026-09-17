"""Poda periodica de `mcp_oauth` (tasks.md T016, threat-model.md C-42/C-58,
data-model.md "Retencion"): tres barridos SQL directos, sin pasar por los
agregados -- mismo criterio que `composition/credential_health_metrics.py`
(housekeeping agregado, no una transaccion de dominio por fila).

1. Clientes sin consentir (`last_seen_at IS NULL`, nunca marcados TRUSTED
   por `ApproveConsent`) con mas de `UNCONSENTED_CLIENT_TTL` (24 h,
   `application/policy.py`) desde su registro -- C-42. Un cliente CON
   concesion nunca se poda aqui (`ApproveConsent._mark_client_trusted` fija
   `last_seen_at`); se revoca desde el panel (C-55), no se borra.
2. Solicitudes de autorizacion: primero se marcan EXPIRED las PENDING/
   CONSENTED cuyo plazo ya paso (nadie las transiciona solo con el reloj,
   `AuthorizationRequest.is_expired` solo se evalua al tocarlas); despues
   se borran las YA terminales (REDEEMED/DENIED/EXPIRED) mas antiguas que
   `AUTHORIZATION_REQUEST_RETENTION` (7 dias, data-model.md), para dejar
   ventana de auditoria antes de reclamar espacio.
3. Tokens caducados (`oauth_tokens.expires_at < ahora - 30 dias`) --
   `rotated_from` con `ON DELETE SET NULL` evita que borrar uno viejo deje
   una referencia colgante en el siguiente de su cadena de rotacion.
4. Transacciones federadas caducadas (`federated_login_transactions.
   expires_at < ahora`, spec 002b tasks.md T055): de un solo uso y con
   TTL corto (`FEDERATED_TRANSACTION_TTL`, 10 min) -- a diferencia de las
   solicitudes de autorizacion, no necesitan ventana de auditoria propia
   (`NFR-106` ya audita el intento en el registro estructurado, no en esta
   fila); se borran sin retencion adicional, consumidas o no. Mismo
   janitor, ningun proceso nuevo."""

from __future__ import annotations

import asyncio
from dataclasses import asdict, dataclass
from datetime import timedelta

import structlog
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from safent_ads.mcp_oauth.application.policy import UNCONSENTED_CLIENT_TTL
from safent_ads.shared.clock import Clock

logger = structlog.get_logger(__name__)

_DEFAULT_INTERVAL_SECONDS = 3600.0
AUTHORIZATION_REQUEST_RETENTION = timedelta(days=7)
EXPIRED_TOKEN_RETENTION = timedelta(days=30)

# `RETURNING` + contar filas devueltas en vez de `.rowcount`: mas portable
# (`CursorResult` generico no lo tipa para mypy, mismo criterio que
# `crm/infrastructure/sql_repositories.py`) y no depende de un detalle del
# driver.
_EXPIRE_STALE_AUTHORIZATION_REQUESTS_SQL = text("""
    UPDATE oauth_authorization_requests SET state = 'EXPIRED', code_hash = NULL
     WHERE state IN ('PENDING', 'CONSENTED') AND expires_at < :now
    RETURNING txn_id
""")
_DELETE_TERMINAL_AUTHORIZATION_REQUESTS_SQL = text("""
    DELETE FROM oauth_authorization_requests
     WHERE state IN ('REDEEMED', 'DENIED', 'EXPIRED') AND created_at < :cutoff
    RETURNING txn_id
""")
_DELETE_UNCONSENTED_CLIENTS_SQL = text("""
    DELETE FROM oauth_clients WHERE last_seen_at IS NULL AND created_at < :cutoff
    RETURNING client_id
""")
_DELETE_EXPIRED_TOKENS_SQL = text("""
    DELETE FROM oauth_tokens WHERE expires_at < :cutoff
    RETURNING token_hash
""")
_DELETE_EXPIRED_FEDERATED_LOGIN_TRANSACTIONS_SQL = text("""
    DELETE FROM federated_login_transactions WHERE expires_at < :now
    RETURNING state_hash
""")


@dataclass(frozen=True, slots=True, kw_only=True)
class PruneReport:
    expired_authorization_requests: int
    deleted_authorization_requests: int
    deleted_unconsented_clients: int
    deleted_expired_tokens: int
    deleted_federated_login_transactions: int


async def prune_stale_oauth_state(
    session_factory: async_sessionmaker[AsyncSession], *, clock: Clock
) -> PruneReport:
    now = clock.now()
    async with session_factory() as session:
        expired_requests = await session.execute(
            _EXPIRE_STALE_AUTHORIZATION_REQUESTS_SQL, {"now": now}
        )
        deleted_requests = await session.execute(
            _DELETE_TERMINAL_AUTHORIZATION_REQUESTS_SQL,
            {"cutoff": now - AUTHORIZATION_REQUEST_RETENTION},
        )
        deleted_clients = await session.execute(
            _DELETE_UNCONSENTED_CLIENTS_SQL, {"cutoff": now - UNCONSENTED_CLIENT_TTL}
        )
        deleted_tokens = await session.execute(
            _DELETE_EXPIRED_TOKENS_SQL, {"cutoff": now - EXPIRED_TOKEN_RETENTION}
        )
        deleted_federated_login_transactions = await session.execute(
            _DELETE_EXPIRED_FEDERATED_LOGIN_TRANSACTIONS_SQL, {"now": now}
        )
        await session.commit()
    report = PruneReport(
        expired_authorization_requests=len(expired_requests.all()),
        deleted_authorization_requests=len(deleted_requests.all()),
        deleted_unconsented_clients=len(deleted_clients.all()),
        deleted_expired_tokens=len(deleted_tokens.all()),
        deleted_federated_login_transactions=len(deleted_federated_login_transactions.all()),
    )
    logger.info("mcp_oauth_prune_completed", **asdict(report))
    return report


async def run_prune_stale_oauth_state_forever(
    session_factory: async_sessionmaker[AsyncSession],
    *,
    clock: Clock,
    stop_event: asyncio.Event,
    interval_seconds: float = _DEFAULT_INTERVAL_SECONDS,
) -> None:
    """Bucle de fondo de `ads-api` (`composition/app.py`, lifespan) -- mismo
    patron de `stop_event` que `run_credential_health_refresh_forever`:
    drena limpio en SIGTERM/SIGINT, sin un `asyncio.sleep` ciego que alargue
    el apagado."""
    while not stop_event.is_set():
        try:
            await prune_stale_oauth_state(session_factory, clock=clock)
        except Exception as exc:  # noqa: BLE001 - una poda caida no debe tumbar ads-api
            logger.warning("mcp_oauth_prune_failed", error=str(exc))
        try:
            await asyncio.wait_for(stop_event.wait(), timeout=interval_seconds)
        except TimeoutError:
            pass


__all__ = [
    "AUTHORIZATION_REQUEST_RETENTION",
    "EXPIRED_TOKEN_RETENTION",
    "PruneReport",
    "prune_stale_oauth_state",
    "run_prune_stale_oauth_state_forever",
]

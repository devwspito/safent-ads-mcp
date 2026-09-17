"""Auditoria de arranque de D-11 (threat-model.md C-70 pieza 4): que
clientes YA registrados dejarian de valer con la regla de bucle local
exclusivo.

Por que existe: la regla nueva vive en el dominio (`RedirectUri`), asi que
una fila antigua con un destino remoto no se "migra" ni se borra -- se
queda en `oauth_clients` y falla en el momento de autorizar, con el error
de conjunto cerrado que el dueno puede leer. Eso es correcto pero mudo: sin
esta auditoria, el dueno no tiene forma de saber que existe una
registracion muerta hasta que alguien la use.

No toca ni una fila: es un `SELECT`, un `logger.warning` y nada mas. Corre
en una tarea de fondo (`composition/app.py`), nunca en el camino que da
por listo el servicio. Si la BD no esta lista, se aparta -- una auditoria
caida no puede tumbar `ads-api` (mismo criterio que
`prune_stale_clients.run_prune_stale_oauth_state_forever`). El `except` es
ancho A PROPOSITO: los fallos de CONEXION del driver (`asyncpg
.InvalidPasswordError` y compania) no pasan por `sqlalchemy.exc`, asi que
un `except SQLAlchemyError` dejaria escapar justo el caso que importa --
arrancar sin BD detras. Y TODO el trabajo va dentro del `try`, incluido
el recuento: una fila con una URI malformada tampoco puede escaparse
(revision de seguridad, 17-sep).

Una instalacion recien hecha puede arrancar `ads-api` antes de que las
migraciones creen `oauth_clients`: eso no es un fallo que avisar, es una
auditoria que todavia no toca. Se distingue por el SQLSTATE `42P01` y se
anota en DEBUG."""

from __future__ import annotations

import asyncio
from collections.abc import Iterable

import structlog
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from safent_ads.mcp_oauth.domain.client import RedirectUri
from safent_ads.mcp_oauth.domain.errors import InvalidRedirectUriError

logger = structlog.get_logger(__name__)

# Tres topes, para que una auditoria informativa no se convierta en un
# problema: cuantas filas se leen, cuantos `client_id` se escriben en una
# sola linea de registro y cuanto puede tardar la consulta.
_MAX_ROWS_SCANNED = 500
_MAX_CLIENT_IDS_LOGGED = 20
_TIMEOUT_SECONDS = 10.0
_MISSING_TABLE_SQLSTATE = "42P01"

_SELECT_REGISTRATIONS_SQL = text(
    "SELECT client_id, redirect_uris FROM oauth_clients ORDER BY created_at LIMIT :limit"
)


def has_a_rejected_redirect_uri(redirect_uris: Iterable[str]) -> bool:
    """`True` si alguna de las URI ya no pasaria la validacion del
    dominio. Construir `RedirectUri` es la UNICA definicion de "vale" en
    todo el repo (C-9), asi que la auditoria no reimplementa la regla:
    la ejercita, igual que `presentation/loopback_client.py`."""
    for uri in redirect_uris:
        try:
            RedirectUri(uri)
        except InvalidRedirectUriError:
            return True
    return False


async def audit_client_redirect_uris(
    session_factory: async_sessionmaker[AsyncSession],
) -> tuple[str, ...]:
    """Una linea de arranque con los `client_id` que D-11 deja sin poder
    autorizar. Los `redirect_uris` NO se registran: son entrada de un
    tercero (DCR esta abierta a Internet) y el `client_id` basta para
    encontrarlos en la BD."""
    try:
        async with asyncio.timeout(_TIMEOUT_SECONDS), session_factory() as session:
            rows = (
                await session.execute(_SELECT_REGISTRATIONS_SQL, {"limit": _MAX_ROWS_SCANNED})
            ).all()
        offenders = tuple(
            sorted(row.client_id for row in rows if has_a_rejected_redirect_uri(row.redirect_uris))
        )
    except Exception as exc:  # noqa: BLE001 - ver el modulo: el driver no envuelve
        _log_failure(exc)
        return ()
    if offenders:
        logger.warning(
            "mcp_oauth_non_loopback_client_registrations",
            count=len(offenders),
            client_ids=list(offenders[:_MAX_CLIENT_IDS_LOGGED]),
            scan_truncated=len(rows) == _MAX_ROWS_SCANNED,
        )
    return offenders


def _log_failure(exc: BaseException) -> None:
    if _is_a_missing_table(exc):
        logger.debug("mcp_oauth_redirect_uri_audit_skipped", reason="oauth_clients_missing")
        return
    logger.warning(
        "mcp_oauth_redirect_uri_audit_failed", error=str(exc), error_type=type(exc).__name__
    )


def _is_a_missing_table(exc: BaseException) -> bool:
    """Mismo criterio que `sql_grant_repository.py`: el SQLSTATE del
    driver, sin importar `asyncpg` en una capa que no deberia conocerlo.
    SQLAlchemy envuelve el error de consulta y deja el original en
    `.orig`."""
    original = getattr(exc, "orig", exc)
    return str(getattr(original, "sqlstate", "")) == _MISSING_TABLE_SQLSTATE


__all__ = ["audit_client_redirect_uris", "has_a_rejected_redirect_uri"]

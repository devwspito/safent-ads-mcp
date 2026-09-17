"""`get_client_tolerating_domain_violations` (revision de codigo, 17-sep,
nit de f36fb2a6): lectura compartida de `ClientRepository.get_by_id()` que
trata una fila legado que ya no cumple una regla del dominio (destino
remoto de D-11, `client_name` con caracteres bidireccionales de C-70
pieza 3) EXACTAMENTE igual que un cliente inexistente -- `None`.

Antes, `consent_router.py::get_consent`, `StartAuthorization._require_client`
y `ApproveConsent._mark_client_trusted` llamaban a `get_by_id()` sin ningun
`try`: una fila asi dejaba escapar el `DomainError` hasta el manejador
generico (500), en vez del mismo desenlace fail-closed que cada uno ya
sabe dar a "cliente ausente" (404/`UnknownClientError`/saltar el marcado
TRUSTED). Vive en `application/` -- no en `infrastructure/` -- para que
tanto la presentacion (`consent_router.py`) como otros casos de uso puedan
importarlo sin violar "application no importa infrastructure"."""

from __future__ import annotations

import structlog

from safent_ads.mcp_oauth.application.ports import ClientRepository
from safent_ads.mcp_oauth.domain.client import OAuthClient
from safent_ads.shared.errors import DomainError

logger = structlog.get_logger(__name__)

# Nit (code review 17-sep): este `client_id` puede llegar SIN validar
# todavia (es justo lo que esta llamada esta comprobando) -- acotado en
# longitud y restringido a caracteres imprimibles antes de entrar en un
# log estructurado, para que un `client_id` fabricado no infle el
# registro ni cuele un caracter de control (salto de linea, escape ANSI)
# que rompa el parseo de logs aguas abajo.
_MAX_LOGGED_CLIENT_ID_LENGTH = 100


def _safe_log_client_id(client_id: str) -> str:
    truncated = client_id[:_MAX_LOGGED_CLIENT_ID_LENGTH]
    safe = "".join(char if char.isascii() and char.isprintable() else "?" for char in truncated)
    if len(client_id) > _MAX_LOGGED_CLIENT_ID_LENGTH:
        safe += "…"
    return safe


async def get_client_tolerating_domain_violations(
    clients: ClientRepository, client_id: str
) -> OAuthClient | None:
    """`None` tanto si el cliente no existe como si su fila ya no cumple una
    regla del dominio. Solo el TIPO del error entra en el registro (mismo
    criterio que `sql_client_repository.py::_hydrate_for_listing`): el
    mensaje de la excepcion lleva el valor rechazado, que es entrada de un
    tercero y nunca debe aterrizar en un log."""
    try:
        return await clients.get_by_id(client_id)
    except DomainError as exc:
        logger.warning(
            "mcp_oauth_client_rejected_by_the_domain",
            client_id=_safe_log_client_id(client_id),
            error_type=type(exc).__name__,
        )
        return None

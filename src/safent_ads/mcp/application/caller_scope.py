"""Alcance del llamador MCP (data-model.md §4, contracts/mcp.md §4: "denegar
por defecto"; contracts/mcp-tools.md regla 4 y threat-model.md C-9).

Fusion lane/003 + spec 002 (mcp_oauth). `CallerScope` no admite alcance
implicito: `allowed_business_ids` es SIEMPRE un `frozenset[str]` explicito
(vacio = ningun negocio, nunca "todos"), y `permission`/`person_label` los
resuelve quien autentica -- Enterprise (`SeatAdmission`,
`mcp/application/seat_authority.py`), el modo de un solo propietario, o una
concesion OAuth propia (`mcp_oauth`).

`granted_scopes` es lo que anade spec 002: `None` significa "este llamador
no presento un token OAuth" (puesto de Enterprise o bearer estatico del
dueno), donde el `permission` del puesto es la unica puerta; un conjunto
explicito es el alcance concedido por la concesion OAuth y `ToolDispatcher`
lo exige ademas del permiso (`ads:propose` para `ToolClass.PROPOSAL`)."""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
from typing import Protocol


class Permission(StrEnum):
    """Puesto de anuncios (data-model.md §1): `ver`/`proponer`/`aprobar` en
    la interfaz, `view`/`propose`/`approve` en el cable y el codigo."""

    VIEW = "view"
    PROPOSE = "propose"
    APPROVE = "approve"


# Caller id of the installation owner in single-owner mode
# (`ADS_SINGLE_OWNER_MODE`); emitted by SingleOwnerCallerScopeResolver.
OWNER_CALLER_ID = "owner"


@dataclass(frozen=True, slots=True)
class CallerScope:
    """`allowed_business_ids` vacio significa "ningun negocio": no existe
    valor que signifique "todos" (contracts/mcp.md §4). `granted_scopes`
    sigue una convencion propia y mas estrecha (M6 de la revision de
    seguridad, 16-sep): `None` = sin restriccion de alcance, reservado a
    los llamadores que NO vienen de OAuth (puesto de Enterprise o bearer
    estatico del dueno, donde `permission` es la puerta). Un token OAuth
    lleva SIEMPRE un conjunto explicito (threat-model.md C-57)."""

    caller_id: str
    allowed_business_ids: frozenset[str]
    permission: Permission
    person_label: str
    granted_scopes: frozenset[str] | None = None

    def can_access(self, business_id: str) -> bool:
        return business_id in self.allowed_business_ids

    def has_scope(self, scope: str) -> bool:
        return self.granted_scopes is None or scope in self.granted_scopes


class CallerScopeResolverPort(Protocol):
    """Resuelve el bearer presentado a un `CallerScope`. En produccion la
    cadena es `OAuthCallerScopeResolver` (`mcp_oauth/presentation/
    caller_scope.py`, spec 002) delante de
    `EnterpriseSeatCallerScopeResolver` o `SingleOwnerCallerScopeResolver`
    (`mcp/infrastructure/`): primero se intenta la concesion OAuth, y si el
    token no es una concesion viva se delega en el resolutor de puesto."""

    async def resolve(self, bearer_token: str) -> CallerScope: ...


class QuotaPort(Protocol):
    """Cuota por minuto y llamador (threat-model.md C-23, contracts/mcp.md
    §6). Devuelve `True` si la llamada esta dentro de cuota (y la
    consume); `False` si no. `tool_class` viaja como el `.value` de
    `mcp.presentation.registry.ToolClass` (`str`, nunca el enum): este
    puerto vive en `application` y no importa un tipo de `presentation`
    (SOLID -- depender de una abstraccion, no de la capa de arriba)."""

    async def check_and_consume(
        self, *, caller_id: str, tool_name: str, tool_class: str
    ) -> bool: ...


class IntrospectionQuotaExceededError(Exception):
    """Cupo de introspecciones por persona agotado (contracts/mcp.md §6):
    protege a Enterprise de un cliente en bucle sin gastar la llamada. La
    clave del contador es un digest de la credencial -- nunca la
    credencial, nunca el `user_id`, que todavia no se conoce en este
    punto de `EnterpriseSeatCallerScopeResolver.resolve`."""

    def __init__(self, retry_after_seconds: int) -> None:
        super().__init__("cupo de introspecciones agotado")
        self.retry_after_seconds = retry_after_seconds

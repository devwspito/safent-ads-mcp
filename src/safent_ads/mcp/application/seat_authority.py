"""Puerto de admision por puesto (data-model.md §3 `PuestoDeAnuncios`,
contracts/mcp.md §2). `SeatAdmission` es la traduccion ya sanitizada de la
respuesta de Enterprise -- nunca un dict crudo, nunca la credencial.

Puro: sin `httpx`, sin nada de `iam/infrastructure` (A2 acceptance,
tasks.md). La implementacion real (`iam/infrastructure/
enterprise_seat_authority.py`) vive fuera de este modulo; `mcp/testing/
fakes.py` cablea un doble configurable para que A3-A6 avancen sin
Enterprise."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol

from safent_ads.mcp.application.caller_scope import Permission


class SeatAuthorityDeniedError(Exception):
    """Credencial invalida, caducada, revocada o sin puesto activo. Nunca
    lleva el valor de la credencial ni el detalle de origen
    (contracts/mcp.md §5: `401 UNAUTHORIZED`)."""


class SeatAuthorityUnavailableError(Exception):
    """Enterprise no responde, TLS, 5xx, o la respuesta no valida contra el
    modelo cerrado. Nunca degrada a un alcance por defecto
    (contracts/mcp.md §5: `503 SEAT_AUTHORITY_UNAVAILABLE`)."""


@dataclass(frozen=True, slots=True)
class SeatAdmission:
    """Principal de una admision (contracts/enterprise-api.md §4 `principal`),
    valida solo para la peticion que la resolvio -- nunca se cachea
    (contracts/mcp.md §2: "Prohibido cachear la admision")."""

    org_id: str
    user_id: str
    person_label: str
    seat_id: str
    business_id: str
    permission: Permission
    expires_at: int


class SeatAuthorityPort(Protocol):
    """Resuelve una credencial de puesto (`sfa_<64 hex>`) contra Enterprise.
    Lanza `SeatAuthorityDeniedError`/`SeatAuthorityUnavailableError`; nunca
    devuelve una admision con datos parciales o por defecto."""

    async def resolve(self, credential: str) -> SeatAdmission: ...

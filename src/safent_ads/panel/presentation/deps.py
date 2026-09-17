"""`require_business_access` (T046, integracion): dependencia FastAPI, no
disciplina por handler (contracts/rest-api.md: "RequireBusinessAccess es
dependencia FastAPI en cada router... la barrida IDOR de oposads demostro
que la disciplina falla").

Cablea la sesion real de `iam` (`current_owner`: cookie `ads_session` ->
`Session` -> `Owner`, 401 si falta o caduco). Modelo de propietario unico
(data-model.md, `iam.infrastructure.sql_business_directory`): no hay tabla
de asignacion propietario-negocio -- "poseer" un negocio es que la fila
exista. `AuthenticatedCaller.allowed_business_ids=None` reflej eso (todo
negocio del propietario autenticado); la comprobacion que de verdad falla
cerrado es `require_business_access` contra `SqlBusinessDirectory.exists`,
nunca solo la posesion de un token valido."""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from typing import Annotated

from fastapi import Depends, HTTPException, Request

from safent_ads.composition.container import Container
from safent_ads.iam.infrastructure.sql_business_directory import SqlBusinessDirectory
from safent_ads.iam.presentation.dependencies import CURRENT_OWNER, AuthenticatedOwner

_NOT_FOUND = HTTPException(status_code=404, detail="No encontrado")


@dataclass(frozen=True, slots=True)
class AuthenticatedCaller:
    """Sesion real de `iam` ya resuelta. `allowed_business_ids=None` = todo
    negocio del propietario autenticado (unico tenant, igual que
    `mcp.CallerScope`) -- el aislamiento entre negocios lo prueba
    `test_idor_sweep` inyectando un caller restringido via
    `app.dependency_overrides`, y la existencia real la comprueba
    `require_business_access` contra la base."""

    allowed_business_ids: frozenset[str] | None

    def can_access(self, business_id: str) -> bool:
        return self.allowed_business_ids is None or business_id in self.allowed_business_ids


async def get_authenticated_caller(
    _owner: AuthenticatedOwner = CURRENT_OWNER,
) -> AuthenticatedCaller:
    return AuthenticatedCaller(allowed_business_ids=None)


CallerDep = Annotated[AuthenticatedCaller, Depends(get_authenticated_caller)]


async def require_business_access(business_id: str, request: Request, caller: CallerDep) -> str:
    """Para rutas con `business_id` como query param directo: ademas del
    alcance del caller, comprueba contra `SqlBusinessDirectory` que la fila
    existe -- fail closed, nunca confia solo en el token de sesion."""
    ensure_business_access(business_id, caller)
    await _ensure_business_exists(business_id, request)
    return business_id


def ensure_business_access(business_id: str, caller: AuthenticatedCaller) -> None:
    """Para rutas con `entity_ref`/`signal_id`: el handler resuelve el
    `business_id` dueño del recurso primero (puede no existir -> 404) y
    llama esto antes de devolver el cuerpo (mismo 404, nunca 403, para no
    filtrar existencia entre negocios). La entidad ya fue resuelta contra
    la base por el propio puerto de lectura, asi que no repite la consulta
    de existencia de negocio aqui."""
    if not caller.can_access(business_id):
        raise _NOT_FOUND


async def _ensure_business_exists(business_id: str, request: Request) -> None:
    try:
        parsed = uuid.UUID(business_id)
    except ValueError as exc:
        raise _NOT_FOUND from exc

    container: Container = request.app.state.container
    async with container.session_factory() as db_session:
        exists = await SqlBusinessDirectory(db_session).exists(parsed)
    if not exists:
        raise _NOT_FOUND

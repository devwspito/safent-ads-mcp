"""`build_grants_router` (tasks.md T015b, threat-model.md C-55): la seccion
«Agentes conectados» del panel -- listar y revocar concesiones OAuth vivas
del propietario. `RevokeGrant` corta la familia entera (acceso + refresco);
la proxima llamada a `/mcp`/`/mcp/health` con ese token cae en
`IntrospectToken` (`Grant.is_token_valid`: `revoked_at IS NOT NULL`), sin
cache de por medio -- se comprueba en cada llamada.

002b (tasks.md T060, contracts/federated-login.md §2, decision 4 del
dueno): `revoke` exige identificacion FRESCA (`X-Reauth-Token` **o**, si el
login federado esta activo, una identificacion reciente ante Google --
`iam/presentation/fresh_identification.py`) y **ademas**
`require_action_confirmation` -- el mismo dialogo "Revisa y confirma esta
accion" del resto del panel (lane/003). El 401 de frescura llega SIEMPRE
antes que el 428 de confirmacion: sin frescura, `require_action_confirmation`
ni se invoca. A diferencia de `consent_router.py::approve` (que NO gana
`require_action_confirmation`, decision 4), revocar es una accion
destructiva que corta el acceso de un agente entero -- la doble prueba es
incondicional, no depende del interruptor federado.

C-82 (T064 security review): "la marca muere con su metodo". Revocar es
destructiva -- la presencia que la autorizo se trata como de un solo uso.
Tras una revocacion CON EXITO, `last_federated_auth_at` se invalida en
TODAS las sesiones del dueno (no solo la que hizo esta llamada: dos
pestanas no deben dejarse una marca fresca a la otra). No siempre es un
`NULL` literal: `sessions_federated_origin_check` (0052) exige que una
sesion `origin='federated'` conserve un valor no nulo ahi -- para esas se
empuja a un instante deliberadamente antiguo (`_STALE_MARK_OFFSET`, muy
por delante de `FEDERATED_IDENTIFICATION_TTL`), que `has_fresh_federated_
identification` nunca vuelve a contar como fresco; las demas (password/
bridge) sí quedan en `NULL` liso. La evidencia TOTP (C-81) ya es de un
solo uso por construccion: vive por `(owner_id, action_hash)`, y el
`action_hash` de ESTA revocacion (que incluye el `grant_id`) nunca sirve
para otra accion."""

from __future__ import annotations

import hashlib
import uuid
from datetime import timedelta

from fastapi import APIRouter, Request
from sqlalchemy import text
from starlette import status

from safent_ads.composition.container import Container
from safent_ads.iam.presentation.action_confirmation import require_action_confirmation
from safent_ads.iam.presentation.dependencies import CURRENT_OWNER, AuthenticatedOwner
from safent_ads.iam.presentation.errors import ApiError
from safent_ads.iam.presentation.fresh_identification import require_fresh_identification
from safent_ads.mcp_oauth.application.errors import GrantNotFoundError
from safent_ads.mcp_oauth.application.list_grants import ConnectedGrant, ListGrants
from safent_ads.mcp_oauth.application.revoke_grant import RevokeGrant
from safent_ads.mcp_oauth.infrastructure.sql_client_repository import SqlClientRepository
from safent_ads.mcp_oauth.infrastructure.sql_grant_repository import SqlGrantRepository
from safent_ads.mcp_oauth.presentation.payloads import GrantsListResponse, GrantSummaryResponse

_ROUTER_PREFIX = "/api/v1/mcp-oauth/grants"

# data-model.md "Desviaciones aplicadas en 0035" #8: vocabulario cerrado de
# `revoked_reason` -- esta es la razon que corresponde a ESTE boton.
_PANEL_REVOCATION_REASON = "panel"

# Muy por delante de `FEDERATED_IDENTIFICATION_TTL` (5 min): garantiza que
# `has_fresh_federated_identification` nunca vuelva a contarla fresca,
# cualquiera que sea `now` cuando se lea.
_STALE_MARK_OFFSET = timedelta(days=3650)

_INVALIDATE_FEDERATED_MARK_SQL = text("""
    UPDATE sessions
       SET last_federated_auth_at = CASE
               WHEN origin = 'federated' THEN CAST(:stale_at AS TIMESTAMPTZ)
               ELSE NULL
           END
     WHERE owner_id = :owner_id
""")


def build_grants_router(*, totp_enc_key: str, federated_available: bool = False) -> APIRouter:
    router = APIRouter(prefix=_ROUTER_PREFIX, tags=["mcp-oauth-grants"])

    @router.get("", response_model=GrantsListResponse)
    async def list_grants(
        request: Request, owner: AuthenticatedOwner = CURRENT_OWNER
    ) -> GrantsListResponse:
        container: Container = request.app.state.container
        async with container.session_factory() as db_session:
            use_case = ListGrants(
                grants=SqlGrantRepository(db_session), clients=SqlClientRepository(db_session)
            )
            connected_grants = await use_case.execute(owner_id=owner.owner_id)
        return GrantsListResponse(grants=[_to_summary(grant) for grant in connected_grants])

    @router.post("/{grant_id}/revoke", status_code=status.HTTP_204_NO_CONTENT)
    async def revoke_grant(
        grant_id: uuid.UUID, request: Request, owner: AuthenticatedOwner = CURRENT_OWNER
    ) -> None:
        container: Container = request.app.state.container
        async with container.session_factory() as db_session:
            action_hash = _revoke_action_hash(grant_id)
            await require_fresh_identification(
                request,
                db_session,
                owner,
                action_hash=action_hash,
                totp_enc_key=totp_enc_key,
                clock=container.clock,
                federated_available=federated_available,
            )
            await require_action_confirmation(
                request, db_session, owner, action_hash=action_hash, clock=container.clock
            )
            use_case = RevokeGrant(grants=SqlGrantRepository(db_session), clock=container.clock)
            try:
                await use_case.execute(
                    grant_id=grant_id, owner_id=owner.owner_id, reason=_PANEL_REVOCATION_REASON
                )
            except GrantNotFoundError as exc:
                raise _not_found() from exc
            # C-82: la revocacion, si llega aqui, tuvo exito -- la presencia
            # que la autorizo no sobrevive a la accion destructiva que acaba
            # de confirmar.
            await db_session.execute(
                _INVALIDATE_FEDERATED_MARK_SQL,
                {
                    "owner_id": str(owner.owner_id),
                    "stale_at": container.clock.now() - _STALE_MARK_OFFSET,
                },
            )
            await db_session.commit()

    return router


def _revoke_action_hash(grant_id: uuid.UUID) -> str:
    return hashlib.sha256(f"revoke|{grant_id}".encode()).hexdigest()


def _not_found() -> ApiError:
    return ApiError(status_code=404, code="NOT_FOUND", message="Concesión no encontrada.")


def _to_summary(grant: ConnectedGrant) -> GrantSummaryResponse:
    return GrantSummaryResponse(
        grant_id=grant.grant_id,
        client_id=grant.client_id,
        client_name=grant.client_name,
        redirect_host=grant.redirect_host,
        scopes=sorted(scope.value for scope in grant.scopes.scopes),
        created_at=grant.created_at,
        expires_at=grant.expires_at,
        last_used_at=grant.last_used_at,
    )

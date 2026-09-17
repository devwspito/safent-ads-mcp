"""`build_consent_router` (tasks.md T015/T015+, contracts/oauth.md SS5/SS9):
`/api/v1/mcp-oauth/consent/*` -- la pantalla de consentimiento del panel
lee y resuelve aqui la `AuthorizationRequest` PENDING que `GET /authorize`
creo (`sdk_provider.py::authorize`).

Bajo `/api/v1`: viaja la cookie `ads_session` (`path=/api`) y el CSRF de
doble envio del resto del panel -- este prefijo NUNCA entra
en `_CSRF_EXEMPT_PREFIXES` (`composition/api.py`, C-40/C-52). `approve`
exige ademas identificacion fresca (`X-Reauth-Token` **o**, si el login
federado esta activo, una identificacion reciente ante Google --
`iam/presentation/fresh_identification.py`, 002b research.md Decision B,
threat-model.md C-71); `deny` no, porque denegar no concede nada. Esta
pantalla ES la revision del consentimiento (C-39): no gana ademas
`require_action_confirmation` (002b research.md Decision B, decision 4 del
dueno)."""

from __future__ import annotations

import hashlib
import uuid
from datetime import datetime
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

from fastapi import APIRouter, Request

from safent_ads.composition.container import Container
from safent_ads.iam.presentation.dependencies import CURRENT_OWNER, AuthenticatedOwner
from safent_ads.iam.presentation.errors import ApiError
from safent_ads.iam.presentation.fresh_identification import require_fresh_identification
from safent_ads.mcp_oauth.application.client_lookup import get_client_tolerating_domain_violations
from safent_ads.mcp_oauth.application.errors import UnknownClientError
from safent_ads.mcp_oauth.application.grant_consent import ApproveConsent, DenyConsent
from safent_ads.mcp_oauth.application.ports import OpaqueTokenFactory, TokenHasher
from safent_ads.mcp_oauth.domain.authorization import (
    AuthorizationRequest,
    AuthorizationRequestState,
)
from safent_ads.mcp_oauth.domain.client import OAuthClient
from safent_ads.mcp_oauth.domain.errors import (
    AuthorizationRequestExpiredError,
    AuthorizationRequestNotPendingError,
)
from safent_ads.mcp_oauth.domain.scope import Scope, ScopeSet
from safent_ads.mcp_oauth.infrastructure.sql_authorization_repository import (
    SqlAuthorizationRequestRepository,
)
from safent_ads.mcp_oauth.infrastructure.sql_client_repository import SqlClientRepository
from safent_ads.mcp_oauth.presentation.payloads import (
    ConsentDetailResponse,
    RedirectResponse,
    ScopeDescriptor,
)

_ROUTER_PREFIX = "/api/v1/mcp-oauth/consent"

# contracts/oauth.md SS9: copy de respaldo -- `panel/src/utils/
# mcpOauthScopes.ts` es la fuente, esto solo cubre el caso de que la SPA no
# tenga su propia traduccion todavia.
_SCOPE_LABELS: dict[Scope, str] = {
    Scope.READ: "Leer tu cartera, señales y registro",
    Scope.PROPOSE: "Crear propuestas (siguen necesitando tu aprobación)",
}
_SCOPE_DISPLAY_ORDER: tuple[Scope, ...] = (Scope.READ, Scope.PROPOSE)


def build_consent_router(
    *,
    token_hasher: TokenHasher,
    token_factory: OpaqueTokenFactory,
    totp_enc_key: str,
    public_base_url: str,
    federated_available: bool = False,
) -> APIRouter:
    router = APIRouter(prefix=_ROUTER_PREFIX, tags=["mcp-oauth-consent"])

    @router.get("/{txn_id}", response_model=ConsentDetailResponse)
    async def get_consent(
        txn_id: uuid.UUID,
        request: Request,
        _owner: AuthenticatedOwner = CURRENT_OWNER,
    ) -> ConsentDetailResponse:
        container: Container = request.app.state.container
        async with container.session_factory() as db_session:
            authorization_request = await _require_visible_request(
                SqlAuthorizationRequestRepository(db_session, clock=container.clock),
                txn_id,
                container.clock.now(),
            )
            client = await get_client_tolerating_domain_violations(
                SqlClientRepository(db_session), authorization_request.client_id
            )
        return _to_consent_response(txn_id, authorization_request, client)

    @router.post("/{txn_id}/approve", response_model=RedirectResponse)
    async def approve_consent(
        txn_id: uuid.UUID,
        request: Request,
        owner: AuthenticatedOwner = CURRENT_OWNER,
    ) -> RedirectResponse:
        container: Container = request.app.state.container
        async with container.session_factory() as db_session:
            requests_repo = SqlAuthorizationRequestRepository(db_session, clock=container.clock)
            authorization_request = await _require_known_request(requests_repo, txn_id)
            await require_fresh_identification(
                request,
                db_session,
                owner,
                action_hash=_consent_action_hash(txn_id, authorization_request),
                totp_enc_key=totp_enc_key,
                clock=container.clock,
                federated_available=federated_available,
            )
            use_case = ApproveConsent(
                authorization_requests=requests_repo,
                clients=SqlClientRepository(db_session),
                token_hasher=token_hasher,
                token_factory=token_factory,
                clock=container.clock,
            )
            try:
                approved = await use_case.execute(txn_id=txn_id, owner_id=owner.owner_id)
            except AuthorizationRequestNotPendingError as exc:
                raise _txn_not_pending() from exc
            except AuthorizationRequestExpiredError as exc:
                raise _txn_expired() from exc
            except UnknownClientError as exc:
                # I-2 (revision de seguridad 17-sep): `ApproveConsent`
                # rechaza en vez de tolerar una fila de cliente que viola
                # el dominio -- mismo cuerpo que un `txn_id` inexistente,
                # nunca un 500.
                raise _not_found() from exc
            await db_session.commit()
        redirect_to = _build_redirect(
            approved.redirect_uri,
            params=_approval_params(code=approved.code, state=approved.client_state),
            iss=public_base_url,
        )
        return RedirectResponse(redirect_to=redirect_to)

    @router.post("/{txn_id}/deny", response_model=RedirectResponse)
    async def deny_consent(
        txn_id: uuid.UUID,
        request: Request,
        _owner: AuthenticatedOwner = CURRENT_OWNER,
    ) -> RedirectResponse:
        container: Container = request.app.state.container
        async with container.session_factory() as db_session:
            requests_repo = SqlAuthorizationRequestRepository(db_session, clock=container.clock)
            # 404 explicito antes del caso de uso: `DenyConsent.execute()`
            # lanza `AuthorizationRequestNotFoundError` (aplicacion), que
            # sin traducir aqui llegaria como 500, no 404.
            await _require_known_request(requests_repo, txn_id)
            use_case = DenyConsent(authorization_requests=requests_repo, clock=container.clock)
            try:
                denied = await use_case.execute(txn_id=txn_id)
            except AuthorizationRequestNotPendingError as exc:
                raise _txn_not_pending() from exc
            except AuthorizationRequestExpiredError as exc:
                raise _txn_expired() from exc
            await db_session.commit()
        redirect_to = _build_redirect(
            denied.redirect_uri,
            params=_denial_params(state=denied.client_state),
            iss=public_base_url,
        )
        return RedirectResponse(redirect_to=redirect_to)

    return router


async def _require_known_request(
    requests_repo: SqlAuthorizationRequestRepository, txn_id: uuid.UUID
) -> AuthorizationRequest:
    """404 `ApiError` (nunca la excepcion de aplicacion sin traducir) si el
    `txn_id` no existe -- lo comparten `approve`/`deny`/`GET`."""
    authorization_request = await requests_repo.get_by_id(txn_id)
    if authorization_request is None:
        raise _not_found()
    return authorization_request


async def _require_visible_request(
    requests_repo: SqlAuthorizationRequestRepository, txn_id: uuid.UUID, now: datetime
) -> AuthorizationRequest:
    """contracts/oauth.md SS9: 404 desconocida, 410 caducada/resuelta --
    ambas colapsan a los mismos dos codigos en `GET`, a diferencia de
    `approve`/`deny` (404/409/410, contracts/oauth.md SS5), que la SPA trata
    igual («solicitud caducada»)."""
    authorization_request = await _require_known_request(requests_repo, txn_id)
    if authorization_request.state is not AuthorizationRequestState.PENDING:
        raise _txn_expired()
    if authorization_request.is_expired(now):
        raise _txn_expired()
    return authorization_request


def _consent_action_hash(txn_id: uuid.UUID, request: AuthorizationRequest) -> str:
    """threat-model.md C-41: ata el TOTP quemado a la transaccion EXACTA
    que confirma -- client_id/redirect_uri/scopes vienen de la fila
    guardada, nunca de lo que mande la peticion (C-39)."""
    scopes = " ".join(sorted(scope.value for scope in request.scope_set.scopes))
    raw = f"{txn_id}|{request.client_id}|{request.redirect_uri}|{scopes}"
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def _approval_params(*, code: str, state: str | None) -> list[tuple[str, str]]:
    params = [("code", code)]
    if state is not None:
        params.append(("state", state))
    return params


def _denial_params(*, state: str | None) -> list[tuple[str, str]]:
    params = [("error", "access_denied")]
    if state is not None:
        params.append(("state", state))
    return params


def _build_redirect(redirect_uri: str, *, params: list[tuple[str, str]], iss: str) -> str:
    """RFC 9207: `iss` va en TODA redireccion de vuelta, tambien en la de
    denegacion -- mitiga la confusion de authorization server cuando el
    cliente habla con mas de uno. La URL se construye solo con lo que la
    fila `AuthorizationRequest` ya fijo (C-39): nunca con un parametro de
    esta peticion."""
    all_params = [*params, ("iss", iss)]
    parsed = urlsplit(redirect_uri)
    combined_query = parse_qsl(parsed.query, keep_blank_values=True) + all_params
    return urlunsplit((parsed.scheme, parsed.netloc, parsed.path, urlencode(combined_query), ""))


def _scope_descriptors(scope_set: ScopeSet) -> list[ScopeDescriptor]:
    return [
        ScopeDescriptor(name=scope.value, label=_SCOPE_LABELS[scope])
        for scope in _SCOPE_DISPLAY_ORDER
        if scope_set.contains(scope)
    ]


def _to_consent_response(
    txn_id: uuid.UUID, request: AuthorizationRequest, client: OAuthClient | None
) -> ConsentDetailResponse:
    return ConsentDetailResponse(
        txn_id=txn_id,
        client_id=request.client_id,
        client_name=client.client_name if client is not None else request.client_id,
        redirect_host=urlsplit(request.redirect_uri).netloc,
        scopes=_scope_descriptors(request.scope_set),
        expires_at=request.expires_at,
    )


def _not_found() -> ApiError:
    return ApiError(status_code=404, code="NOT_FOUND", message="Solicitud no encontrada.")


def _txn_expired() -> ApiError:
    return ApiError(status_code=410, code="TXN_EXPIRED", message="La solicitud ha caducado.")


def _txn_not_pending() -> ApiError:
    return ApiError(
        status_code=409, code="TXN_NOT_PENDING", message="La solicitud ya se resolvió."
    )

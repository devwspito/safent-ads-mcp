"""`build_federated_router` (spec 002b contracts/federated-login.md §1):
`GET /status`, `POST /start`, `GET /callback` under
`/api/v1/auth/federated`. Only ever mounted when
`settings.federated_login_active` (composition, tasks.md T033/T036) --
these three routes answer 404 by NOT EXISTING when the switch is off,
never by a branch inside a handler (research.md Decision F).

The callback is a GET **with effects**, reached anonymously from
`accounts.google.com`, and reflects nothing it received: it ALWAYS answers
`303` and derives its `Location` solely from what the stored transaction
row says (`txn_id`), never from a query parameter (threat-model.md C-69).
Every failure path -- known or not -- ends in `federated_error=<code>`
with zero effects: no session, no owner, no bound identity, no freshness
mark (banderas rojas, threat-model.md §5).

NFR-106/SC-107 (002b tasks.md T080): every attempt -- success or failure
-- emits ONE structured `federated_login_*` event with outcome and reason,
never a credential nor a plain email: no `client_secret`, `code`,
`id_token`, `state`, `nonce` or session token ever becomes a log field or
lands inside an f-string passed to the logger."""

from __future__ import annotations

import uuid

import structlog
from fastapi import APIRouter, Request, Response
from sqlalchemy.ext.asyncio import AsyncSession
from starlette import status
from starlette.responses import RedirectResponse

from safent_ads.composition.container import Container
from safent_ads.iam.application.consume_federated_transaction import ConsumeFederatedTransaction
from safent_ads.iam.application.errors import (
    ConsentTransactionNotFoundError,
    ConsentTransactionNotOpenError,
    FederatedIdentityMismatchError,
    FederatedIdentityNotAuthorizedError,
    FederatedTransactionInvalidError,
    OwnerBoundElsewhereError,
    TooManyPendingFederatedTransactionsError,
)
from safent_ads.iam.application.ports import (
    AuthorizedEmailList,
    FederatedIdentityClaims,
    FederatedIdentityProvider,
    OpenConsentTransactions,
)
from safent_ads.iam.application.resolve_federated_login import (
    CompletedFederatedLogin,
    FederatedCallbackFailed,
    FederatedCallbackFailureContext,
    FederatedSessionIssued,
    ProviderDeniedConsent,
    ResolveFederatedLogin,
    exchange_federated_code,
)
from safent_ads.iam.application.start_federated_login import (
    StartedFederatedLogin,
    StartFederatedLogin,
)
from safent_ads.iam.domain.federated_transaction import (
    FederatedLoginTransaction,
    TransactionPurpose,
)
from safent_ads.iam.infrastructure.google_oidc_provider import GoogleOidcError
from safent_ads.iam.infrastructure.sql_federated_transaction_repository import (
    SqlFederatedTransactionRepository,
)
from safent_ads.iam.infrastructure.sql_login_attempt_repository import SqlLoginAttemptRepository
from safent_ads.iam.infrastructure.sql_owner_federated_identity_repository import (
    SqlOwnerFederatedIdentityRepository,
)
from safent_ads.iam.infrastructure.sql_session_repository import SqlSessionRepository
from safent_ads.iam.presentation.dependencies import current_session, set_session_cookie
from safent_ads.iam.presentation.errors import ApiError
from safent_ads.iam.presentation.schemas import (
    FederatedStartRequest,
    FederatedStartResponse,
    FederatedStatusResponse,
)
from safent_ads.shared.net.client_ip import resolve_client_ip

logger = structlog.get_logger(__name__)

_ROUTER_PREFIX = "/api/v1/auth/federated"
_LOGIN_ERROR_LOCATION = "/login?federated_error={code}"


def build_federated_router(
    *,
    provider: FederatedIdentityProvider,
    redirect_uri: str,
    allowed_emails: AuthorizedEmailList,
    open_consent_transactions: OpenConsentTransactions,
    trusted_proxy_hops: int,
) -> APIRouter:
    router = APIRouter(prefix=_ROUTER_PREFIX, tags=["auth-federated"])

    @router.get("/status", response_model=FederatedStatusResponse)
    async def status_endpoint(response: Response) -> FederatedStatusResponse:
        response.headers["Cache-Control"] = "no-store"
        return FederatedStatusResponse(available=True)

    @router.post("/start", response_model=FederatedStartResponse)
    async def start(payload: FederatedStartRequest, request: Request) -> FederatedStartResponse:
        if payload.txn_id is not None:
            await _require_open_consent_transaction(open_consent_transactions, payload.txn_id)
        container: Container = request.app.state.container
        async with container.session_factory() as db_session:
            session_id = await _reidentify_session_id(request, db_session)
            use_case = StartFederatedLogin(
                provider=provider,
                transactions=SqlFederatedTransactionRepository(db_session),
                clock=container.clock,
                redirect_uri=redirect_uri,
            )
            started = await _start_federated_login(
                use_case,
                txn_id=payload.txn_id,
                session_id=session_id,
                request=request,
                trusted_proxy_hops=trusted_proxy_hops,
            )
        return FederatedStartResponse(
            authorization_url=started.authorization_url, expires_at=started.expires_at
        )

    @router.get("/callback", include_in_schema=False)
    async def callback(request: Request) -> RedirectResponse:
        return await _handle_callback(
            request,
            provider=provider,
            allowed_emails=allowed_emails,
            redirect_uri=redirect_uri,
            trusted_proxy_hops=trusted_proxy_hops,
        )

    return router


async def _reidentify_session_id(request: Request, db_session: AsyncSession) -> uuid.UUID | None:
    """El proposito lo decide el SERVIDOR (research.md Decision C): con
    cookie de sesion viva, re-identifica; sin ella -- ausente, invalida,
    revocada o caducada --, entra. Un fallo aqui nunca es un error para el
    llamador: solo cambia el proposito del salto."""
    try:
        session = await current_session(request, db_session)
    except ApiError:
        return None
    return session.id


async def _start_federated_login(
    use_case: StartFederatedLogin,
    *,
    txn_id: uuid.UUID | None,
    session_id: uuid.UUID | None,
    request: Request,
    trusted_proxy_hops: int,
) -> StartedFederatedLogin:
    """threat-model.md C-79: mismo cuerpo de denegacion que el resto de
    `/api/v1/auth` (`ACCOUNT_LOCKED`, 429) -- ninguna forma nueva, solo un
    motivo nuevo para la misma."""
    try:
        return await use_case.execute(
            txn_id=txn_id,
            session_id=session_id,
            ip_address=_client_ip(request, trusted_proxy_hops=trusted_proxy_hops),
        )
    except TooManyPendingFederatedTransactionsError as exc:
        raise ApiError(
            status_code=429, code="ACCOUNT_LOCKED", message="Demasiados intentos recientes."
        ) from exc


async def _require_open_consent_transaction(
    open_consent_transactions: OpenConsentTransactions, txn_id: uuid.UUID
) -> None:
    """T085 (plan.md "mcp_oauth -> iam, nunca al reves"): `iam` pide la
    respuesta al puerto sin saber que la fila real es una `mcp_oauth.
    AuthorizationRequest` -- eso lo sabe la implementacion que `composition/
    federated_routes.py` inyecta. El contrato HTTP no cambia (404/410)."""
    try:
        await open_consent_transactions.require_open(txn_id)
    except ConsentTransactionNotFoundError as exc:
        raise ApiError(
            status_code=404, code="NOT_FOUND", message="Solicitud no encontrada."
        ) from exc
    except ConsentTransactionNotOpenError as exc:
        raise ApiError(
            status_code=410, code="TXN_EXPIRED", message="La solicitud ha caducado."
        ) from exc


_ERROR_CODE_FOR_EXCEPTION: tuple[tuple[type[Exception], str], ...] = (
    (FederatedTransactionInvalidError, "expired"),
    (FederatedIdentityMismatchError, "identity_mismatch"),
    (OwnerBoundElsewhereError, "owner_already_bound"),
    (GoogleOidcError, "provider_unavailable"),
)


async def _handle_callback(
    request: Request,
    *,
    provider: FederatedIdentityProvider,
    allowed_emails: AuthorizedEmailList,
    redirect_uri: str,
    trusted_proxy_hops: int,
) -> RedirectResponse:
    state = request.query_params.get("state")
    code = request.query_params.get("code")
    error = request.query_params.get("error")
    if not state or (code is None) == (error is None):
        _log_attempt("expired", context=None, reason="malformed_callback_request")
        return _redirect_to_login_error("expired")

    container: Container = request.app.state.container
    try:
        outcome = await _run_callback_phases(
            container,
            provider=provider,
            allowed_emails=allowed_emails,
            redirect_uri=redirect_uri,
            state=state,
            code=code,
        )
    except Exception as exc:  # noqa: BLE001 - threat-model.md C-75: anonymous callback with effects, fail closed, never a 500
        cause, context = _unwrap(exc)
        if isinstance(cause, FederatedIdentityNotAuthorizedError):
            await _record_denied_attempt(
                container, request, cause, trusted_proxy_hops=trusted_proxy_hops
            )
            _log_attempt("denied", context=context, reason=cause.reason.value)
            return _redirect_to_error("denied", context)
        failure_code = _classify_failure(cause)
        _log_attempt(failure_code, context=context, reason=type(cause).__name__)
        return _redirect_to_error(failure_code, context)

    if isinstance(outcome, ProviderDeniedConsent):
        context = FederatedCallbackFailureContext(purpose=outcome.purpose, txn_id=outcome.txn_id)
        _log_attempt("denied", context=context, reason="provider_returned_error")
        return _redirect_to_error("denied", context)
    _log_success(outcome)
    return _redirect_for_outcome(outcome)


def _unwrap(exc: Exception) -> tuple[Exception, FederatedCallbackFailureContext | None]:
    """`exchange_federated_code`/`ResolveFederatedLogin.execute()` wrap any
    post-consume failure in `FederatedCallbackFailed` (T064, revision de
    codigo): unwrap it here so the rest of this module keeps classifying by
    the ORIGINAL exception type. A failure raised before the reference is
    consumed (unknown/expired state) arrives unwrapped, with no context to
    attach."""
    if isinstance(exc, FederatedCallbackFailed):
        return exc.cause, exc.context
    return exc, None


def _classify_failure(exc: Exception) -> str:
    for exception_type, code in _ERROR_CODE_FOR_EXCEPTION:
        if isinstance(exc, exception_type):
            return code
    logger.exception("federated_login_callback_failed", exc_info=exc)
    return "provider_unavailable"


def _log_attempt(
    outcome: str, *, context: FederatedCallbackFailureContext | None, reason: str
) -> None:
    """NFR-106/SC-107: `reason` is always a code name (an enum value or an
    exception type name), never a message string built from provider or
    request data -- the one thing that could smuggle a credential into a
    log field."""
    logger.info(
        "federated_login_attempt_failed",
        outcome=outcome,
        purpose=(context.purpose.value if context is not None else None),
        reason=reason,
    )


def _log_success(outcome: CompletedFederatedLogin) -> None:
    logger.info(
        "federated_login_attempt_succeeded",
        outcome=outcome.outcome.value,
        resolution=(
            outcome.resolution.value if isinstance(outcome, FederatedSessionIssued) else None
        ),
    )


async def _run_callback_phases(
    container: Container,
    *,
    provider: FederatedIdentityProvider,
    allowed_emails: AuthorizedEmailList,
    redirect_uri: str,
    state: str,
    code: str | None,
) -> CompletedFederatedLogin | ProviderDeniedConsent:
    """threat-model.md C-75 pieza 2: tres fases, cada una con su PROPIO
    alcance de conexion -- nunca una sola sesion de BD abarcando las tres.
    La fase (b), el canje contra Google (hasta 10s, NFR-103), corre SIN
    ninguna sesion de Postgres abierta: unas pocas vueltas concurrentes o
    lentas ya no pueden agotar el pool."""
    transaction = await _consume_reference(container, state)
    if code is None:
        # Google volvio con `error=` (el dueno cancelo, o denego el
        # consentimiento): la referencia ya se consumio arriba -- de un
        # solo uso tambien en el camino de fallo (C-66) -- y el resultado
        # es SIEMPRE `denied`, sin canje ni fase (c).
        return ProviderDeniedConsent(purpose=transaction.purpose, txn_id=transaction.txn_id)
    claims = await exchange_federated_code(
        provider, transaction, code=code, redirect_uri=redirect_uri
    )
    return await _resolve_after_exchange(container, allowed_emails, transaction, claims)


async def _consume_reference(container: Container, state: str) -> FederatedLoginTransaction:
    """Fase (a): abre su PROPIA sesion, consume la referencia, confirma y
    la cierra -- para cuando la fase (b) llama a Google no queda ninguna
    conexion de este callback retenida."""
    async with container.session_factory() as db_session:
        use_case = ConsumeFederatedTransaction(
            transactions=SqlFederatedTransactionRepository(db_session), clock=container.clock
        )
        transaction = await use_case.execute(state)
        await db_session.commit()
    return transaction


async def _resolve_after_exchange(
    container: Container,
    allowed_emails: AuthorizedEmailList,
    transaction: FederatedLoginTransaction,
    claims: FederatedIdentityClaims,
) -> CompletedFederatedLogin:
    """Fase (c): sesion de BD NUEVA, abierta solo despues de que Google ya
    respondio -- resuelve al dueno y emite sesion o marca frescura."""
    async with container.session_factory() as db_session:
        use_case = ResolveFederatedLogin(
            owner_identities=SqlOwnerFederatedIdentityRepository(db_session),
            sessions=SqlSessionRepository(db_session),
            allowed_emails=allowed_emails,
            id_generator=container.id_generator,
            clock=container.clock,
        )
        outcome = await use_case.execute(transaction, claims)
        # `SqlSessionRepository.create/save()` do not commit on their own
        # (same contract as `SqlOwnerRepository`, `router.py::login`): the
        # caller commits once the whole outcome (new owner + new session,
        # or the freshness mark) is known good.
        await db_session.commit()
    return outcome


async def _record_denied_attempt(
    container: Container,
    request: Request,
    exc: FederatedIdentityNotAuthorizedError,
    *,
    trusted_proxy_hops: int,
) -> None:
    """threat-model.md C-77: SOLO los desenlaces `denied` alimentan el
    bloqueo 5/15 min, y SOLO contra el correo que vino en el `id_token` ya
    validado -- nunca contra el correo del dueno de la instalacion. Un
    fallo anterior a tener identidad (state desconocido, proveedor caido)
    no llega aqui: no hay correo que registrar. Sesion propia: para cuando
    esto se llama, la de la fase (c) (si llego a abrirse) ya se cerro.

    M-1 (revision de seguridad 17-sep): esto se llama DENTRO del `except`
    de `_handle_callback` -- un fallo aqui (Postgres caido a medias) sin
    su propio `try` escaparia por encima de ese `except` y aterrizaria en
    el manejador generico (500), la ultima grieta de "nunca un 500" en el
    tramo federado. El registro del intento es auditoria de mejor
    esfuerzo: si falla, el dueno sigue viendo `denied`, nunca un error
    tecnico."""
    try:
        await _write_denied_attempt(
            container, request, exc, trusted_proxy_hops=trusted_proxy_hops
        )
    except Exception as record_exc:  # noqa: BLE001 - auditoria de mejor esfuerzo, nunca un 500
        logger.warning(
            "federated_denied_attempt_not_recorded", error_type=type(record_exc).__name__
        )


async def _write_denied_attempt(
    container: Container,
    request: Request,
    exc: FederatedIdentityNotAuthorizedError,
    *,
    trusted_proxy_hops: int,
) -> None:
    async with container.session_factory() as db_session:
        await SqlLoginAttemptRepository(db_session).record(
            email=str(exc.email),
            succeeded=False,
            ip_address=_client_ip(request, trusted_proxy_hops=trusted_proxy_hops),
        )


def _redirect_for_outcome(outcome: CompletedFederatedLogin) -> RedirectResponse:
    response = _redirect(_success_location(outcome.txn_id))
    if isinstance(outcome, FederatedSessionIssued):
        set_session_cookie(response, outcome.authenticated_session.raw_token)
    return response


def _success_location(txn_id: uuid.UUID | None) -> str:
    if txn_id is None:
        return "/"
    return f"/oauth/autorizar?txn={txn_id}"


def _redirect_to_login_error(code: str) -> RedirectResponse:
    return _redirect(_LOGIN_ERROR_LOCATION.format(code=code))


def _redirect_to_error(
    code: str, context: FederatedCallbackFailureContext | None
) -> RedirectResponse:
    """contracts/federated-login.md §1: un fallo cuyo proposito era
    `reidentify` y que conoce su `txn_id` vuelve a la MISMA pantalla de
    consentimiento con el error en la query -- nunca a `/login`, donde el
    dueno ya tiene sesion y perderia el hilo de lo que estaba aprobando
    (checkpoints/us1.md). Sin contexto -- entrar, o una re-identificacion
    sin transaccion que retomar -- el destino sigue siendo `/login`."""
    if context is not None and context.purpose is TransactionPurpose.REIDENTIFY:
        if context.txn_id is not None:
            return _redirect(f"/oauth/autorizar?txn={context.txn_id}&federated_error={code}")
    return _redirect_to_login_error(code)


def _redirect(location: str) -> RedirectResponse:
    response = RedirectResponse(url=location, status_code=status.HTTP_303_SEE_OTHER)
    response.headers["Cache-Control"] = "no-store"
    return response


def _client_ip(request: Request, *, trusted_proxy_hops: int) -> str | None:
    """Code review 17-sep (B-1/item 2): delega en `shared/net/client_ip.py`
    (UN solo lugar, nunca copiado) -- antes ignoraba `ADS_TRUSTED_PROXY_HOPS`
    del todo y devolvia el literal `"unknown"` sin `request.client`, que
    `federated_login_transactions.ip_address`/`login_attempts.ip_address`
    (`INET`) no pueden aceptar (`DataError`, 500, en la unica ruta anonima
    del login federado)."""
    return resolve_client_ip(
        forwarded_for=request.headers.get("x-forwarded-for"),
        peer_ip=request.client.host if request.client is not None else None,
        trusted_proxy_hops=trusted_proxy_hops,
    )

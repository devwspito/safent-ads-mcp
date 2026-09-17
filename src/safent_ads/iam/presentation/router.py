"""Community password and native SSO owner sessions. No companion MFA."""

from __future__ import annotations

import secrets
from datetime import datetime

from fastapi import APIRouter, Request, Response
from starlette import status

from safent_ads.composition.container import Container
from safent_ads.composition.settings import ApiSettings
from safent_ads.iam.application.errors import (
    AccountLockedError,
    AssertionExpiredError,
    AssertionInvalidError,
    AssertionMalformedError,
    AssertionReplayedError,
    InvalidCredentialsError,
    OwnerBoundElsewhereError,
)
from safent_ads.iam.application.exchange_owner_assertion import ExchangeOwnerAssertion
from safent_ads.iam.application.login import Login
from safent_ads.iam.application.logout import Logout
from safent_ads.iam.application.session_policy import (
    FEDERATED_IDENTIFICATION_TTL,
    SESSION_ABSOLUTE_TTL,
)
from safent_ads.iam.domain.session import Session
from safent_ads.iam.infrastructure.argon2_password_hasher import Argon2PasswordHasher
from safent_ads.iam.infrastructure.ed25519_assertion_verifier import Ed25519AssertionVerifier
from safent_ads.iam.infrastructure.sql_assertion_replay_repository import (
    SqlAssertionReplayRepository,
)
from safent_ads.iam.infrastructure.sql_business_directory import SqlBusinessDirectory
from safent_ads.iam.infrastructure.sql_login_attempt_repository import SqlLoginAttemptRepository
from safent_ads.iam.infrastructure.sql_owner_bridge_repository import SqlOwnerBridgeRepository
from safent_ads.iam.infrastructure.sql_owner_repository import SqlOwnerRepository
from safent_ads.iam.infrastructure.sql_session_repository import SqlSessionRepository
from safent_ads.iam.presentation.dependencies import (
    CURRENT_OWNER,
    SESSION_COOKIE_NAME,
    AuthenticatedOwner,
    current_session,
    set_session_cookie,
)
from safent_ads.iam.presentation.errors import ApiError
from safent_ads.iam.presentation.schemas import (
    BusinessSummary,
    ExchangeRequest,
    ExchangeResponse,
    LoginRequest,
    MeResponse,
    SessionSummary,
)
from safent_ads.shared.net.client_ip import resolve_client_ip

_SSO_EXPECTED_SLUG = "safent-ads"


def _client_ip(request: Request, *, trusted_proxy_hops: int) -> str | None:
    """Code review 17-sep: delega en `shared/net/client_ip.py` (UN solo
    lugar) -- antes devolvia el literal `"unknown"` sin `request.client`,
    que un llamador que lo persiste en una columna `INET` (`login_attempts.
    ip_address`) no puede aceptar."""
    return resolve_client_ip(
        forwarded_for=request.headers.get("x-forwarded-for"),
        peer_ip=request.client.host if request.client is not None else None,
        trusted_proxy_hops=trusted_proxy_hops,
    )


def _session_summary(
    session: Session, *, federated_login_active: bool, now: datetime
) -> SessionSummary:
    """contracts/federated-login.md §2: con el interruptor apagado, la
    pista de frescura es `null` sin importar lo que diga la fila -- el
    dueño ya no tiene forma de renovarla por esa vía.

    C-81 (T064 security review): esta pista es SOLO federada -- desde que
    `fresh_identification.py` dejó de escribir `last_federated_auth_at`
    para TOTP (evidencia per-acción, en `totp_reauth_confirmations`, sin
    equivalente a nivel de sesión que mostrar aquí).

    T064, re-verificación de C-82: NO basta con mirar si la columna es
    `NULL` -- una sesión `origin='federated'` cuya marca fue INVALIDADA
    (revocación destructiva) la lleva empujada a un instante muy antiguo,
    nunca a `NULL` (`sessions_federated_origin_check` lo prohíbe). Sumarle
    la ventana a ese instante antiguo filtraría una fecha fabricada
    (~2016) que no significa nada. Por eso esto llama al MISMO predicado
    booleano que decide de verdad en `require_fresh_identification`
    (`Session.has_fresh_federated_identification`) en vez de repetir la
    aritmética -- nunca pueden discrepar."""
    fresh_until = None
    if federated_login_active and session.has_fresh_federated_identification(
        now, FEDERATED_IDENTIFICATION_TTL, SESSION_ABSOLUTE_TTL
    ):
        assert session.last_federated_auth_at is not None  # noqa: S101 - lo exige el predicado de arriba
        fresh_until = session.last_federated_auth_at + FEDERATED_IDENTIFICATION_TTL
    return SessionSummary(origin=session.origin, fresh_identification_until=fresh_until)


def build_auth_router(settings: ApiSettings) -> APIRouter:
    password_hasher = Argon2PasswordHasher()
    decoy_password_hash = password_hasher.hash(secrets.token_urlsafe(16))

    router = APIRouter(prefix="/api/v1/auth", tags=["auth"])
    if settings.companion_mode:
        _register_exchange_route(router, settings)

    @router.post("/login", status_code=status.HTTP_204_NO_CONTENT)
    async def login(payload: LoginRequest, request: Request, response: Response) -> None:
        container: Container = request.app.state.container
        async with container.session_factory() as db_session:
            use_case = Login(
                owner_repository=SqlOwnerRepository(db_session),
                login_attempt_repository=SqlLoginAttemptRepository(db_session),
                password_hasher=password_hasher,
                session_repository=SqlSessionRepository(db_session),
                id_generator=container.id_generator,
                clock=container.clock,
                decoy_password_hash=decoy_password_hash,
            )
            try:
                authenticated = await use_case.execute(
                    email=payload.email,
                    password=payload.password,
                    ip_address=_client_ip(
                        request, trusted_proxy_hops=settings.trusted_proxy_hops
                    ),
                )
            except AccountLockedError as exc:
                raise ApiError(
                    status_code=status.HTTP_429_TOO_MANY_REQUESTS,
                    code="ACCOUNT_LOCKED",
                    message="Demasiados intentos recientes.",
                ) from exc
            except InvalidCredentialsError as exc:
                raise ApiError(
                    status_code=status.HTTP_401_UNAUTHORIZED,
                    code="INVALID_CREDENTIALS",
                    message="Credenciales invalidas.",
                ) from exc
            await db_session.commit()

        set_session_cookie(response, authenticated.raw_token)

    @router.post("/logout", status_code=status.HTTP_204_NO_CONTENT)
    async def logout(request: Request, response: Response) -> None:
        raw_token = request.cookies.get(SESSION_COOKIE_NAME)
        if raw_token:
            container: Container = request.app.state.container
            async with container.session_factory() as db_session:
                await Logout(SqlSessionRepository(db_session), container.clock).execute(
                    raw_token=raw_token
                )
                await db_session.commit()
        response.delete_cookie(key=SESSION_COOKIE_NAME, path="/api")

    @router.get("/me", response_model=MeResponse)
    async def me(
        request: Request, response: Response, owner: AuthenticatedOwner = CURRENT_OWNER
    ) -> MeResponse:
        response.headers["Cache-Control"] = "no-store"
        container: Container = request.app.state.container
        async with container.session_factory() as db_session:
            businesses = await SqlBusinessDirectory(db_session).list_all()
            session = await current_session(request, db_session)

        return MeResponse(
            owner_id=owner.owner_id,
            email=owner.email,
            businesses=[
                BusinessSummary(business_id=bid, slug=slug, name=name)
                for bid, slug, name in businesses
            ],
            session=_session_summary(
                session,
                federated_login_active=settings.federated_login_active,
                now=container.clock.now(),
            ),
            federated_login_available=settings.federated_login_active,
        )

    return router


def _register_exchange_route(router: APIRouter, settings: ApiSettings) -> None:
    """`POST /auth/exchange` (026, contracts/sso.md §4): solo existe en modo
    companion (`build_auth_router` ni siquiera la registra fuera de ese
    modo, así que la ruta responde `404` por diseño de enrutado, no por un
    `if` dentro del handler). El verificador Ed25519 se construye una vez
    al arrancar, igual que el resto de adaptadores criptográficos de este
    router."""
    verifier = Ed25519AssertionVerifier(settings.sso_public_key or "")

    @router.post("/exchange", response_model=ExchangeResponse)
    async def exchange(
        payload: ExchangeRequest, request: Request, response: Response
    ) -> ExchangeResponse:
        container: Container = request.app.state.container
        async with container.session_factory() as db_session:
            use_case = ExchangeOwnerAssertion(
                verifier=verifier,
                replay_guard=SqlAssertionReplayRepository(db_session),
                owner_bridge=SqlOwnerBridgeRepository(db_session),
                session_repository=SqlSessionRepository(db_session),
                id_generator=container.id_generator,
                clock=container.clock,
                expected_slug=_SSO_EXPECTED_SLUG,
            )
            try:
                authenticated = await use_case.execute(assertion=payload.assertion)
            except AssertionMalformedError as exc:
                raise ApiError(
                    status_code=status.HTTP_400_BAD_REQUEST,
                    code="ASSERTION_MALFORMED",
                    message="Aserción ilegible.",
                ) from exc
            except AssertionReplayedError as exc:
                raise ApiError(
                    status_code=status.HTTP_409_CONFLICT,
                    code="ASSERTION_REPLAYED",
                    message="Aserción ya utilizada.",
                ) from exc
            except AssertionExpiredError as exc:
                raise ApiError(
                    status_code=status.HTTP_401_UNAUTHORIZED,
                    code="ASSERTION_EXPIRED",
                    message="Aserción caducada.",
                ) from exc
            except AssertionInvalidError as exc:
                raise ApiError(
                    status_code=status.HTTP_401_UNAUTHORIZED,
                    code="ASSERTION_INVALID",
                    message="Aserción inválida.",
                ) from exc
            except OwnerBoundElsewhereError as exc:
                raise ApiError(
                    status_code=status.HTTP_403_FORBIDDEN,
                    code="OWNER_BOUND_ELSEWHERE",
                    message="El propietario ya está atado a otra identidad.",
                ) from exc

            businesses = await SqlBusinessDirectory(db_session).list_all()
            await db_session.commit()

        set_session_cookie(response, authenticated.raw_token)
        return ExchangeResponse(
            owner_id=authenticated.session.owner_id,
            business_id=businesses[0][0] if businesses else None,
            expires_at=authenticated.session.expires_at,
        )

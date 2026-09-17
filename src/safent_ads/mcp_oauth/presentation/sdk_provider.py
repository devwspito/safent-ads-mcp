"""`SdkOAuthProvider` (tasks.md T009): implementa
`OAuthAuthorizationServerProvider` del SDK delegando en los casos de uso de
`mcp_oauth/application/` -- ACL pura (plan.md): los modelos pydantic del
SDK (`AuthorizationCode`, `RefreshToken`, `AccessToken`,
`OAuthClientInformationFull`) nunca cruzan hacia `application`/`domain`, y
las excepciones de dominio/aplicacion nunca cruzan hacia el SDK sin
traducir.

Una sesion SQLAlchemy por llamada (`OAuthSessionFactory`, mismo patron que
`composition/mcp_write_adapter.py`): cada metodo abre su `async with
self._session_factory()`, construye el caso de uso sobre esos
repositorios y confirma explicitamente -- a veces incluso en la rama de
error (`exchange_authorization_code`, C-38: la revocacion de la familia
reutilizada tiene que sobrevivir aunque la respuesta final sea
`invalid_grant`).

`code_verifier` y el `resource` de la peticion de `/token` nunca llegan a
`exchange_authorization_code()`/`exchange_refresh_token()`
(`sdk:handlers/token.py:33,144-200,204-240`: el SDK los usa para su propia
validacion y no los reenvia al provider) -- por eso
`RedeemCode.execute_with_pkce_verified_upstream()` no repite la
comprobacion de PKCE, y por eso el recurso del token emitido nunca sale de
lo que `AuthorizationRequest`/`Grant` ya fijaron en `/authorize` (siempre
canonico, `StartAuthorization`): un cliente no puede desviar la audiencia
del token mintiendo en `/token` porque ese dato ya no se lee ahi
(threat-model.md C-46)."""

from __future__ import annotations

import time
from contextlib import AbstractAsyncContextManager

import structlog
from mcp.server.auth.provider import (
    AccessToken,
    AuthorizationCode,
    AuthorizationParams,
    AuthorizeError,
    IdentityAssertionParams,
    RefreshToken,
    RegistrationError,
    TokenError,
)
from mcp.shared.auth import OAuthClientInformationFull, OAuthToken
from pydantic import AnyUrl
from sqlalchemy.exc import IntegrityError

from safent_ads.mcp_oauth.application.errors import (
    ClientMismatchError,
    ConcurrentRefreshInProgressError,
    GrantNotFoundError,
    InvalidTargetError,
    RedirectUriMismatchError,
    TooManyPendingAuthorizationsError,
    TooManyUnconsentedClientsError,
    UnknownAuthorizationCodeError,
    UnknownClientError,
    UnknownRefreshTokenError,
)
from safent_ads.mcp_oauth.application.introspect_token import IntrospectToken
from safent_ads.mcp_oauth.application.policy import ACCESS_TOKEN_TTL, DEFAULT_DCR_SCOPE
from safent_ads.mcp_oauth.application.ports import (
    OAuthSession,
    OAuthSessionFactory,
    OpaqueTokenFactory,
    TokenHasher,
)
from safent_ads.mcp_oauth.application.redeem_code import IssuedTokenPair, RedeemCode
from safent_ads.mcp_oauth.application.refresh_grant import RefreshedTokenPair, RefreshGrant
from safent_ads.mcp_oauth.application.register_client import ClientRegistration, RegisterClient
from safent_ads.mcp_oauth.application.revoke_grant import RevokeGrant
from safent_ads.mcp_oauth.application.start_authorization import (
    AuthorizationRequestDraft,
    StartAuthorization,
)
from safent_ads.mcp_oauth.domain.authorization import AuthorizationRequest
from safent_ads.mcp_oauth.domain.client import OAuthClient, TokenEndpointAuthMethod
from safent_ads.mcp_oauth.domain.errors import (
    AuthorizationRequestExpiredError,
    AuthorizationRequestNotConsentedError,
    CodeAlreadyRedeemedError,
    EmptyScopeSetError,
    GrantRevokedError,
    InvalidClientNameError,
    InvalidCodeChallengeError,
    InvalidRedirectUriError,
    RefreshTokenReusedError,
    ScopeExpansionError,
    TokenExpiredError,
    TooManyRedirectUrisError,
    UnknownScopeError,
)
from safent_ads.mcp_oauth.domain.grant import TokenKind
from safent_ads.mcp_oauth.domain.resource import ResourceIndicator
from safent_ads.mcp_oauth.domain.scope import ScopeSet
from safent_ads.mcp_oauth.presentation.loopback_client import LoopbackAwareClientInformation
from safent_ads.mcp_oauth.presentation.token_verifier import introspection_to_access_token
from safent_ads.shared.clock import Clock
from safent_ads.shared.errors import DomainError
from safent_ads.shared.ids import IdGenerator

logger = structlog.get_logger(__name__)

# threat-model.md C-42: mismo tope que 0035_mcp_oauth
# (`oauth_authorization_requests_client_state_check`).
_MAX_CLIENT_STATE_LENGTH = 512
# Solo un margen para que la comprobacion de caducidad PROPIA del SDK
# (`sdk:handlers/token.py:156`) nunca descarte un replay tardio antes de
# que `exchange_authorization_code()` pueda revocar la familia emitida
# (C-38) -- la caducidad real (60 s desde el consentimiento) la aplica
# `AuthorizationRequest.is_expired()` dentro del canje, no este valor.
_LOAD_CODE_GRACE_SECONDS = 300


class SdkOAuthProvider:
    def __init__(
        self,
        *,
        session_factory: OAuthSessionFactory,
        id_generator: IdGenerator,
        clock: Clock,
        token_hasher: TokenHasher,
        token_factory: OpaqueTokenFactory,
        public_base_url: str,
    ) -> None:
        self._session_factory = session_factory
        self._id_generator = id_generator
        self._clock = clock
        self._token_hasher = token_hasher
        self._token_factory = token_factory
        self._canonical_resource = ResourceIndicator.canonical(public_base_url).value
        self._public_base_url = public_base_url

    # --- registro dinamico (RFC 7591) --------------------------------

    async def get_client(self, client_id: str) -> OAuthClientInformationFull | None:
        """`None` = "ese cliente no existe para nosotros", que el SDK
        traduce al `invalid_client` legible que promete `oauth.md`. Una
        registracion que ya no cumple una regla endurecida despues entra
        por aqui -- destino remoto (D-11) o `client_name` con caracteres
        bidireccionales (C-70 pieza 3) --: sin este `except` saldria un
        500 opaco en `/authorize` (revision de codigo, 17-sep). Se atrapa
        `DomainError` entero, no una excepcion concreta, para que la
        SIGUIENTE regla que alguien endurezca tampoco lo convierta en un
        500."""
        async with self._open() as db:
            try:
                client = await db.clients.get_by_id(client_id)
            except DomainError as exc:
                logger.warning(
                    "mcp_oauth_client_rejected_by_the_domain",
                    client_id=client_id,
                    error_type=type(exc).__name__,
                )
                return None
        return None if client is None else _to_sdk_client(client)

    async def register_client(self, client_info: OAuthClientInformationFull) -> None:
        self._require_public_client(client_info)
        registration = _to_registration(client_info)
        async with self._open() as db:
            use_case = RegisterClient(
                clients=db.clients,
                id_generator=self._id_generator,
                token_hasher=self._token_hasher,
                token_factory=self._token_factory,
                clock=self._clock,
            )
            try:
                await use_case.execute(registration)
            except (
                InvalidRedirectUriError,
                TooManyRedirectUrisError,
                InvalidClientNameError,
                EmptyScopeSetError,
                UnknownScopeError,
                TooManyUnconsentedClientsError,
            ) as exc:
                raise RegistrationError(
                    error="invalid_client_metadata", error_description=str(exc)
                ) from exc
            await db.commit()

    @staticmethod
    def _require_public_client(client_info: OAuthClientInformationFull) -> None:
        # RFC 8252 SS8.4: los agentes nativos (Claude Code, Codex) son
        # clientes publicos; un cliente confidencial exigiria guardar su
        # secreto en claro para que `ClientAuthenticator` lo compare
        # (`sdk:middleware/client_auth.py:106-114`), lo que violaria C-44.
        if client_info.token_endpoint_auth_method != "none":  # noqa: S105 - metodo RFC 7591, no un secreto
            raise RegistrationError(
                error="invalid_client_metadata",
                error_description="solo se admiten clientes publicos (RFC 8252 SS8.4)",
            )

    # --- autorizacion --------------------------------------------------

    async def authorize(
        self, client: OAuthClientInformationFull, params: AuthorizationParams
    ) -> str:
        client_state = self._require_valid_state(params.state)
        requested_scope = " ".join(params.scopes) if params.scopes else (client.scope or "")
        draft = AuthorizationRequestDraft(
            client_id=client.client_id,
            redirect_uri=str(params.redirect_uri),
            code_challenge=params.code_challenge,
            client_state=client_state,
            requested_scope=requested_scope,
            # I6 de la revision de seguridad (16-sep): sin `_require_canonical_resource`
            # duplicando la comprobacion aqui -- `StartAuthorization.execute()`
            # (application) ya la hace y es la UNICA implementacion (C-9).
            # `""` nunca coincide con ningun recurso canonico real, asi que
            # `resource` ausente sigue cayendo en `InvalidTargetError` igual.
            resource=params.resource or "",
        )
        async with self._open() as db:
            use_case = StartAuthorization(
                clients=db.clients,
                authorization_requests=db.authorization_requests,
                id_generator=self._id_generator,
                clock=self._clock,
                public_base_url=self._public_base_url,
            )
            request = await self._start(use_case, draft)
            await db.commit()
        return f"{self._public_base_url}/oauth/autorizar?txn={request.id}"

    @staticmethod
    def _require_valid_state(state: str | None) -> str | None:
        client_state = state or None
        if client_state is not None and len(client_state) > _MAX_CLIENT_STATE_LENGTH:
            raise AuthorizeError(
                error="invalid_request",
                error_description=f"state supera {_MAX_CLIENT_STATE_LENGTH} caracteres",
            )
        return client_state

    @staticmethod
    async def _start(
        use_case: StartAuthorization, draft: AuthorizationRequestDraft
    ) -> AuthorizationRequest:
        try:
            return await use_case.execute(draft)
        except UnknownClientError as exc:
            raise AuthorizeError(error="invalid_request", error_description=str(exc)) from exc
        except RedirectUriMismatchError as exc:
            raise AuthorizeError(error="invalid_request", error_description=str(exc)) from exc
        except InvalidTargetError as exc:
            raise AuthorizeError(error="invalid_target", error_description=str(exc)) from exc
        except TooManyPendingAuthorizationsError as exc:
            raise AuthorizeError(
                error="temporarily_unavailable", error_description=str(exc)
            ) from exc
        except (EmptyScopeSetError, UnknownScopeError) as exc:
            raise AuthorizeError(error="invalid_scope", error_description=str(exc)) from exc
        except InvalidCodeChallengeError as exc:
            # El SDK (`sdk:handlers/authorize.py::AuthorizationRequest`) solo
            # exige `code_challenge_method = "S256"`; la FORMA del propio
            # `code_challenge` (43 base64url, RFC 7636) no la valida -- sin
            # este `except`, un cliente que la manda mal formada tira un
            # `InvalidCodeChallengeError` de dominio sin capturar (revision
            # de seguridad T049): mismo 500 opaco que el `IntegrityError` de
            # abajo, por una condicion que el cliente SI puede corregir.
            raise AuthorizeError(error="invalid_request", error_description=str(exc)) from exc
        except IntegrityError as exc:
            # T049 (spec 008): un `resource`/`redirect_uri` que pasa la
            # validacion de aplicacion pero sigue violando un CHECK de
            # `oauth_authorization_requests` (0035_mcp_oauth) no debe escapar
            # como el `IntegrityError` crudo que el catch-all de
            # `sdk:handlers/authorize.py` convierte en un 500 `server_error`
            # -- solo `error_type`/el nombre del constraint al registro
            # (nunca la fila, que lleva valores del cliente).
            #
            # Revision de seguridad (PR 44, IMPORTANT-2): `error`, no
            # `warning` -- tras 0054/`ResourceIndicator` (T049), llegar
            # aqui ya no deberia ser posible con la validacion de aplicacion
            # al dia, asi que es una falla real que requiere investigar, no
            # una condicion esperable del cliente. `exc_info=True` para que
            # quien investigue tenga rastro de DONDE goleo -- el processor
            # `_exception_type_only` (`logging_setup.py`) ya reduce
            # cualquier `exc_info` al nombre del tipo antes de serializar,
            # asi que ni la fila ni el mensaje crudo (que trae valores del
            # cliente) llegan al log de todas formas.
            logger.error(
                "mcp_oauth_authorize_integrity_violation",
                error_type=type(exc).__name__,
                constraint=_constraint_name(exc),
                exc_info=True,
            )
            raise AuthorizeError(
                error="invalid_request",
                error_description="no se pudo crear la solicitud de autorizacion",
            ) from exc

    # --- codigo de autorizacion -----------------------------------------

    async def load_authorization_code(
        self,
        client: OAuthClientInformationFull,  # noqa: ARG002 - forma exigida por el Protocol del SDK
        authorization_code: str,
    ) -> AuthorizationCode | None:
        async with self._open() as db:
            request = await db.authorization_requests.get_by_code_hash(
                self._token_hasher.hash(authorization_code)
            )
        if request is None:
            return None
        return AuthorizationCode(
            code=authorization_code,
            scopes=[scope.value for scope in request.scope_set.scopes],
            expires_at=time.time() + _LOAD_CODE_GRACE_SECONDS,
            client_id=request.client_id,
            code_challenge=request.code_challenge,
            redirect_uri=AnyUrl(request.redirect_uri),
            redirect_uri_provided_explicitly=True,
            resource=request.resource.value,
        )

    async def exchange_authorization_code(
        self,
        client: OAuthClientInformationFull,  # noqa: ARG002 - forma exigida por el Protocol del SDK
        authorization_code: AuthorizationCode,
    ) -> OAuthToken:
        async with self._open() as db:
            use_case = RedeemCode(
                authorization_requests=db.authorization_requests,
                grants=db.grants,
                token_hasher=self._token_hasher,
                token_factory=self._token_factory,
                id_generator=self._id_generator,
                clock=self._clock,
            )
            pair = await self._redeem(db, use_case, authorization_code)
        return _to_oauth_token(pair.access_token, pair.refresh_token, pair.scope_set)

    async def _redeem(
        self, db: OAuthSession, use_case: RedeemCode, authorization_code: AuthorizationCode
    ) -> IssuedTokenPair:
        try:
            pair = await use_case.execute_with_pkce_verified_upstream(
                code=authorization_code.code,
                redirect_uri=str(authorization_code.redirect_uri),
                client_id=authorization_code.client_id,
                resource=authorization_code.resource or self._canonical_resource,
            )
        except CodeAlreadyRedeemedError as exc:
            # La revocacion de la familia (C-38) ya ocurrio dentro de
            # `execute_with_pkce_verified_upstream()`: confirma antes de
            # traducir, o se perderia al deshacerse la transaccion.
            await db.commit()
            raise TokenError(error="invalid_grant", error_description=str(exc)) from exc
        except (
            UnknownAuthorizationCodeError,
            ClientMismatchError,
            RedirectUriMismatchError,
            AuthorizationRequestExpiredError,
            AuthorizationRequestNotConsentedError,
        ) as exc:
            raise TokenError(error="invalid_grant", error_description=str(exc)) from exc
        await db.commit()
        return pair

    # --- refresco ---------------------------------------------------------

    async def load_refresh_token(
        self,
        client: OAuthClientInformationFull,  # noqa: ARG002 - forma exigida por el Protocol del SDK
        refresh_token: str,
    ) -> RefreshToken | None:
        token_hash = self._token_hasher.hash(refresh_token)
        async with self._open() as db:
            grant = await db.grants.get_by_token_hash(token_hash)
        token = None if grant is None else grant.find_token(token_hash)
        if grant is None or token is None or token.kind is not TokenKind.REFRESH:
            return None
        return RefreshToken(
            token=refresh_token,
            client_id=grant.client_id,
            scopes=[scope.value for scope in grant.scope_set.scopes],
            expires_at=int(token.expires_at.timestamp()),
            resource=grant.resource.value,
        )

    async def exchange_refresh_token(
        self,
        client: OAuthClientInformationFull,  # noqa: ARG002 - forma exigida por el Protocol del SDK
        refresh_token: RefreshToken,
        scopes: list[str],
    ) -> OAuthToken:
        async with self._open() as db:
            use_case = RefreshGrant(
                grants=db.grants,
                token_hasher=self._token_hasher,
                token_factory=self._token_factory,
                clock=self._clock,
            )
            pair = await self._refresh(db, use_case, refresh_token, scopes)
        return _to_oauth_token(pair.access_token, pair.refresh_token, pair.scope_set)

    async def _refresh(
        self,
        db: OAuthSession,
        use_case: RefreshGrant,
        refresh_token: RefreshToken,
        scopes: list[str],
    ) -> RefreshedTokenPair:
        requested_scope = " ".join(scopes) if scopes else None
        try:
            pair = await use_case.execute(
                refresh_token=refresh_token.token,
                client_id=refresh_token.client_id,
                resource=refresh_token.resource or self._canonical_resource,
                requested_scope=requested_scope,
            )
        except RefreshTokenReusedError as exc:
            # Igual que en `_redeem`: el reuso ya revoco la concesion
            # dentro de `RefreshGrant.execute()` (su propio `finally`
            # confirma el `save()`), pero esta transaccion sigue siendo la
            # que tiene que confirmarla.
            await db.commit()
            raise TokenError(error="invalid_grant", error_description=str(exc)) from exc
        except (
            UnknownRefreshTokenError,
            ClientMismatchError,
            GrantRevokedError,
            TokenExpiredError,
        ) as exc:
            raise TokenError(error="invalid_grant", error_description=str(exc)) from exc
        except ScopeExpansionError as exc:
            raise TokenError(error="invalid_scope", error_description=str(exc)) from exc
        except ConcurrentRefreshInProgressError as exc:
            # fix/refresh-rotation-race: `SqlGrantRepository.
            # get_by_token_hash_for_rotation` ya se rindio SIN leer nada
            # (`FOR UPDATE NOWAIT` choco al instante) -- perder una
            # carrera legitima contra otro refresco del MISMO token no es
            # reuso, la ganadora sigue viva; no hay nada que confirmar ni
            # revocar aqui.
            raise TokenError(error="invalid_grant", error_description=str(exc)) from exc
        except IntegrityError as exc:
            # L7 de la revision de seguridad (16-sep), defensa en
            # profundidad: si por lo que sea dos rotaciones del MISMO
            # token llegasen a insertar tokens ACTIVE a la vez sin pasar
            # por el lock de arriba, `ix_oauth_tokens_grant_active`
            # (UNIQUE parcial) las separa igual -- mismo resultado de
            # negocio que `ConcurrentRefreshInProgressError`.
            raise TokenError(
                error="invalid_grant",
                error_description="refresh token en uso por otra peticion concurrente",
            ) from exc
        await db.commit()
        return pair

    # --- acceso e introspeccion --------------------------------------

    async def load_access_token(self, token: str) -> AccessToken | None:
        async with self._open() as db:
            use_case = IntrospectToken(
                grants=db.grants, token_hasher=self._token_hasher, clock=self._clock
            )
            introspection = await use_case.execute(token)
        if not introspection.active:
            return None
        return introspection_to_access_token(token, introspection)

    async def revoke_token(self, token: AccessToken | RefreshToken) -> None:
        async with self._open() as db:
            grant = await db.grants.get_by_token_hash(self._token_hasher.hash(token.token))
            if grant is None:
                return
            use_case = RevokeGrant(grants=db.grants, clock=self._clock)
            try:
                await use_case.execute(
                    grant_id=grant.id, owner_id=grant.owner_id, reason="revoke_endpoint"
                )
            except GrantNotFoundError:
                # RFC 7009: revocar un token ya invalido/desconocido sigue
                # siendo 200, nunca un error.
                return
            await db.commit()

    # --- SEP-990 (ID-JAG), fuera de alcance de esta spec ------------------

    async def exchange_identity_assertion(
        self,
        client: OAuthClientInformationFull,  # noqa: ARG002 - forma exigida por el Protocol del SDK
        params: IdentityAssertionParams,  # noqa: ARG002 - idem
    ) -> OAuthToken:
        """`identity_assertion_enabled` nunca se activa en `AuthSettings`
        (`composition/app.py::_build_mcp_oauth_wiring`): mismo rechazo por
        defecto que documenta `OAuthAuthorizationServerProvider.
        exchange_identity_assertion` (`mcp:server/auth/provider.py`) -- este
        metodo solo existe para que `SdkOAuthProvider` conforme el Protocol
        completo (duck typing, sin heredar de el)."""
        raise TokenError(
            error="unsupported_grant_type",
            error_description="El grant jwt-bearer (SEP-990) no esta soportado",
        )

    def _open(self) -> AbstractAsyncContextManager[OAuthSession]:
        return self._session_factory()


def _constraint_name(exc: IntegrityError) -> str | None:
    """Nombre del CHECK/UNIQUE que violo el driver, sin arrastrar la fila
    (`asyncpg.exceptions.PostgresError.constraint_name`, colgado de
    `exc.orig.__cause__` en el dialecto asyncpg de SQLAlchemy) -- lo unico
    seguro de registrar de un `IntegrityError`, cuyo mensaje por defecto
    lleva los valores que el cliente mando."""
    cause = getattr(exc.orig, "__cause__", None)
    name = getattr(cause, "constraint_name", None)
    return name if isinstance(name, str) else None


def _to_sdk_client(client: OAuthClient) -> LoopbackAwareClientInformation:
    return LoopbackAwareClientInformation(
        client_id=client.id,
        client_name=client.client_name,
        redirect_uris=[AnyUrl(str(uri)) for uri in client.redirect_uris],
        token_endpoint_auth_method=client.token_endpoint_auth_method.value,
        grant_types=list(client.grant_types),
        scope=str(client.requested_scope),
        client_secret=None,
    )


def _to_registration(client_info: OAuthClientInformationFull) -> ClientRegistration:
    return ClientRegistration(
        client_id=client_info.client_id,
        client_name=client_info.client_name or client_info.client_id,
        redirect_uris=tuple(str(uri) for uri in client_info.redirect_uris or ()),
        token_endpoint_auth_method=TokenEndpointAuthMethod.NONE,
        grant_types=tuple(client_info.grant_types),
        requested_scope=_parse_scope(client_info.scope),
    )


def _parse_scope(raw: str | None) -> ScopeSet:
    return ScopeSet.parse(raw) if raw else DEFAULT_DCR_SCOPE


def _to_oauth_token(access_token: str, refresh_token: str, scope_set: ScopeSet) -> OAuthToken:
    return OAuthToken(
        access_token=access_token,
        token_type="Bearer",  # noqa: S106 - RFC 6749 SS5.1 literal, no un secreto
        expires_in=int(ACCESS_TOKEN_TTL.total_seconds()),
        scope=str(scope_set),
        refresh_token=refresh_token,
    )

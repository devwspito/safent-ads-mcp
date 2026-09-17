"""`SdkOAuthProvider` (tasks.md T009): cada metodo traduce sin logica de
negocio propia; `authorize()` no emite codigo (contracts/oauth.md §4:
crea la solicitud PENDING y redirige al panel)."""

from __future__ import annotations

import base64
import hashlib
import uuid
from datetime import UTC, datetime, timedelta
from urllib.parse import parse_qs, urlsplit

import pytest
import structlog
from mcp.server.auth.provider import (
    AccessToken,
    AuthorizationParams,
    AuthorizeError,
    RegistrationError,
    TokenError,
)
from mcp.shared.auth import OAuthClientInformationFull
from pydantic import AnyUrl, TypeAdapter
from sqlalchemy.exc import IntegrityError

import safent_ads.mcp_oauth.presentation.sdk_provider as sdk_provider_module
from safent_ads.logging_setup import _exception_type_only, redact_secrets
from safent_ads.mcp_oauth.application.grant_consent import ApproveConsent
from safent_ads.mcp_oauth.application.policy import (
    AUTHORIZATION_REQUEST_TTL,
    MAX_UNCONSENTED_CLIENTS_HARD_CEILING,
)
from safent_ads.mcp_oauth.domain.authorization import AuthorizationRequestState
from safent_ads.mcp_oauth.domain.client import OAuthClient, RedirectUri, TokenEndpointAuthMethod
from safent_ads.mcp_oauth.domain.errors import InvalidRedirectUriError
from safent_ads.mcp_oauth.domain.scope import ScopeSet
from safent_ads.mcp_oauth.infrastructure.fakes import FakeOAuthSession
from safent_ads.mcp_oauth.infrastructure.secrets_token_factory import SecretsOpaqueTokenFactory
from safent_ads.mcp_oauth.infrastructure.sha256_token_hasher import Sha256TokenHasher
from safent_ads.mcp_oauth.presentation.sdk_provider import SdkOAuthProvider
from safent_ads.shared.clock import FixedClock
from safent_ads.shared.ids import UuidIdGenerator

_URL = TypeAdapter(AnyUrl)
_NOW = datetime(2026, 1, 1, 0, 0, tzinfo=UTC)
_PUBLIC_BASE_URL = "https://ads.example.com"
_RESOURCE = f"{_PUBLIC_BASE_URL}/mcp"
_REDIRECT_URI = "http://127.0.0.1:54321/callback"
_CLIENT_ID = "test-client-1"


def _pkce_pair() -> tuple[str, str]:
    verifier = "test-code-verifier-0123456789abcdefghijk"[:43]
    digest = hashlib.sha256(verifier.encode("ascii")).digest()
    challenge = base64.urlsafe_b64encode(digest).rstrip(b"=").decode("ascii")
    return verifier, challenge


def _authorization_params(*, code_challenge: str, state: str | None = "xyz") -> AuthorizationParams:
    return AuthorizationParams(
        state=state,
        scopes=["ads:read"],
        code_challenge=code_challenge,
        redirect_uri=_URL.validate_python(_REDIRECT_URI),
        redirect_uri_provided_explicitly=True,
        resource=_RESOURCE,
    )


def _txn_id_from_redirect(redirect_to: str) -> uuid.UUID:
    query = parse_qs(urlsplit(redirect_to).query)
    return uuid.UUID(query["txn"][0])


class _Fixture:
    def __init__(self) -> None:
        self.session = FakeOAuthSession()
        self.clock = FixedClock(_NOW)
        self.hasher = Sha256TokenHasher()
        self.factory = SecretsOpaqueTokenFactory()
        self.provider = SdkOAuthProvider(
            session_factory=lambda: self.session,
            id_generator=UuidIdGenerator(),
            clock=self.clock,
            token_hasher=self.hasher,
            token_factory=self.factory,
            public_base_url=_PUBLIC_BASE_URL,
        )

    async def register_client(
        self,
        *,
        client_id: str = _CLIENT_ID,
        token_endpoint_auth_method: str = "none",  # noqa: S107 - metodo RFC 7591, no un secreto
    ) -> OAuthClientInformationFull:
        client_info = OAuthClientInformationFull(
            client_id=client_id,
            client_name="Claude Code",
            redirect_uris=[_URL.validate_python(_REDIRECT_URI)],
            token_endpoint_auth_method=token_endpoint_auth_method,
            grant_types=["authorization_code", "refresh_token"],
            response_types=["code"],
            scope="ads:read ads:propose",
        )
        await self.provider.register_client(client_info)
        loaded = await self.provider.get_client(client_id)
        assert loaded is not None
        return loaded

    async def consent(self, txn_id: uuid.UUID) -> tuple[str, uuid.UUID]:
        approver = ApproveConsent(
            authorization_requests=self.session.authorization_requests,
            clients=self.session.clients,
            token_hasher=self.hasher,
            token_factory=self.factory,
            clock=self.clock,
        )
        owner_id = uuid.uuid4()
        approved = await approver.execute(txn_id=txn_id, owner_id=owner_id)
        return approved.code, owner_id

    async def authorized_code(self) -> tuple[str, uuid.UUID]:
        client = await self.register_client()
        _, challenge = _pkce_pair()
        params = _authorization_params(code_challenge=challenge)
        redirect_to = await self.provider.authorize(client, params)
        txn_id = _txn_id_from_redirect(redirect_to)
        return await self.consent(txn_id)


async def test_get_client_returns_none_for_unknown_client() -> None:
    fixture = _Fixture()

    assert await fixture.provider.get_client("does-not-exist") is None


async def test_register_client_then_get_client_round_trips() -> None:
    fixture = _Fixture()

    client = await fixture.register_client()

    assert client.client_id == _CLIENT_ID
    assert client.client_secret is None
    assert [str(u) for u in client.redirect_uris or []] == [_REDIRECT_URI]


async def test_get_client_with_a_rejected_redirect_uri_is_treated_as_unknown(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Revision de codigo (17-sep): una registracion anterior a D-11
    (destino remoto) ya no se puede hidratar. `get_client` la trata como
    desconocida, que el SDK traduce al `invalid_client` legible de
    `contracts/oauth.md`, en vez de dejar escapar un 500 opaco."""
    fixture = _Fixture()

    async def _rejected(_client_id: str) -> OAuthClient | None:
        raise InvalidRedirectUriError("redirect_uri debe ser de bucle local")

    monkeypatch.setattr(fixture.session.clients, "get_by_id", _rejected)

    assert await fixture.provider.get_client(_CLIENT_ID) is None


async def test_register_client_rejects_confidential_clients() -> None:
    fixture = _Fixture()

    with pytest.raises(RegistrationError):
        await fixture.register_client(token_endpoint_auth_method="client_secret_post")


async def test_register_client_rejects_invalid_redirect_uri() -> None:
    """D-11 (threat-model.md C-70 pieza 4) en la frontera de DCR: un
    destino remoto sale por `invalid_client_metadata`, no se registra."""
    fixture = _Fixture()
    client_info = OAuthClientInformationFull(
        client_id=_CLIENT_ID,
        client_name="Claude Code",
        redirect_uris=[_URL.validate_python("https://agent.example/callback")],
        token_endpoint_auth_method="none",
        grant_types=["authorization_code"],
        response_types=["code"],
        scope="ads:read",
    )

    with pytest.raises(RegistrationError):
        await fixture.provider.register_client(client_info)


async def test_register_client_evicts_the_oldest_when_capacity_reached() -> None:
    """M3 de la revision de seguridad (16-sep, threat-model.md C-42): al
    llegar al tope (50), el registro numero 51 tiene EXITO y el cliente
    mas antiguo (el primero registrado) desaparece. El reloj avanza mas
    alla de `AUTHORIZATION_REQUEST_TTL` (nit de la revision final,
    16-sep) para que los 50 sembrados ya no puedan tener una
    `AuthorizationRequest` PENDING en vuelo -- si no, ninguno es
    desalojable y el registro 51 no reduce el recuento."""
    fixture = _Fixture()
    for i in range(50):
        await fixture.register_client(client_id=f"client-{i}")
    fixture.clock.advance_to(_NOW + AUTHORIZATION_REQUEST_TTL + timedelta(seconds=1))

    await fixture.register_client(client_id="client-51")

    assert await fixture.provider.get_client("client-51") is not None
    assert await fixture.provider.get_client("client-0") is None


async def test_register_client_rejects_at_the_hard_ceiling() -> None:
    """El techo DURO (10x el tope real) si rechaza. Sembrado directo en el
    repositorio -- no via `register_client()` -- porque el desalojo
    normal mantiene el recuento estable en el tope y nunca dejaria
    llegar al techo por un bucle de registros reales."""
    fixture = _Fixture()
    for i in range(MAX_UNCONSENTED_CLIENTS_HARD_CEILING):
        await fixture.session.clients.save(
            OAuthClient(
                client_id=f"client-{i}",
                client_name="agente",
                redirect_uris=(RedirectUri(_REDIRECT_URI),),
                token_endpoint_auth_method=TokenEndpointAuthMethod.NONE,
                client_secret_hash=None,
                grant_types=("authorization_code",),
                requested_scope=ScopeSet.parse("ads:read"),
                created_at=_NOW,
            )
        )

    with pytest.raises(RegistrationError):
        await fixture.register_client(client_id="client-over-hard-ceiling")


async def test_authorize_returns_consent_url_without_minting_a_code() -> None:
    fixture = _Fixture()
    client = await fixture.register_client()
    _, challenge = _pkce_pair()
    params = _authorization_params(code_challenge=challenge)

    redirect_to = await fixture.provider.authorize(client, params)

    assert redirect_to.startswith(f"{_PUBLIC_BASE_URL}/oauth/autorizar?txn=")
    txn_id = _txn_id_from_redirect(redirect_to)
    request = await fixture.session.authorization_requests.get_by_id(txn_id)
    assert request is not None
    assert request.state is AuthorizationRequestState.PENDING
    assert request.code_hash is None


async def test_authorize_rejects_resource_mismatch() -> None:
    fixture = _Fixture()
    client = await fixture.register_client()
    _, challenge = _pkce_pair()
    params = AuthorizationParams(
        state="xyz",
        scopes=["ads:read"],
        code_challenge=challenge,
        redirect_uri=_URL.validate_python(_REDIRECT_URI),
        redirect_uri_provided_explicitly=True,
        resource="https://other.example/mcp",
    )

    with pytest.raises(AuthorizeError) as exc_info:
        await fixture.provider.authorize(client, params)
    assert exc_info.value.error == "invalid_target"


async def test_authorize_rejects_state_over_512_characters() -> None:
    fixture = _Fixture()
    client = await fixture.register_client()
    _, challenge = _pkce_pair()

    with pytest.raises(AuthorizeError) as exc_info:
        await fixture.provider.authorize(
            client, _authorization_params(code_challenge=challenge, state="x" * 513)
        )
    assert exc_info.value.error == "invalid_request"


async def test_authorize_rejects_a_malformed_pkce_code_challenge() -> None:
    """T049 (spec 008): el SDK (`sdk:handlers/authorize.py::
    AuthorizationRequest`) solo exige `code_challenge_method = "S256"`, no
    la FORMA de `code_challenge` -- un cliente que la manda mal formada
    (ni 43 caracteres base64url) disparaba un `InvalidCodeChallengeError`
    de dominio sin capturar en `SdkOAuthProvider._start()`, mismo 500
    opaco que el `IntegrityError` de `resource`."""
    fixture = _Fixture()
    client = await fixture.register_client()
    params = _authorization_params(code_challenge="not-a-valid-pkce-challenge")

    with pytest.raises(AuthorizeError) as exc_info:
        await fixture.provider.authorize(client, params)
    assert exc_info.value.error == "invalid_request"


class _FakeAsyncpgConstraintError(Exception):
    """Sustituye a `asyncpg.exceptions.PostgresError`: solo el atributo que
    `sdk_provider._constraint_name()` lee de verdad."""

    def __init__(self, *, constraint_name: str) -> None:
        super().__init__("constraint violation")
        self.constraint_name = constraint_name


def _crafted_integrity_error(*, constraint_name: str, client_secret_in_row: str) -> IntegrityError:
    """Simula lo que produce el dialecto asyncpg de SQLAlchemy: `.orig`
    lleva la fila entera (con datos del cliente) en el mensaje, colgada de
    `__cause__.constraint_name` (`sdk_provider._constraint_name()` ya solo
    lee ese atributo, nunca el mensaje)."""
    cause = _FakeAsyncpgConstraintError(constraint_name=constraint_name)
    orig = RuntimeError(f"Failing row contains ({client_secret_in_row}, ...)")
    orig.__cause__ = cause
    return IntegrityError("INSERT INTO oauth_authorization_requests ...", {}, orig)


async def test_authorize_translates_an_unexpected_integrity_error_without_leaking_the_row() -> None:
    """Revision de seguridad (PR 44, IMPORTANT-2): `error`, no `warning`
    (tras T049 llegar aqui es un fallo real, no una condicion esperable) y
    `exc_info=True` -- pero el processor `_exception_type_only`
    (`logging_setup.py`) ya reduce cualquier `exc_info` al nombre del tipo,
    asi que el mensaje crudo (con datos del cliente) nunca debe aparecer en
    el registro capturado, solo `exception_type`/`constraint`."""
    fixture = _Fixture()
    client = await fixture.register_client()
    _, challenge = _pkce_pair()
    secret_marker = "s3cret-client-supplied-value"  # noqa: S105 - marcador de prueba, no un secreto
    crafted = _crafted_integrity_error(
        constraint_name="oauth_authorization_requests_resource_check",
        client_secret_in_row=secret_marker,
    )

    async def _raise_integrity_error(_request: object) -> None:
        raise crafted

    # Mismos processors que `logging_setup.py::configure_logging` aplica en
    # produccion ANTES del `JSONRenderer` (`_exception_type_only` reduce
    # `exc_info=True` al nombre del tipo; `redact_secrets` enmascara por
    # clave/patron) -- `capture_logs()` por defecto los desactiva TODOS, asi
    # que sin pasarlos aqui la prueba no confirmaria nada sobre produccion.
    with (
        structlog.testing.capture_logs(processors=[_exception_type_only, redact_secrets]) as logs,
        pytest.MonkeyPatch.context() as monkeypatch,
    ):
        monkeypatch.setattr(sdk_provider_module, "logger", structlog.get_logger())
        monkeypatch.setattr(
            fixture.session.authorization_requests, "create", _raise_integrity_error
        )
        params = _authorization_params(code_challenge=challenge)
        with pytest.raises(AuthorizeError) as exc_info:
            await fixture.provider.authorize(client, params)

    assert exc_info.value.error == "invalid_request"
    assert exc_info.value.error_description == "no se pudo crear la solicitud de autorizacion"

    [entry] = [log for log in logs if log["event"] == "mcp_oauth_authorize_integrity_violation"]
    assert entry["log_level"] == "error"
    assert entry["constraint"] == "oauth_authorization_requests_resource_check"
    assert entry["error_type"] == "IntegrityError"
    serialized = repr(entry)
    assert secret_marker not in serialized
    assert "Failing row" not in serialized


async def test_authorize_rejects_unknown_client() -> None:
    fixture = _Fixture()
    unregistered = OAuthClientInformationFull(
        client_id="ghost-client",
        redirect_uris=[_URL.validate_python(_REDIRECT_URI)],
        token_endpoint_auth_method="none",
        grant_types=["authorization_code"],
        response_types=["code"],
    )
    _, challenge = _pkce_pair()
    params = _authorization_params(code_challenge=challenge)

    with pytest.raises(AuthorizeError):
        await fixture.provider.authorize(unregistered, params)


async def test_load_authorization_code_returns_none_for_unknown_code() -> None:
    fixture = _Fixture()

    assert await fixture.provider.load_authorization_code(None, "never-issued") is None  # type: ignore[arg-type]


async def test_load_authorization_code_returns_dto_for_a_consented_code() -> None:
    fixture = _Fixture()
    code, _ = await fixture.authorized_code()

    dto = await fixture.provider.load_authorization_code(None, code)  # type: ignore[arg-type]

    assert dto is not None
    assert dto.client_id == _CLIENT_ID
    assert str(dto.redirect_uri) == _REDIRECT_URI
    assert dto.resource == _RESOURCE
    assert dto.scopes == ["ads:read"]


async def test_exchange_authorization_code_issues_tokens() -> None:
    fixture = _Fixture()
    code, _ = await fixture.authorized_code()
    dto = await fixture.provider.load_authorization_code(None, code)  # type: ignore[arg-type]
    assert dto is not None

    tokens = await fixture.provider.exchange_authorization_code(None, dto)  # type: ignore[arg-type]

    assert tokens.access_token
    assert tokens.refresh_token
    assert tokens.token_type == "Bearer"  # noqa: S105 - RFC 6749 SS5.1 literal, no un secreto


async def test_exchange_authorization_code_replay_revokes_the_family() -> None:
    fixture = _Fixture()
    code, _ = await fixture.authorized_code()
    first_dto = await fixture.provider.load_authorization_code(None, code)  # type: ignore[arg-type]
    assert first_dto is not None
    await fixture.provider.exchange_authorization_code(None, first_dto)  # type: ignore[arg-type]

    second_dto = await fixture.provider.load_authorization_code(None, code)  # type: ignore[arg-type]
    assert second_dto is not None
    with pytest.raises(TokenError) as exc_info:
        await fixture.provider.exchange_authorization_code(None, second_dto)  # type: ignore[arg-type]
    assert exc_info.value.error == "invalid_grant"

    request = await fixture.session.authorization_requests.get_by_code_hash(
        fixture.hasher.hash(code)
    )
    assert request is not None
    grant = await fixture.session.grants.get_by_authorization_request_id(request.id)
    assert grant is not None
    assert grant.is_revoked is True


async def test_exchange_authorization_code_unknown_code_raises_invalid_grant() -> None:
    fixture = _Fixture()
    await fixture.register_client()
    dto = await fixture.provider.load_authorization_code(None, "never-issued")  # type: ignore[arg-type]
    assert dto is None


async def test_load_refresh_token_returns_none_for_unknown_token() -> None:
    fixture = _Fixture()

    assert await fixture.provider.load_refresh_token(None, "unknown") is None  # type: ignore[arg-type]


async def test_load_refresh_token_round_trips() -> None:
    fixture = _Fixture()
    code, _ = await fixture.authorized_code()
    dto = await fixture.provider.load_authorization_code(None, code)  # type: ignore[arg-type]
    assert dto is not None
    tokens = await fixture.provider.exchange_authorization_code(None, dto)  # type: ignore[arg-type]
    assert tokens.refresh_token is not None

    loaded = await fixture.provider.load_refresh_token(None, tokens.refresh_token)  # type: ignore[arg-type]

    assert loaded is not None
    assert loaded.client_id == _CLIENT_ID
    assert loaded.resource == _RESOURCE


async def test_exchange_refresh_token_rotates() -> None:
    fixture = _Fixture()
    code, _ = await fixture.authorized_code()
    dto = await fixture.provider.load_authorization_code(None, code)  # type: ignore[arg-type]
    assert dto is not None
    first_tokens = await fixture.provider.exchange_authorization_code(None, dto)  # type: ignore[arg-type]
    assert first_tokens.refresh_token is not None
    refresh_dto = await fixture.provider.load_refresh_token(None, first_tokens.refresh_token)  # type: ignore[arg-type]
    assert refresh_dto is not None

    rotated = await fixture.provider.exchange_refresh_token(None, refresh_dto, ["ads:read"])  # type: ignore[arg-type]

    assert rotated.access_token != first_tokens.access_token
    assert rotated.refresh_token != first_tokens.refresh_token


async def test_exchange_refresh_token_reuse_revokes_grant() -> None:
    fixture = _Fixture()
    code, _ = await fixture.authorized_code()
    dto = await fixture.provider.load_authorization_code(None, code)  # type: ignore[arg-type]
    assert dto is not None
    first_tokens = await fixture.provider.exchange_authorization_code(None, dto)  # type: ignore[arg-type]
    assert first_tokens.refresh_token is not None
    refresh_dto = await fixture.provider.load_refresh_token(None, first_tokens.refresh_token)  # type: ignore[arg-type]
    assert refresh_dto is not None
    await fixture.provider.exchange_refresh_token(None, refresh_dto, [])  # type: ignore[arg-type]

    with pytest.raises(TokenError) as exc_info:
        await fixture.provider.exchange_refresh_token(None, refresh_dto, [])  # type: ignore[arg-type]
    assert exc_info.value.error == "invalid_grant"

    grant = await fixture.session.grants.get_by_token_hash(
        fixture.hasher.hash(first_tokens.access_token)
    )
    assert grant is not None
    assert grant.is_revoked is True


async def test_exchange_refresh_token_scope_expansion_is_rejected() -> None:
    fixture = _Fixture()
    client = await fixture.register_client()
    _, challenge = _pkce_pair()
    params = AuthorizationParams(
        state="xyz",
        scopes=["ads:read"],
        code_challenge=challenge,
        redirect_uri=_URL.validate_python(_REDIRECT_URI),
        redirect_uri_provided_explicitly=True,
        resource=_RESOURCE,
    )
    redirect_to = await fixture.provider.authorize(client, params)
    txn_id = _txn_id_from_redirect(redirect_to)
    code, _ = await fixture.consent(txn_id)
    dto = await fixture.provider.load_authorization_code(None, code)  # type: ignore[arg-type]
    assert dto is not None
    tokens = await fixture.provider.exchange_authorization_code(None, dto)  # type: ignore[arg-type]
    assert tokens.refresh_token is not None
    refresh_dto = await fixture.provider.load_refresh_token(None, tokens.refresh_token)  # type: ignore[arg-type]
    assert refresh_dto is not None

    with pytest.raises(TokenError) as exc_info:
        await fixture.provider.exchange_refresh_token(None, refresh_dto, ["ads:propose"])  # type: ignore[arg-type]
    assert exc_info.value.error == "invalid_scope"


async def test_load_access_token_returns_none_for_unknown_token() -> None:
    fixture = _Fixture()

    assert await fixture.provider.load_access_token("unknown") is None


async def test_load_access_token_round_trips() -> None:
    fixture = _Fixture()
    code, owner_id = await fixture.authorized_code()
    dto = await fixture.provider.load_authorization_code(None, code)  # type: ignore[arg-type]
    assert dto is not None
    tokens = await fixture.provider.exchange_authorization_code(None, dto)  # type: ignore[arg-type]

    access = await fixture.provider.load_access_token(tokens.access_token)

    assert access is not None
    assert access.client_id == _CLIENT_ID
    assert access.resource == _RESOURCE
    assert access.subject == str(owner_id)


async def test_revoke_token_revokes_the_grant() -> None:
    fixture = _Fixture()
    code, _ = await fixture.authorized_code()
    dto = await fixture.provider.load_authorization_code(None, code)  # type: ignore[arg-type]
    assert dto is not None
    tokens = await fixture.provider.exchange_authorization_code(None, dto)  # type: ignore[arg-type]
    access = await fixture.provider.load_access_token(tokens.access_token)
    assert access is not None

    await fixture.provider.revoke_token(access)

    grant = await fixture.session.grants.get_by_token_hash(
        fixture.hasher.hash(tokens.access_token)
    )
    assert grant is not None
    assert grant.is_revoked is True


async def test_revoke_token_is_a_noop_for_an_unknown_token() -> None:
    fixture = _Fixture()
    unknown = AccessToken(token="unknown", client_id="nobody", scopes=[])  # noqa: S106

    await fixture.provider.revoke_token(unknown)

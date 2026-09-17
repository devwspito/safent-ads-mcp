"""`build_consent_router` (tasks.md T015/T015+, contracts/oauth.md SS5/SS9)
contra Postgres real: sin sesion 401; txn ajeno/caducado 404/410; `approve`
sin `X-Reauth-Token` 401 `REAUTH_REQUIRED`; `approve` devuelve `redirect_to`
con `code`+`state`+`iss`; el mismo codigo TOTP no puede confirmar dos
consentimientos (threat-model.md C-41, `totp_reauth_confirmations`).

002b (tasks.md T035, threat-model.md C-71): `approve` con un dueno federado
fresco pasa SIN ninguna cabecera; con la frescura vencida, 401 con
`details.methods == ["federated"]`; los casos TOTP de arriba no cambian de
expectativa; un dueno sin TOTP con el login federado apagado sigue dando el
401 legado sin `details` (= comportamiento de 002).

Router mount aislado (FastAPI vacia, mismo patron que
`tests/integration/accounts/test_platform_apps_router.py`): el CSRF de
doble envio y el limite de tasa ya se prueban en
`tests/integration/composition/test_oauth_routes_registered.py`, no aqui."""

from __future__ import annotations

import hashlib
import json
import uuid
from collections.abc import AsyncIterator, Awaitable, Callable, Mapping
from datetime import UTC, datetime, timedelta
from urllib.parse import parse_qs, urlsplit

import httpx
import pyotp
import pytest
from fastapi import FastAPI
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine

from safent_ads.composition.api import _handle_api_error
from safent_ads.composition.container import Container
from safent_ads.iam.infrastructure.aesgcm_totp_cipher import (
    PURPOSE_TOTP_SECRET,
    AesGcmTotpCipher,
)
from safent_ads.iam.presentation.dependencies import SESSION_COOKIE_NAME
from safent_ads.iam.presentation.errors import ApiError
from safent_ads.mcp_oauth.domain.authorization import AuthorizationRequest
from safent_ads.mcp_oauth.domain.client import OAuthClient, RedirectUri, TokenEndpointAuthMethod
from safent_ads.mcp_oauth.domain.resource import ResourceIndicator
from safent_ads.mcp_oauth.domain.scope import ScopeSet
from safent_ads.mcp_oauth.infrastructure.secrets_token_factory import SecretsOpaqueTokenFactory
from safent_ads.mcp_oauth.infrastructure.sha256_token_hasher import Sha256TokenHasher
from safent_ads.mcp_oauth.infrastructure.sql_authorization_repository import (
    SqlAuthorizationRequestRepository,
)
from safent_ads.mcp_oauth.infrastructure.sql_client_repository import SqlClientRepository
from safent_ads.mcp_oauth.presentation.consent_router import build_consent_router
from safent_ads.shared.clock import SystemClock
from tests.unit.composition.factories import build_api_settings

pytestmark = pytest.mark.integration

# 002b research.md Decision B, session_policy.FEDERATED_IDENTIFICATION_TTL:
# cualquier valor mayor cuenta como "vencida" para estas pruebas.
_STALE_FEDERATED_WINDOW = timedelta(minutes=6)

_VALID_32_BYTE_KEY_B64 = "MDAwMDAwMDAwMDAwMDAwMDAwMDAwMDAwMDAwMDAwMDA="
_TOTP_SECRET = pyotp.random_base32()
_PUBLIC_BASE_URL = "https://ads.test.ts.net"
_RESOURCE = ResourceIndicator(f"{_PUBLIC_BASE_URL}/mcp")
_CODE_CHALLENGE = "E9Melhoa2OwvFrEMTJguCHaoeK1t8URWbuGJSstw-cM"
_REDIRECT_URI = "http://127.0.0.1:54321/callback"
# Prefijo propio de este fichero: la limpieza al final de cada prueba borra
# por el (CASCADE se lleva sus solicitudes/concesiones) para no inflar
# `count_unconsented()`/otras consultas GLOBALES que otros ficheros de esta
# misma suite corren contra el mismo Postgres compartido de sesion.
_CLIENT_ID_PREFIX = "client-consent-router-test-"


class _SeededOwner:
    def __init__(self, *, owner_id: uuid.UUID, raw_token: str) -> None:
        self.owner_id = owner_id
        self.raw_token = raw_token


async def _seed_owner(session: AsyncSession) -> _SeededOwner:
    owner_id = uuid.uuid4()
    raw_token = f"consent-router-test-token-{uuid.uuid4().hex}"
    now = datetime.now(UTC)
    encrypted_secret = AesGcmTotpCipher(_VALID_32_BYTE_KEY_B64).encrypt(
        _TOTP_SECRET, purpose=PURPOSE_TOTP_SECRET
    )
    await session.execute(
        text(
            "INSERT INTO owners (id, email, password_hash, totp_secret_encrypted, "
            "totp_confirmed_at) VALUES (:id, :email, :password_hash, :totp_secret, :now)"
        ),
        {
            "id": str(owner_id),
            "email": f"owner-{owner_id.hex[:8]}@safent.example",
            "password_hash": "argon2id$fixture$not-a-real-hash",  # noqa: S106
            "totp_secret": encrypted_secret,
            "now": now,
        },
    )
    await session.execute(
        text(
            "INSERT INTO sessions (id, owner_id, token_hash, created_at, expires_at) "
            "VALUES (:id, :owner_id, :token_hash, :created_at, :expires_at)"
        ),
        {
            "id": str(uuid.uuid4()),
            "owner_id": str(owner_id),
            "token_hash": hashlib.sha256(raw_token.encode("utf-8")).hexdigest(),
            "created_at": now,
            "expires_at": now + timedelta(hours=1),
        },
    )
    return _SeededOwner(owner_id=owner_id, raw_token=raw_token)


def _oauth_client(client_id: str) -> OAuthClient:
    return OAuthClient(
        client_id=client_id,
        client_name="Claude Code",
        redirect_uris=(RedirectUri(_REDIRECT_URI),),
        token_endpoint_auth_method=TokenEndpointAuthMethod.NONE,
        client_secret_hash=None,
        grant_types=("authorization_code", "refresh_token"),
        requested_scope=ScopeSet.parse("ads:read ads:propose"),
        created_at=datetime.now(UTC),
    )


def _authorization_request(
    *, client_id: str, expired: bool = False, state: str | None = "xyz"
) -> AuthorizationRequest:
    now = datetime.now(UTC)
    created_at = now - timedelta(minutes=20) if expired else now
    expires_at = now - timedelta(minutes=10) if expired else now + timedelta(minutes=10)
    return AuthorizationRequest(
        txn_id=uuid.uuid4(),
        client_id=client_id,
        redirect_uri=_REDIRECT_URI,
        code_challenge=_CODE_CHALLENGE,
        client_state=state,
        scope_set=ScopeSet.parse("ads:read ads:propose"),
        resource=_RESOURCE,
        created_at=created_at,
        expires_at=expires_at,
    )


@pytest.fixture
async def owner(database_url: str) -> AsyncIterator[_SeededOwner]:
    engine = create_async_engine(database_url, pool_pre_ping=True)
    async with AsyncSession(engine, expire_on_commit=False) as session:
        seeded = await _seed_owner(session)
        await session.commit()
    try:
        yield seeded
    finally:
        async with engine.begin() as connection:
            await connection.execute(
                text("DELETE FROM sessions WHERE owner_id = :id"), {"id": str(seeded.owner_id)}
            )
            # 0020_totp_replay_guard: `require_reauth` quema el time_step en
            # `totp_reauth_confirmations` (FK RESTRICT hacia `owners`).
            await connection.execute(
                text("DELETE FROM totp_reauth_confirmations WHERE owner_id = :id"),
                {"id": str(seeded.owner_id)},
            )
            await connection.execute(
                text("DELETE FROM owners WHERE id = :id"), {"id": str(seeded.owner_id)}
            )
        await engine.dispose()


async def _seed_federated_owner(
    session: AsyncSession, *, last_federated_auth_at: datetime, with_totp: bool
) -> _SeededOwner:
    """Dueno con `sessions.origin='federated'` y una identidad atada en
    `owner_federated_identities` (002b tasks.md T035). `with_totp=False`
    reproduce el dueno que solo entra con Google, TAL cual FR-109 lo deja."""
    owner_id = uuid.uuid4()
    raw_token = f"consent-router-federated-token-{uuid.uuid4().hex}"
    now = datetime.now(UTC)
    encrypted_secret = (
        AesGcmTotpCipher(_VALID_32_BYTE_KEY_B64).encrypt(_TOTP_SECRET, purpose=PURPOSE_TOTP_SECRET)
        if with_totp
        else None
    )
    await session.execute(
        text(
            "INSERT INTO owners (id, email, password_hash, totp_secret_encrypted, "
            "totp_confirmed_at) VALUES (:id, :email, :password_hash, :totp_secret, :confirmed_at)"
        ),
        {
            "id": str(owner_id),
            "email": f"federated-owner-{owner_id.hex[:8]}@example.com",
            "password_hash": "argon2id$fixture$not-a-real-hash",  # noqa: S106
            "totp_secret": encrypted_secret,
            "confirmed_at": now if with_totp else None,
        },
    )
    await session.execute(
        text(
            "INSERT INTO sessions (id, owner_id, token_hash, created_at, expires_at, "
            "origin, last_federated_auth_at) "
            "VALUES (:id, :owner_id, :token_hash, :created_at, :expires_at, "
            "'federated', :last_federated_auth_at)"
        ),
        {
            "id": str(uuid.uuid4()),
            "owner_id": str(owner_id),
            "token_hash": hashlib.sha256(raw_token.encode("utf-8")).hexdigest(),
            "created_at": now,
            "expires_at": now + timedelta(hours=1),
            "last_federated_auth_at": last_federated_auth_at,
        },
    )
    await session.execute(
        text(
            "INSERT INTO owner_federated_identities (owner_id, issuer, subject, "
            "email_at_binding, bound_at, last_seen_at) VALUES "
            "(:owner_id, 'https://accounts.google.com', :subject, :email, :now, :now)"
        ),
        {
            "owner_id": str(owner_id),
            "subject": f"google-sub-{owner_id.hex[:8]}",
            "email": f"federated-owner-{owner_id.hex[:8]}@example.com",
            "now": now,
        },
    )
    return _SeededOwner(owner_id=owner_id, raw_token=raw_token)


@pytest.fixture
async def federated_owner_factory(
    database_url: str,
) -> AsyncIterator[Callable[..., Awaitable[_SeededOwner]]]:
    engine = create_async_engine(database_url, pool_pre_ping=True)
    created_owner_ids: list[uuid.UUID] = []

    async def factory(*, last_federated_auth_at: datetime, with_totp: bool = False) -> _SeededOwner:
        async with AsyncSession(engine, expire_on_commit=False) as session:
            seeded = await _seed_federated_owner(
                session, last_federated_auth_at=last_federated_auth_at, with_totp=with_totp
            )
            await session.commit()
        created_owner_ids.append(seeded.owner_id)
        return seeded

    try:
        yield factory
    finally:
        async with engine.begin() as connection:
            for owner_id in created_owner_ids:
                await connection.execute(
                    text("DELETE FROM totp_reauth_confirmations WHERE owner_id = :id"),
                    {"id": str(owner_id)},
                )
                await connection.execute(
                    text("DELETE FROM sessions WHERE owner_id = :id"), {"id": str(owner_id)}
                )
                await connection.execute(
                    text("DELETE FROM owner_federated_identities WHERE owner_id = :id"),
                    {"id": str(owner_id)},
                )
                await connection.execute(
                    text("DELETE FROM owners WHERE id = :id"), {"id": str(owner_id)}
                )
        await engine.dispose()


@pytest.fixture(autouse=True)
async def _cleanup_seeded_clients(database_url: str) -> AsyncIterator[None]:
    """`oauth_clients.client_id -> oauth_authorization_requests`/
    `oauth_grants` son CASCADE (0035_mcp_oauth): borrar el cliente por
    `_CLIENT_ID_PREFIX` se lleva por delante todo lo que este fichero
    sembro, sin dejar clientes sin consentir que infle
    `count_unconsented()` en `test_sql_repositories.py`."""
    yield
    engine = create_async_engine(database_url, pool_pre_ping=True)
    async with engine.begin() as connection:
        await connection.execute(
            text("DELETE FROM oauth_clients WHERE client_id LIKE :prefix"),
            {"prefix": f"{_CLIENT_ID_PREFIX}%"},
        )
    await engine.dispose()


def _build_consent_app(
    database_url: str, *, federated_available: bool
) -> tuple[FastAPI, Container]:
    settings = build_api_settings(
        database_url=database_url, public_base_url=_PUBLIC_BASE_URL, mcp_oauth_enabled=True
    )
    container = Container.build(settings)
    fastapi_app = FastAPI()
    fastapi_app.state.container = container
    fastapi_app.add_exception_handler(ApiError, _handle_api_error)
    fastapi_app.include_router(
        build_consent_router(
            token_hasher=Sha256TokenHasher(),
            token_factory=SecretsOpaqueTokenFactory(),
            totp_enc_key=_VALID_32_BYTE_KEY_B64,
            public_base_url=_PUBLIC_BASE_URL,
            federated_available=federated_available,
        )
    )
    return fastapi_app, container


@pytest.fixture
async def app(database_url: str) -> AsyncIterator[FastAPI]:
    fastapi_app, container = _build_consent_app(database_url, federated_available=False)
    try:
        yield fastapi_app
    finally:
        await container.aclose()


@pytest.fixture
async def federated_app(database_url: str) -> AsyncIterator[FastAPI]:
    """002b: el login federado esta activo (`federated_available=True`), lo
    que hace que `require_fresh_identification` compruebe frescura federada
    ademas de `X-Reauth-Token`."""
    fastapi_app, container = _build_consent_app(database_url, federated_available=True)
    try:
        yield fastapi_app
    finally:
        await container.aclose()


async def _seed_client_and_request(
    database_url: str, *, expired: bool = False, state: str | None = "xyz"
) -> tuple[OAuthClient, AuthorizationRequest]:
    engine = create_async_engine(database_url, pool_pre_ping=True)
    client = _oauth_client(f"{_CLIENT_ID_PREFIX}{uuid.uuid4().hex[:8]}")
    request = _authorization_request(client_id=client.id, expired=expired, state=state)
    async with AsyncSession(engine, expire_on_commit=False) as session:
        await SqlClientRepository(session).save(client)
        await SqlAuthorizationRequestRepository(session, clock=SystemClock()).create(request)
        await session.commit()
    await engine.dispose()
    return client, request


# Revision de codigo (17-sep, nit de f36fb2a6): una fila anterior a D-11
# (destino remoto) que el dominio ya no sabe construir -- solo se puede
# sembrar con SQL crudo, exactamente como la dejo una version anterior del
# motor (mismo patron que `test_grants_router.py::_seed_legacy_client_and_grant`).
_INSERT_LEGACY_CLIENT_SQL = text("""
    INSERT INTO oauth_clients
        (client_id, client_name, redirect_uris, token_endpoint_auth_method,
         client_secret_hash, grant_types, requested_scopes, created_at, last_seen_at)
    VALUES
        (:client_id, 'Claude Code', CAST(:redirect_uris AS jsonb), 'none',
         NULL, CAST(:grant_types AS jsonb), 'ads:read ads:propose', :now, NULL)
""")


async def _seed_legacy_client_and_pending_request(
    database_url: str,
) -> tuple[str, AuthorizationRequest]:
    """Cliente con un `redirect_uri` remoto (invalido desde D-11) y una
    `AuthorizationRequest` PENDING que lo referencia -- para que `GET
    /consent/{txn_id}` lea la fila del cliente y `_row_to_client` levante
    `RedirectUri`'s `DomainError` al construirla."""
    engine = create_async_engine(database_url, pool_pre_ping=True)
    client_id = f"{_CLIENT_ID_PREFIX}legado-{uuid.uuid4().hex[:8]}"
    now = datetime.now(UTC)
    request = _authorization_request(client_id=client_id)
    async with AsyncSession(engine, expire_on_commit=False) as session:
        await session.execute(
            _INSERT_LEGACY_CLIENT_SQL,
            {
                "client_id": client_id,
                "redirect_uris": json.dumps(["https://evil.example/callback"]),
                "grant_types": json.dumps(["authorization_code", "refresh_token"]),
                "now": now,
            },
        )
        await SqlAuthorizationRequestRepository(session, clock=SystemClock()).create(request)
        await session.commit()
    await engine.dispose()
    return client_id, request


def _client_for(app: FastAPI, *, raw_token: str | None) -> httpx.AsyncClient:
    cookies = {SESSION_COOKIE_NAME: raw_token} if raw_token else {}
    return httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="https://test", cookies=cookies
    )


def _reauth_headers(*, time_step_offset: int = 0) -> Mapping[str, str]:
    """`time_step_offset` != 0 pide el codigo de OTRO paso de 30 s (dentro
    de la tolerancia +/-1 de `PyotpTotpVerifier`): dos llamadas
    `require_reauth` en la misma prueba necesitan dos codigos DISTINTOS --
    el mismo ya quemado (`totp_reauth_confirmations`, C-41) siempre
    devuelve `REAUTH_REQUIRED`, nunca llega a la comprobacion de negocio."""
    moment = datetime.now(UTC) + timedelta(seconds=30 * time_step_offset)
    return {"X-Reauth-Token": pyotp.TOTP(_TOTP_SECRET).at(moment)}


async def test_get_consent_without_session_is_401(app: FastAPI, database_url: str) -> None:
    client, request = await _seed_client_and_request(database_url)
    async with _client_for(app, raw_token=None) as http_client:
        response = await http_client.get(f"/api/v1/mcp-oauth/consent/{request.id}")

    assert response.status_code == 401


async def test_get_consent_unknown_txn_is_404(app: FastAPI, owner: _SeededOwner) -> None:
    async with _client_for(app, raw_token=owner.raw_token) as http_client:
        response = await http_client.get(f"/api/v1/mcp-oauth/consent/{uuid.uuid4()}")

    assert response.status_code == 404
    assert response.json()["error"]["code"] == "NOT_FOUND"


async def test_get_consent_expired_txn_is_410(
    app: FastAPI, owner: _SeededOwner, database_url: str
) -> None:
    _, request = await _seed_client_and_request(database_url, expired=True)
    async with _client_for(app, raw_token=owner.raw_token) as http_client:
        response = await http_client.get(f"/api/v1/mcp-oauth/consent/{request.id}")

    assert response.status_code == 410
    assert response.json()["error"]["code"] == "TXN_EXPIRED"


async def test_get_consent_returns_the_contract_shape(
    app: FastAPI, owner: _SeededOwner, database_url: str
) -> None:
    client, request = await _seed_client_and_request(database_url)
    async with _client_for(app, raw_token=owner.raw_token) as http_client:
        response = await http_client.get(f"/api/v1/mcp-oauth/consent/{request.id}")

    assert response.status_code == 200
    body = response.json()
    assert body["txn_id"] == str(request.id)
    assert body["client_id"] == client.id
    assert body["client_name"] == "Claude Code"
    assert body["redirect_host"] == "127.0.0.1:54321"
    assert body["scopes"] == [
        {"name": "ads:read", "label": "Leer tu cartera, señales y registro"},
        {"name": "ads:propose", "label": "Crear propuestas (siguen necesitando tu aprobación)"},
    ]


async def test_get_consent_with_a_client_row_that_violates_a_domain_rule_degrades_like_unknown(
    app: FastAPI, owner: _SeededOwner, database_url: str
) -> None:
    """Revision de codigo (17-sep, nit de f36fb2a6): antes,
    `SqlClientRepository.get_by_id()` dejaba escapar el `DomainError` de
    `RedirectUri` hasta el manejador generico (500). Ahora se trata
    EXACTAMENTE igual que un cliente ausente -- el mismo fallback que
    `_to_consent_response` ya tenia para `client=None`."""
    client_id, request = await _seed_legacy_client_and_pending_request(database_url)
    async with _client_for(app, raw_token=owner.raw_token) as http_client:
        response = await http_client.get(f"/api/v1/mcp-oauth/consent/{request.id}")

    assert response.status_code == 200
    body = response.json()
    assert body["client_id"] == client_id
    assert body["client_name"] == client_id
    assert "evil.example" not in response.text


async def test_approve_without_session_is_401(app: FastAPI, database_url: str) -> None:
    _, request = await _seed_client_and_request(database_url)
    async with _client_for(app, raw_token=None) as http_client:
        response = await http_client.post(f"/api/v1/mcp-oauth/consent/{request.id}/approve")

    assert response.status_code == 401


async def test_approve_unknown_txn_is_404(app: FastAPI, owner: _SeededOwner) -> None:
    async with _client_for(app, raw_token=owner.raw_token) as http_client:
        response = await http_client.post(
            f"/api/v1/mcp-oauth/consent/{uuid.uuid4()}/approve", headers=_reauth_headers()
        )

    assert response.status_code == 404


async def test_approve_without_reauth_token_is_reauth_required(
    app: FastAPI, owner: _SeededOwner, database_url: str
) -> None:
    _, request = await _seed_client_and_request(database_url)
    async with _client_for(app, raw_token=owner.raw_token) as http_client:
        response = await http_client.post(f"/api/v1/mcp-oauth/consent/{request.id}/approve")

    assert response.status_code == 401
    assert response.json()["error"]["code"] == "REAUTH_REQUIRED"


async def test_approve_returns_redirect_to_with_code_state_and_iss(
    app: FastAPI, owner: _SeededOwner, database_url: str
) -> None:
    client, request = await _seed_client_and_request(database_url, state="xyz")
    async with _client_for(app, raw_token=owner.raw_token) as http_client:
        response = await http_client.post(
            f"/api/v1/mcp-oauth/consent/{request.id}/approve", headers=_reauth_headers()
        )

    assert response.status_code == 200, response.text
    redirect_to = response.json()["redirect_to"]
    parsed = urlsplit(redirect_to)
    assert f"{parsed.scheme}://{parsed.netloc}{parsed.path}" == _REDIRECT_URI
    query = parse_qs(parsed.query)
    assert query["code"][0]
    assert query["state"] == ["xyz"]
    assert query["iss"] == [_PUBLIC_BASE_URL]


async def test_approve_with_a_client_row_that_violates_a_domain_rule_is_rejected_not_500(
    app: FastAPI, owner: _SeededOwner, database_url: str
) -> None:
    """I-2 (revision de seguridad 17-sep): a diferencia de `GET` (que
    degrada mostrando el `client_id` crudo, item 9), `approve` NUNCA debe
    emitir un codigo para un cliente cuya fila ya no cumple una regla del
    dominio -- `sql_client_repository.py` declara "autorizar y consentir
    fallan cerrado". Mismo cuerpo que un `txn_id` desconocido, nunca 500."""
    _client_id, request = await _seed_legacy_client_and_pending_request(database_url)
    async with _client_for(app, raw_token=owner.raw_token) as http_client:
        response = await http_client.post(
            f"/api/v1/mcp-oauth/consent/{request.id}/approve", headers=_reauth_headers()
        )

    assert response.status_code == 404
    assert response.json()["error"]["code"] == "NOT_FOUND"


async def test_approve_of_an_already_resolved_txn_is_409(
    app: FastAPI, owner: _SeededOwner, database_url: str
) -> None:
    client, request = await _seed_client_and_request(database_url)
    async with _client_for(app, raw_token=owner.raw_token) as http_client:
        first = await http_client.post(
            f"/api/v1/mcp-oauth/consent/{request.id}/approve", headers=_reauth_headers()
        )
        assert first.status_code == 200, first.text

        second = await http_client.post(
            f"/api/v1/mcp-oauth/consent/{request.id}/approve",
            headers=_reauth_headers(time_step_offset=1),
        )

    assert second.status_code == 409
    assert second.json()["error"]["code"] == "TXN_NOT_PENDING"


async def test_same_totp_code_cannot_confirm_two_consents(
    app: FastAPI, owner: _SeededOwner, database_url: str
) -> None:
    """threat-model.md C-41 (0020_totp_replay_guard): el contador RFC 6238
    que hizo match se quema -- reusarlo para una segunda transaccion falla,
    aunque la primera consintiera una solicitud distinta."""
    _, request_a = await _seed_client_and_request(database_url)
    _, request_b = await _seed_client_and_request(database_url)
    shared_code = pyotp.TOTP(_TOTP_SECRET).now()

    async with _client_for(app, raw_token=owner.raw_token) as http_client:
        first = await http_client.post(
            f"/api/v1/mcp-oauth/consent/{request_a.id}/approve",
            headers={"X-Reauth-Token": shared_code},
        )
        assert first.status_code == 200, first.text

        second = await http_client.post(
            f"/api/v1/mcp-oauth/consent/{request_b.id}/approve",
            headers={"X-Reauth-Token": shared_code},
        )

    assert second.status_code == 401
    assert second.json()["error"]["code"] == "REAUTH_REQUIRED"


async def test_deny_needs_no_totp_and_returns_redirect_with_error_and_iss(
    app: FastAPI, owner: _SeededOwner, database_url: str
) -> None:
    _, request = await _seed_client_and_request(database_url, state="xyz")
    async with _client_for(app, raw_token=owner.raw_token) as http_client:
        response = await http_client.post(f"/api/v1/mcp-oauth/consent/{request.id}/deny")

    assert response.status_code == 200, response.text
    parsed = urlsplit(response.json()["redirect_to"])
    query = parse_qs(parsed.query)
    assert query["error"] == ["access_denied"]
    assert query["state"] == ["xyz"]
    assert query["iss"] == [_PUBLIC_BASE_URL]


async def test_deny_unknown_txn_is_404(app: FastAPI, owner: _SeededOwner) -> None:
    async with _client_for(app, raw_token=owner.raw_token) as http_client:
        response = await http_client.post(f"/api/v1/mcp-oauth/consent/{uuid.uuid4()}/deny")

    assert response.status_code == 404


async def test_fresh_federated_owner_approves_without_any_header(
    federated_app: FastAPI,
    federated_owner_factory: Callable[..., Awaitable[_SeededOwner]],
    database_url: str,
) -> None:
    """002b threat-model.md C-71: la frescura federada satisface
    `require_fresh_identification` exactamente igual que un TOTP fresco --
    sin `X-Reauth-Token`, un clic."""
    federated_owner = await federated_owner_factory(last_federated_auth_at=datetime.now(UTC))
    _, request = await _seed_client_and_request(database_url)
    async with _client_for(federated_app, raw_token=federated_owner.raw_token) as http_client:
        response = await http_client.post(f"/api/v1/mcp-oauth/consent/{request.id}/approve")

    assert response.status_code == 200, response.text


async def test_stale_federated_freshness_returns_401_with_methods_federated(
    federated_app: FastAPI,
    federated_owner_factory: Callable[..., Awaitable[_SeededOwner]],
    database_url: str,
) -> None:
    stale_at = datetime.now(UTC) - _STALE_FEDERATED_WINDOW
    federated_owner = await federated_owner_factory(last_federated_auth_at=stale_at)
    _, request = await _seed_client_and_request(database_url)
    async with _client_for(federated_app, raw_token=federated_owner.raw_token) as http_client:
        response = await http_client.post(f"/api/v1/mcp-oauth/consent/{request.id}/approve")

    assert response.status_code == 401
    body = response.json()["error"]
    assert body["code"] == "REAUTH_REQUIRED"
    assert body["details"]["methods"] == ["federated"]


async def test_owner_without_totp_and_federated_off_gets_the_legacy_401_without_details(
    app: FastAPI,
    federated_owner_factory: Callable[..., Awaitable[_SeededOwner]],
    database_url: str,
) -> None:
    """threat-model.md C-71.iv: `methods` vacio (federado apagado + sin
    TOTP) es, literalmente, el 401 de 002 -- sin `details` (FR-106: nunca
    "TOTP no habilitado" a un dueno con una via viable, pero este dueno no
    tiene ninguna)."""
    owner_without_totp = await federated_owner_factory(
        last_federated_auth_at=datetime.now(UTC), with_totp=False
    )
    _, request = await _seed_client_and_request(database_url)
    async with _client_for(app, raw_token=owner_without_totp.raw_token) as http_client:
        response = await http_client.post(f"/api/v1/mcp-oauth/consent/{request.id}/approve")

    assert response.status_code == 401
    body = response.json()["error"]
    assert body["code"] == "REAUTH_REQUIRED"
    assert body["details"] == {}


async def test_federated_off_never_reads_a_federated_mark_even_for_a_totp_owner(
    app: FastAPI,
    federated_owner_factory: Callable[..., Awaitable[_SeededOwner]],
    database_url: str,
) -> None:
    """T064 security review (C-81): un dueno con TOTP Y una marca federada
    FRESCA (de hace 1 minuto) sigue dando 401 sin cabecera cuando el
    interruptor esta apagado -- `federated_available=False` corta la
    lectura de `last_federated_auth_at` de raiz, nunca llega a mirar si la
    marca esta fresca. Antes de C-81, la marca federada NO se compartia
    con TOTP; esto es una foto de esa invariante restaurada, con un dueno
    que SI tiene TOTP para distinguirlo del caso "sin ninguna via"."""
    federated_owner = await federated_owner_factory(
        last_federated_auth_at=datetime.now(UTC) - timedelta(minutes=1), with_totp=True
    )
    _, request = await _seed_client_and_request(database_url)
    async with _client_for(app, raw_token=federated_owner.raw_token) as http_client:
        response = await http_client.post(f"/api/v1/mcp-oauth/consent/{request.id}/approve")

    assert response.status_code == 401
    body = response.json()["error"]
    assert body["code"] == "REAUTH_REQUIRED"
    assert body["details"]["methods"] == ["totp"]

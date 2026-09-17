"""`build_grants_router` (tasks.md T015b, threat-model.md C-55) contra
Postgres real: `test_panel_revoke_kills_family` (revoca acceso+refresco de
una tacada) y `test_revoked_token_rejected_on_next_call` (el
`CompositeTokenVerifier` real, no un doble, rechaza el access token
revocado en la siguiente llamada -- sin cache de por medio).

002b (tasks.md T060/T061, contracts/federated-login.md §2, decision 4 del
dueno): `revoke` ahora exige identificacion FRESCA (`require_fresh_
identification`, igual que `consent_router.py::approve`) Y ADEMAS
`require_action_confirmation` -- 428 con `details.confirmation_token`
seguido de un segundo POST con `X-Action-Confirmation`. El 401 de
frescura llega SIEMPRE antes que el 428: sin frescura, la confirmacion ni
se pide. Esto es INCONDICIONAL (no depende del interruptor federado), asi
que los tres tests de 002 que revocaban con un solo POST
(`test_revoke_of_a_foreign_grant_is_404`, `test_panel_revoke_kills_family`,
`test_revoked_token_rejected_on_next_call`) ahora completan el baile de
confirmacion -- comportamiento nuevo de 002b, no una perdida de cobertura.

Router mount aislado, mismo patron que `test_consent_router.py`: el CSRF
de doble envio de la app completa NO esta montado aqui, asi que el cliente
de pruebas fija el mismo valor en la cookie `ads_csrf` y en `X-CSRF-Token`
-- `require_action_confirmation` lo exige por si mismo (ver
`test_oauth_routes_registered.py` para el CSRF de la app completa)."""

from __future__ import annotations

import hashlib
import json
import uuid
from collections.abc import AsyncIterator, Awaitable, Callable, Mapping
from datetime import UTC, datetime, timedelta

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
from safent_ads.mcp_oauth.application.refresh_grant import RefreshGrant
from safent_ads.mcp_oauth.application.token_issuance import mint_access_and_refresh
from safent_ads.mcp_oauth.domain.authorization import AuthorizationRequest
from safent_ads.mcp_oauth.domain.client import OAuthClient, RedirectUri, TokenEndpointAuthMethod
from safent_ads.mcp_oauth.domain.errors import GrantRevokedError
from safent_ads.mcp_oauth.domain.grant import Grant
from safent_ads.mcp_oauth.domain.resource import ResourceIndicator
from safent_ads.mcp_oauth.domain.scope import ScopeSet
from safent_ads.mcp_oauth.infrastructure.secrets_token_factory import SecretsOpaqueTokenFactory
from safent_ads.mcp_oauth.infrastructure.sha256_token_hasher import Sha256TokenHasher
from safent_ads.mcp_oauth.infrastructure.sql_authorization_repository import (
    SqlAuthorizationRequestRepository,
)
from safent_ads.mcp_oauth.infrastructure.sql_client_repository import SqlClientRepository
from safent_ads.mcp_oauth.infrastructure.sql_grant_repository import SqlGrantRepository
from safent_ads.mcp_oauth.infrastructure.sql_oauth_session import SqlOAuthSession
from safent_ads.mcp_oauth.presentation.grants_router import build_grants_router
from safent_ads.mcp_oauth.presentation.token_verifier import CompositeTokenVerifier
from safent_ads.shared.clock import SystemClock
from tests.unit.composition.factories import build_api_settings

pytestmark = pytest.mark.integration

_VALID_32_BYTE_KEY_B64 = "MDAwMDAwMDAwMDAwMDAwMDAwMDAwMDAwMDAwMDAwMDA="
_TOTP_SECRET = pyotp.random_base32()
_PUBLIC_BASE_URL = "https://ads.test.ts.net"
_RESOURCE = ResourceIndicator(f"{_PUBLIC_BASE_URL}/mcp")
_REDIRECT_URI = "http://127.0.0.1:54321/callback"
_CODE_CHALLENGE = "E9Melhoa2OwvFrEMTJguCHaoeK1t8URWbuGJSstw-cM"
# Ver docstring de `_cleanup_seeded_clients`: prefijo propio, cascada hasta
# `oauth_grants`/`oauth_tokens`.
_CLIENT_ID_PREFIX = "client-grants-router-test-"
# `require_action_confirmation` valida cookie==cabecera (doble envio); no
# hay middleware CSRF en este montaje aislado, asi que basta un valor fijo.
_CSRF_TOKEN = "grants-router-test-csrf-token"  # noqa: S105 - valor de doble envio, no un secreto


class _SeededOwner:
    def __init__(self, *, owner_id: uuid.UUID, raw_token: str) -> None:
        self.owner_id = owner_id
        self.raw_token = raw_token


async def _seed_owner(session: AsyncSession) -> _SeededOwner:
    owner_id = uuid.uuid4()
    raw_token = f"grants-router-test-token-{uuid.uuid4().hex}"
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
            await connection.execute(
                text("DELETE FROM totp_reauth_confirmations WHERE owner_id = :id"),
                {"id": str(seeded.owner_id)},
            )
            await connection.execute(
                text("DELETE FROM owners WHERE id = :id"), {"id": str(seeded.owner_id)}
            )
        await engine.dispose()


async def _seed_federated_owner(
    session: AsyncSession, *, last_federated_auth_at: datetime
) -> _SeededOwner:
    """Dueno con `sessions.origin='federated'` y una identidad atada
    (002b tasks.md T061), sin TOTP -- FR-109: nacido de Google, ninguna
    otra via. Mismo patron que `test_consent_router.py::_seed_federated_
    owner`, duplicado aqui a proposito (ficheros de test autocontenidos)."""
    owner_id = uuid.uuid4()
    raw_token = f"grants-router-federated-token-{uuid.uuid4().hex}"
    now = datetime.now(UTC)
    await session.execute(
        text("INSERT INTO owners (id, email, password_hash) VALUES (:id, :email, :password_hash)"),
        {
            "id": str(owner_id),
            "email": f"federated-owner-{owner_id.hex[:8]}@example.com",
            "password_hash": "argon2id$fixture$not-a-real-hash",  # noqa: S106
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

    async def factory(*, last_federated_auth_at: datetime) -> _SeededOwner:
        async with AsyncSession(engine, expire_on_commit=False) as session:
            seeded = await _seed_federated_owner(
                session, last_federated_auth_at=last_federated_auth_at
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
    """Ver `test_consent_router.py::_cleanup_seeded_clients` -- mismo
    motivo, prefijo propio de este fichero."""
    yield
    engine = create_async_engine(database_url, pool_pre_ping=True)
    async with engine.begin() as connection:
        await connection.execute(
            text("DELETE FROM oauth_clients WHERE client_id LIKE :prefix"),
            {"prefix": f"{_CLIENT_ID_PREFIX}%"},
        )
    await engine.dispose()


def _build_grants_app(database_url: str, *, federated_available: bool) -> tuple[FastAPI, Container]:
    settings = build_api_settings(
        database_url=database_url, public_base_url=_PUBLIC_BASE_URL, mcp_oauth_enabled=True
    )
    container = Container.build(settings)
    fastapi_app = FastAPI()
    fastapi_app.state.container = container
    fastapi_app.add_exception_handler(ApiError, _handle_api_error)
    fastapi_app.include_router(
        build_grants_router(
            totp_enc_key=_VALID_32_BYTE_KEY_B64, federated_available=federated_available
        )
    )
    return fastapi_app, container


@pytest.fixture
async def app(database_url: str) -> AsyncIterator[FastAPI]:
    fastapi_app, container = _build_grants_app(database_url, federated_available=False)
    try:
        yield fastapi_app
    finally:
        await container.aclose()


@pytest.fixture
async def federated_app(database_url: str) -> AsyncIterator[FastAPI]:
    """002b: login federado activo -- `require_fresh_identification`
    tambien comprueba frescura federada (`test_consent_router.py::
    federated_app`, mismo patron)."""
    fastapi_app, container = _build_grants_app(database_url, federated_available=True)
    try:
        yield fastapi_app
    finally:
        await container.aclose()


async def _seed_grant_rows(
    session: AsyncSession, *, owner_id: uuid.UUID, client_id: str
) -> tuple[uuid.UUID, str, str]:
    """Solicitud de autorizacion + concesion con un par access/refresh
    ACTIVE ya emitido -- el estado que deja `RedeemCode` de verdad tras un
    canje, sin pasar por todo el flujo de autorizacion. Recibe el
    `client_id` en vez del cliente: la fila del cliente puede haberla
    escrito el repositorio o SQL crudo (ver `_seed_legacy_remote_client_
    and_grant`)."""
    hasher = Sha256TokenHasher()
    factory = SecretsOpaqueTokenFactory()
    now = datetime.now(UTC)
    access, refresh = mint_access_and_refresh(now=now, factory=factory, hasher=hasher)
    # `oauth_grants.authorization_txn_id` exige una fila real en
    # `oauth_authorization_requests` (FK) -- PENDING basta, el estado no
    # importa para este test, solo que exista.
    authorization_request = AuthorizationRequest(
        txn_id=uuid.uuid4(),
        client_id=client_id,
        redirect_uri=_REDIRECT_URI,
        code_challenge=_CODE_CHALLENGE,
        client_state=None,
        scope_set=ScopeSet.parse("ads:read ads:propose"),
        resource=_RESOURCE,
        created_at=now,
        expires_at=now + timedelta(minutes=10),
    )
    grant = Grant(
        grant_id=uuid.uuid4(),
        authorization_request_id=authorization_request.id,
        owner_id=owner_id,
        client_id=client_id,
        scope_set=ScopeSet.parse("ads:read ads:propose"),
        resource=_RESOURCE,
        created_at=now,
        tokens=(access.issued, refresh.issued),
    )
    await SqlAuthorizationRequestRepository(session, clock=SystemClock()).create(
        authorization_request
    )
    await SqlGrantRepository(session).create(grant)
    return grant.id, access.raw_value, refresh.raw_value


async def _seed_client_and_grant(
    database_url: str, *, owner_id: uuid.UUID
) -> tuple[OAuthClient, uuid.UUID, str, str]:
    engine = create_async_engine(database_url, pool_pre_ping=True)
    client = OAuthClient(
        client_id=f"{_CLIENT_ID_PREFIX}{uuid.uuid4().hex[:8]}",
        client_name="Claude Code",
        redirect_uris=(RedirectUri(_REDIRECT_URI),),
        token_endpoint_auth_method=TokenEndpointAuthMethod.NONE,
        client_secret_hash=None,
        grant_types=("authorization_code", "refresh_token"),
        requested_scope=ScopeSet.parse("ads:read ads:propose"),
        created_at=datetime.now(UTC),
    )
    async with AsyncSession(engine, expire_on_commit=False) as session:
        await SqlClientRepository(session).save(client)
        grant_id, access_token, refresh_token = await _seed_grant_rows(
            session, owner_id=owner_id, client_id=client.id
        )
        await session.commit()
    await engine.dispose()
    return client, grant_id, access_token, refresh_token


_INSERT_LEGACY_CLIENT_SQL = text("""
    INSERT INTO oauth_clients
        (client_id, client_name, redirect_uris, token_endpoint_auth_method,
         client_secret_hash, grant_types, requested_scopes, created_at, last_seen_at)
    VALUES
        (:client_id, :client_name, CAST(:redirect_uris AS jsonb), 'none',
         NULL, CAST(:grant_types AS jsonb), 'ads:read ads:propose', :now, :now)
""")


async def _seed_legacy_client_and_grant(
    database_url: str, *, owner_id: uuid.UUID, redirect_uri: str, client_name: str
) -> tuple[str, uuid.UUID]:
    """Una registracion ANTERIOR a las reglas de hoy -- destino remoto
    (D-11) o nombre con anulacion bidireccional (C-70 pieza 3) --: el
    dominio ya no sabe construirla, asi que solo se puede sembrar con SQL
    crudo, exactamente como la dejo una version anterior del motor."""
    engine = create_async_engine(database_url, pool_pre_ping=True)
    client_id = f"{_CLIENT_ID_PREFIX}legado-{uuid.uuid4().hex[:8]}"
    now = datetime.now(UTC)
    async with AsyncSession(engine, expire_on_commit=False) as session:
        await session.execute(
            _INSERT_LEGACY_CLIENT_SQL,
            {
                "client_id": client_id,
                "client_name": client_name,
                "redirect_uris": json.dumps([redirect_uri]),
                "grant_types": json.dumps(["authorization_code", "refresh_token"]),
                "now": now,
            },
        )
        grant_id, _access, _refresh = await _seed_grant_rows(
            session, owner_id=owner_id, client_id=client_id
        )
        await session.commit()
    await engine.dispose()
    return client_id, grant_id


def _client_for(app: FastAPI, *, raw_token: str | None) -> httpx.AsyncClient:
    cookies = {SESSION_COOKIE_NAME: raw_token} if raw_token else {}
    cookies["ads_csrf"] = _CSRF_TOKEN
    return httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app),
        base_url="https://test",
        cookies=cookies,
        headers={"x-csrf-token": _CSRF_TOKEN},
    )


def _reauth_headers(*, time_step_offset: int = 0) -> dict[str, str]:
    """`time_step_offset` != 0 pide el codigo de OTRO paso de 30 s -- solo
    hace falta cuando dos pruebas TOTP DISTINTAS comparten test (dos
    acciones distintas, cada una con su propio `action_hash`); dentro del
    MISMO baile de confirmacion (`_confirm`) un solo codigo basta (C-81:
    la evidencia queda por `action_hash`, y la segunda peticion es la
    MISMA accion)."""
    moment = datetime.now(UTC) + timedelta(seconds=30 * time_step_offset)
    return {"X-Reauth-Token": pyotp.TOTP(_TOTP_SECRET).at(moment)}


async def _confirm(
    http_client: httpx.AsyncClient, url: str, *, first_headers: Mapping[str, str] | None = None
) -> httpx.Response:
    """El baile de "Revisa y confirma esta accion" (002b tasks.md T060/
    T061, `iam/presentation/action_confirmation.py`): un primer POST sin
    `X-Action-Confirmation` siempre da 428 con `details.confirmation_
    token`; el segundo, con ese token Y NADA MAS (C-81, T064: la misma
    accion ya dejo su propia evidencia en `totp_reauth_confirmations` o
    la frescura federada sigue viva -- ninguna necesita una segunda
    prueba), produce el efecto real. `first_headers` es la prueba de
    frescura del PRIMER intento (`_reauth_headers()` para TOTP, nada para
    frescura federada, que ya viene fresca en la sesion)."""
    first = await http_client.post(url, headers=first_headers or {})
    assert first.status_code == 428, first.text
    token = first.json()["error"]["details"]["confirmation_token"]
    return await http_client.post(url, headers={"X-Action-Confirmation": token})


async def test_list_grants_without_session_is_401(app: FastAPI) -> None:
    async with _client_for(app, raw_token=None) as http_client:
        response = await http_client.get("/api/v1/mcp-oauth/grants")

    assert response.status_code == 401


async def test_list_grants_returns_the_contract_shape(
    app: FastAPI, owner: _SeededOwner, database_url: str
) -> None:
    client, grant_id, _access, _refresh = await _seed_client_and_grant(
        database_url, owner_id=owner.owner_id
    )
    async with _client_for(app, raw_token=owner.raw_token) as http_client:
        response = await http_client.get("/api/v1/mcp-oauth/grants")

    assert response.status_code == 200
    body = response.json()
    assert body["grants"] == [
        {
            "grant_id": str(grant_id),
            "client_id": client.id,
            "client_name": "Claude Code",
            "redirect_host": "127.0.0.1:54321",
            "scopes": ["ads:propose", "ads:read"],
            "created_at": body["grants"][0]["created_at"],
            "expires_at": body["grants"][0]["expires_at"],
            "last_used_at": body["grants"][0]["last_used_at"],
        }
    ]
    assert body["grants"][0]["last_used_at"] is not None


@pytest.mark.parametrize(
    ("redirect_uri", "client_name"),
    (
        pytest.param(
            "https://agent.example/callback", "Agente heredado", id="destino-remoto-D-11"
        ),
        # `chr(0x202E)` da la vuelta al texto que se pinta: prohibido desde
        # C-70 pieza 3, pero una fila registrada ANTES sigue en la BD.
        pytest.param(
            "http://127.0.0.1:54321/callback",
            f"Agente {chr(0x202E)}odagerdeh",
            id="nombre-bidireccional-C-70-3",
        ),
    ),
)
async def test_a_legacy_client_neither_hides_the_list_nor_blocks_its_revocation(
    redirect_uri: str,
    client_name: str,
    app: FastAPI,
    owner: _SeededOwner,
    database_url: str,
) -> None:
    """Revision de codigo (17-sep): una fila que ya no cumple una regla
    endurecida DESPUES de registrarla no se puede hidratar. Si eso
    tumbara `get_many`, el dueno perderia la lista ENTERA -- incluido el
    boton «Quitar acceso» de la peligrosa. La lista responde 200, pinta la
    concesion legado por su `client_id` (sin `redirect_host`, porque el
    cliente no se pudo cargar) y se puede revocar. Dos motivos distintos,
    el mismo desenlace: por eso el repositorio atrapa `DomainError` y no
    una excepcion concreta."""
    good_client, good_grant_id, _access, _refresh = await _seed_client_and_grant(
        database_url, owner_id=owner.owner_id
    )
    legacy_client_id, legacy_grant_id = await _seed_legacy_client_and_grant(
        database_url, owner_id=owner.owner_id, redirect_uri=redirect_uri, client_name=client_name
    )

    async with _client_for(app, raw_token=owner.raw_token) as http_client:
        listing = await http_client.get("/api/v1/mcp-oauth/grants")

        assert listing.status_code == 200, listing.text
        by_grant_id = {entry["grant_id"]: entry for entry in listing.json()["grants"]}
        assert set(by_grant_id) == {str(good_grant_id), str(legacy_grant_id)}
        assert by_grant_id[str(good_grant_id)]["client_name"] == good_client.client_name
        assert by_grant_id[str(good_grant_id)]["redirect_host"] == "127.0.0.1:54321"
        legacy = by_grant_id[str(legacy_grant_id)]
        assert legacy["client_id"] == legacy_client_id
        assert legacy["client_name"] == legacy_client_id
        assert legacy["redirect_host"] == ""

        revoked = await _confirm(
            http_client,
            f"/api/v1/mcp-oauth/grants/{legacy_grant_id}/revoke",
            first_headers=_reauth_headers(),
        )
        assert revoked.status_code == 204, revoked.text

        after = await http_client.get("/api/v1/mcp-oauth/grants")

    assert [entry["grant_id"] for entry in after.json()["grants"]] == [str(good_grant_id)]


async def test_revoke_without_reauth_token_is_reauth_required(
    app: FastAPI, owner: _SeededOwner, database_url: str
) -> None:
    """T061: sin frescura, el 401 llega SIN llegar nunca al 428 -- la
    ausencia de `confirmation_token` en el cuerpo lo distingue."""
    _client, grant_id, _access, _refresh = await _seed_client_and_grant(
        database_url, owner_id=owner.owner_id
    )
    async with _client_for(app, raw_token=owner.raw_token) as http_client:
        response = await http_client.post(f"/api/v1/mcp-oauth/grants/{grant_id}/revoke")

    assert response.status_code == 401
    assert response.json()["error"]["code"] == "REAUTH_REQUIRED"
    assert "confirmation_token" not in response.text


async def test_revoke_with_freshness_and_no_confirmation_header_is_428(
    app: FastAPI, owner: _SeededOwner, database_url: str
) -> None:
    """T061: con frescura (aqui, TOTP) y sin `X-Action-Confirmation`, 428
    con `details.confirmation_token` y `details.expires_at`."""
    _client, grant_id, _access, _refresh = await _seed_client_and_grant(
        database_url, owner_id=owner.owner_id
    )
    async with _client_for(app, raw_token=owner.raw_token) as http_client:
        response = await http_client.post(
            f"/api/v1/mcp-oauth/grants/{grant_id}/revoke", headers=_reauth_headers()
        )

    assert response.status_code == 428
    details = response.json()["error"]["details"]
    assert details["confirmation_token"]
    assert details["expires_at"]


async def test_revoke_of_a_foreign_grant_is_404(app: FastAPI, owner: _SeededOwner) -> None:
    async with _client_for(app, raw_token=owner.raw_token) as http_client:
        response = await _confirm(
            http_client,
            f"/api/v1/mcp-oauth/grants/{uuid.uuid4()}/revoke",
            first_headers=_reauth_headers(),
        )

    assert response.status_code == 404


async def test_panel_revoke_kills_family(
    app: FastAPI, owner: _SeededOwner, database_url: str
) -> None:
    client, grant_id, _access, _refresh = await _seed_client_and_grant(
        database_url, owner_id=owner.owner_id
    )
    async with _client_for(app, raw_token=owner.raw_token) as http_client:
        response = await _confirm(
            http_client,
            f"/api/v1/mcp-oauth/grants/{grant_id}/revoke",
            first_headers=_reauth_headers(),
        )
        assert response.status_code == 204, response.text

        listing = await http_client.get("/api/v1/mcp-oauth/grants")

    # `ListGrants.execute` solo enumera concesiones activas
    # (`GrantRepository.list_active_for_owner`): una revocada desaparece.
    assert listing.json()["grants"] == []


async def test_revoked_token_rejected_on_next_call(
    app: FastAPI, owner: _SeededOwner, database_url: str
) -> None:
    """El `CompositeTokenVerifier` real (el mismo que protege `/mcp`,
    threat-model.md C-48/C-55) rechaza el access token de una concesion
    revocada desde el panel -- consulta la BD en cada llamada, sin cache.
    T061: su refresco tambien -- `RefreshGrant` se topa con `Grant.
    _require_active()` (`GrantRevokedError`), no con un token desconocido."""
    _client, grant_id, access_token, refresh_token = await _seed_client_and_grant(
        database_url, owner_id=owner.owner_id
    )
    settings = build_api_settings(database_url=database_url, public_base_url=_PUBLIC_BASE_URL)
    container = Container.build(settings)
    verifier = CompositeTokenVerifier(
        session_factory=lambda: SqlOAuthSession(container.session_factory, SystemClock()),
        token_hasher=Sha256TokenHasher(),
        clock=SystemClock(),
        static_token=None,
        resource=_RESOURCE.value,
    )
    try:
        before = await verifier.verify_token(access_token)
        assert before is not None

        async with _client_for(app, raw_token=owner.raw_token) as http_client:
            revoke_response = await _confirm(
                http_client,
                f"/api/v1/mcp-oauth/grants/{grant_id}/revoke",
                first_headers=_reauth_headers(),
            )
            assert revoke_response.status_code == 204, revoke_response.text

        after = await verifier.verify_token(access_token)
        assert after is None

        engine = create_async_engine(database_url, pool_pre_ping=True)
        async with AsyncSession(engine, expire_on_commit=False) as session:
            refresh_use_case = RefreshGrant(
                grants=SqlGrantRepository(session),
                token_hasher=Sha256TokenHasher(),
                token_factory=SecretsOpaqueTokenFactory(),
                clock=SystemClock(),
            )
            with pytest.raises(GrantRevokedError):
                await refresh_use_case.execute(
                    refresh_token=refresh_token,
                    client_id=_client.id,
                    resource=_RESOURCE.value,
                    requested_scope=None,
                )
        await engine.dispose()
    finally:
        await container.aclose()


async def test_stale_federated_freshness_on_revoke_is_401_with_methods_federated(
    federated_app: FastAPI,
    federated_owner_factory: Callable[..., Awaitable[_SeededOwner]],
    database_url: str,
) -> None:
    stale_at = datetime.now(UTC) - timedelta(minutes=6)
    federated_owner = await federated_owner_factory(last_federated_auth_at=stale_at)
    _client, grant_id, _access, _refresh = await _seed_client_and_grant(
        database_url, owner_id=federated_owner.owner_id
    )
    async with _client_for(federated_app, raw_token=federated_owner.raw_token) as http_client:
        response = await http_client.post(f"/api/v1/mcp-oauth/grants/{grant_id}/revoke")

    assert response.status_code == 401
    assert response.json()["error"]["details"]["methods"] == ["federated"]


async def test_fresh_federated_owner_revokes_with_the_confirmation_dance_and_no_header(
    federated_app: FastAPI,
    federated_owner_factory: Callable[..., Awaitable[_SeededOwner]],
    database_url: str,
) -> None:
    """002b: frescura federada satisface `require_fresh_identification`
    igual que TOTP -- sin `X-Reauth-Token` en ninguno de los dos POST, ya
    que la frescura federada no se quema por peticion (a diferencia de un
    codigo TOTP)."""
    federated_owner = await federated_owner_factory(last_federated_auth_at=datetime.now(UTC))
    _client, grant_id, _access, _refresh = await _seed_client_and_grant(
        database_url, owner_id=federated_owner.owner_id
    )
    async with _client_for(federated_app, raw_token=federated_owner.raw_token) as http_client:
        response = await _confirm(http_client, f"/api/v1/mcp-oauth/grants/{grant_id}/revoke")

    assert response.status_code == 204, response.text


async def test_totp_owner_revokes_with_the_confirmation_dance_and_a_single_code(
    app: FastAPI, owner: _SeededOwner, database_url: str
) -> None:
    """C-81 (T064 security review): la primera peticion TOTP deja su
    propia evidencia por `action_hash` en `totp_reauth_confirmations`; la
    segunda -- la MISMA accion, solo con `X-Action-Confirmation` -- la lee
    de vuelta sin pedir un segundo codigo. Simetrico al equivalente
    federado de arriba."""
    _client, grant_id, _access, _refresh = await _seed_client_and_grant(
        database_url, owner_id=owner.owner_id
    )
    async with _client_for(app, raw_token=owner.raw_token) as http_client:
        response = await _confirm(
            http_client,
            f"/api/v1/mcp-oauth/grants/{grant_id}/revoke",
            first_headers=_reauth_headers(),
        )

    assert response.status_code == 204, response.text


async def test_totp_confirmation_for_one_grant_does_not_satisfy_revoking_another(
    app: FastAPI, owner: _SeededOwner, database_url: str
) -> None:
    """C-81: la evidencia vive por `(owner_id, action_hash)` -- el
    `action_hash` de revocar A incluye el `grant_id` de A
    (`_revoke_action_hash`), asi que nunca satisface revocar B."""
    _client_a, grant_a, _access_a, _refresh_a = await _seed_client_and_grant(
        database_url, owner_id=owner.owner_id
    )
    _client_b, grant_b, _access_b, _refresh_b = await _seed_client_and_grant(
        database_url, owner_id=owner.owner_id
    )
    async with _client_for(app, raw_token=owner.raw_token) as http_client:
        confirmation_for_a = await http_client.post(
            f"/api/v1/mcp-oauth/grants/{grant_a}/revoke", headers=_reauth_headers()
        )
        assert confirmation_for_a.status_code == 428, confirmation_for_a.text

        response = await http_client.post(f"/api/v1/mcp-oauth/grants/{grant_b}/revoke")

    assert response.status_code == 401
    assert response.json()["error"]["code"] == "REAUTH_REQUIRED"


async def test_revoking_one_grant_does_not_touch_the_other(
    federated_app: FastAPI,
    federated_owner_factory: Callable[..., Awaitable[_SeededOwner]],
    database_url: str,
) -> None:
    """T053 bullet «revocar uno no toca al otro» -- realizado aqui (T061)
    porque depende del baile de confirmacion de T060; usa frescura
    federada por ser el camino mas simple para dos revocaciones seguidas
    sin gestionar codigos TOTP distintos."""
    federated_owner = await federated_owner_factory(last_federated_auth_at=datetime.now(UTC))
    _client_a, grant_a, _access_a, _refresh_a = await _seed_client_and_grant(
        database_url, owner_id=federated_owner.owner_id
    )
    _client_b, grant_b, _access_b, _refresh_b = await _seed_client_and_grant(
        database_url, owner_id=federated_owner.owner_id
    )
    async with _client_for(federated_app, raw_token=federated_owner.raw_token) as http_client:
        revoke_response = await _confirm(http_client, f"/api/v1/mcp-oauth/grants/{grant_a}/revoke")
        assert revoke_response.status_code == 204, revoke_response.text

        listing = await http_client.get("/api/v1/mcp-oauth/grants")

    remaining_ids = {grant["grant_id"] for grant in listing.json()["grants"]}
    assert remaining_ids == {str(grant_b)}


async def test_federated_mark_does_not_survive_a_successful_revoke(
    federated_app: FastAPI,
    federated_owner_factory: Callable[..., Awaitable[_SeededOwner]],
    database_url: str,
) -> None:
    """C-82 (T064 security review): la presencia que autorizo la primera
    revocacion no basta para la segunda -- "la marca muere con su
    metodo". Sin esto, un dueno federado podria revocar CUALQUIER numero
    de agentes con una sola identificacion de hace 5 minutos."""
    federated_owner = await federated_owner_factory(last_federated_auth_at=datetime.now(UTC))
    _client_a, grant_a, _access_a, _refresh_a = await _seed_client_and_grant(
        database_url, owner_id=federated_owner.owner_id
    )
    _client_b, grant_b, _access_b, _refresh_b = await _seed_client_and_grant(
        database_url, owner_id=federated_owner.owner_id
    )
    async with _client_for(federated_app, raw_token=federated_owner.raw_token) as http_client:
        first_revoke = await _confirm(http_client, f"/api/v1/mcp-oauth/grants/{grant_a}/revoke")
        assert first_revoke.status_code == 204, first_revoke.text

        second_attempt = await http_client.post(f"/api/v1/mcp-oauth/grants/{grant_b}/revoke")

    assert second_attempt.status_code == 401
    assert second_attempt.json()["error"]["details"]["methods"] == ["federated"]


async def test_password_session_mark_is_nulled_after_a_successful_revoke(
    federated_app: FastAPI,
    federated_owner_factory: Callable[..., Awaitable[_SeededOwner]],
    database_url: str,
) -> None:
    """C-82: una sesion `origin='password'` refrescada con Google
    (decision 7) SI puede quedar en `NULL` liso -- a diferencia de una
    sesion `origin='federated'`, para la que `sessions_federated_origin_
    check` (0052) prohibe el `NULL` (cubierto por el test anterior, que en
    su lugar comprueba el EFECTO: la marca ya no sirve). Parte de un dueno
    federado ya sembrado (identidad atada, marca fresca) y le cambia el
    `origin` a `password` para simular exactamente esa decision 7."""
    federated_owner = await federated_owner_factory(last_federated_auth_at=datetime.now(UTC))
    engine = create_async_engine(database_url, pool_pre_ping=True)
    async with engine.begin() as connection:
        await connection.execute(
            text("UPDATE sessions SET origin = 'password' WHERE owner_id = :id"),
            {"id": str(federated_owner.owner_id)},
        )
    await engine.dispose()
    _client, grant_id, _access, _refresh = await _seed_client_and_grant(
        database_url, owner_id=federated_owner.owner_id
    )
    async with _client_for(federated_app, raw_token=federated_owner.raw_token) as http_client:
        revoke_response = await _confirm(http_client, f"/api/v1/mcp-oauth/grants/{grant_id}/revoke")

    assert revoke_response.status_code == 204, revoke_response.text
    engine = create_async_engine(database_url, pool_pre_ping=True)
    async with engine.begin() as connection:
        mark_after = (
            await connection.execute(
                text("SELECT last_federated_auth_at FROM sessions WHERE owner_id = :id"),
                {"id": str(federated_owner.owner_id)},
            )
        ).scalar_one()
    await engine.dispose()
    assert mark_after is None

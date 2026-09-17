"""FR-119/NFR-103 (spec 002b tasks.md T074): con el proveedor de identidad
CAIDO durante toda la prueba (`FakeFederatedIdentityProvider.failure`
fijado desde el principio, no a mitad de flujo), tres cosas siguen siendo
ciertas en el MISMO proceso:

1. El panel sigue accesible por la vía clásica (`POST /auth/login`,
   contraseña) -- `build_auth_router` no depende en absoluto de
   `FederatedIdentityProvider`.
2. Un agente YA conectado sigue llamando a `/mcp`: `CompositeTokenVerifier`
   introspecciona la concesión OAuth contra Postgres, nunca contra Google
   (`token_verifier.py`, sin cambios en 002b).
3. El navegador recibe un error accionable y de un solo salto -- nunca una
   espera indefinida: `GoogleOidcProvider` acota el tramo a 10 s
   (`_TIMEOUT_SECONDS`, `test_google_oidc_provider.py::
   test_exchange_code_applies_a_ten_second_timeout`), y aquí el doble
   falla de inmediato, sin red."""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta

import httpx
import pytest
from fastapi import FastAPI
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine
from tests.unit.composition.factories import build_api_settings

from safent_ads.composition.api import _handle_api_error
from safent_ads.composition.container import Container
from safent_ads.composition.federated_routes import _SqlOpenConsentTransactions
from safent_ads.iam.application.ports import AuthorizedEmailList
from safent_ads.iam.infrastructure.argon2_password_hasher import Argon2PasswordHasher
from safent_ads.iam.infrastructure.fakes import FakeFederatedIdentityProvider
from safent_ads.iam.presentation.errors import ApiError
from safent_ads.iam.presentation.federated_router import build_federated_router
from safent_ads.iam.presentation.router import build_auth_router
from safent_ads.mcp_oauth.application.token_issuance import mint_access_and_refresh
from safent_ads.mcp_oauth.domain.authorization import AuthorizationRequest
from safent_ads.mcp_oauth.domain.client import OAuthClient, RedirectUri, TokenEndpointAuthMethod
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
from safent_ads.mcp_oauth.presentation.token_verifier import CompositeTokenVerifier
from safent_ads.shared.clock import SystemClock

pytestmark = pytest.mark.integration

_PUBLIC_BASE_URL = "https://ads.test.ts.net"
_RESOURCE = ResourceIndicator(f"{_PUBLIC_BASE_URL}/mcp")
_REDIRECT_URI = "http://127.0.0.1:54321/callback"
_CORRECT_PASSWORD = "correct horse battery staple"  # noqa: S105 - fixture, no secreto real
_ALLOWED_EMAIL = "dueno@example.com"
_CLIENT_ID_PREFIX = "client-federated-degradation-test-"


async def _seed_owner_with_password(database_url: str) -> uuid.UUID:
    engine = create_async_engine(database_url, pool_pre_ping=True)
    owner_id = uuid.uuid4()
    password_hash = Argon2PasswordHasher().hash(_CORRECT_PASSWORD)
    async with engine.begin() as connection:
        await connection.execute(
            text(
                "INSERT INTO owners (id, email, password_hash) VALUES (:id, :email, :password_hash)"
            ),
            {"id": str(owner_id), "email": _ALLOWED_EMAIL, "password_hash": password_hash},
        )
    await engine.dispose()
    return owner_id


async def _seed_connected_agent(database_url: str, *, owner_id: uuid.UUID) -> tuple[str, str]:
    """Un agente YA conectado: cliente + concesión con un access token
    ACTIVE, el estado que deja `RedeemCode` de verdad tras un canje."""
    engine = create_async_engine(database_url, pool_pre_ping=True)
    client = OAuthClient(
        client_id=f"{_CLIENT_ID_PREFIX}{uuid.uuid4().hex[:8]}",
        client_name="Claude Code",
        redirect_uris=(RedirectUri(_REDIRECT_URI),),
        token_endpoint_auth_method=TokenEndpointAuthMethod.NONE,
        client_secret_hash=None,
        grant_types=("authorization_code", "refresh_token"),
        requested_scope=ScopeSet.parse("ads:read"),
        created_at=datetime.now(UTC),
    )
    hasher = Sha256TokenHasher()
    factory = SecretsOpaqueTokenFactory()
    now = datetime.now(UTC)
    access, _refresh = mint_access_and_refresh(now=now, factory=factory, hasher=hasher)
    authorization_request = AuthorizationRequest(
        txn_id=uuid.uuid4(),
        client_id=client.id,
        redirect_uri=_REDIRECT_URI,
        code_challenge="E9Melhoa2OwvFrEMTJguCHaoeK1t8URWbuGJSstw-cM",
        client_state=None,
        scope_set=ScopeSet.parse("ads:read"),
        resource=_RESOURCE,
        created_at=now,
        expires_at=now + timedelta(minutes=10),
    )
    async with AsyncSession(engine, expire_on_commit=False) as session:
        await SqlClientRepository(session).save(client)
        await SqlAuthorizationRequestRepository(session, clock=SystemClock()).create(
            authorization_request
        )
        grant = Grant(
            grant_id=uuid.uuid4(),
            authorization_request_id=authorization_request.id,
            owner_id=owner_id,
            client_id=client.id,
            scope_set=ScopeSet.parse("ads:read"),
            resource=_RESOURCE,
            created_at=now,
            tokens=(access.issued,),
        )
        await SqlGrantRepository(session).create(grant)
        await session.commit()
    await engine.dispose()
    return client.id, access.raw_value


async def _cleanup(database_url: str, *, owner_id: uuid.UUID, client_id: str) -> None:
    engine = create_async_engine(database_url, pool_pre_ping=True)
    async with engine.begin() as connection:
        await connection.execute(
            text("DELETE FROM oauth_clients WHERE client_id = :id"), {"id": client_id}
        )
        await connection.execute(
            text("DELETE FROM sessions WHERE owner_id = :id"), {"id": str(owner_id)}
        )
        await connection.execute(text("DELETE FROM owners WHERE id = :id"), {"id": str(owner_id)})
    await engine.dispose()


def _build_app(
    isolated_iam_database_url: str, provider: FakeFederatedIdentityProvider
) -> tuple[FastAPI, Container]:
    settings = build_api_settings(
        database_url=isolated_iam_database_url,
        public_base_url=_PUBLIC_BASE_URL,
        federated_login_enabled=True,
        google_oidc_client_id="degradation-test-client-id",
        google_oidc_client_secret="degradation-test-client-secret",
        federated_allowed_emails=[_ALLOWED_EMAIL],
    )
    container = Container.build(settings)
    fastapi_app = FastAPI()
    fastapi_app.state.container = container
    fastapi_app.add_exception_handler(ApiError, _handle_api_error)
    fastapi_app.include_router(build_auth_router(settings))
    fastapi_app.include_router(
        build_federated_router(
            provider=provider,
            redirect_uri=f"{_PUBLIC_BASE_URL}/api/v1/auth/federated/callback",
            allowed_emails=AuthorizedEmailList.from_raw((_ALLOWED_EMAIL,)),
            open_consent_transactions=_SqlOpenConsentTransactions(container),
            trusted_proxy_hops=settings.trusted_proxy_hops,
        )
    )
    return fastapi_app, container


async def test_password_login_and_connected_agents_survive_a_dead_provider(
    isolated_iam_database_url: str,
) -> None:
    owner_id = await _seed_owner_with_password(isolated_iam_database_url)
    client_id, access_token = await _seed_connected_agent(
        isolated_iam_database_url, owner_id=owner_id
    )
    provider = FakeFederatedIdentityProvider()
    provider.failure = RuntimeError("Google no responde")  # caido desde el principio
    app, container = _build_app(isolated_iam_database_url, provider)
    try:
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="https://test"
        ) as http_client:
            # 1. La via clasica sigue accesible, sin tocar Google.
            login_response = await http_client.post(
                "/api/v1/auth/login",
                json={"email": _ALLOWED_EMAIL, "password": _CORRECT_PASSWORD},
            )
            assert login_response.status_code == 204, login_response.text

            # 2. Intentar el tramo federado falla de inmediato (sin red) y
            # sin dejar ninguna sesion a medias -- nunca una espera larga.
            start_response = await http_client.post(
                "/api/v1/auth/federated/start", json={"txn_id": None}
            )
            assert start_response.status_code == 200, start_response.text
            state = start_response.json()["authorization_url"].split("state=")[1].split("&")[0]
            callback_response = await http_client.get(
                f"/api/v1/auth/federated/callback?state={state}&code=fake-code",
                follow_redirects=False,
            )
            assert callback_response.status_code == 303
            assert (
                callback_response.headers["location"]
                == "/login?federated_error=provider_unavailable"
            )

        # 3. Un agente ya conectado sigue autenticando /mcp -- introspeccion
        # contra Postgres, nunca contra el proveedor de identidad caido.
        verifier = CompositeTokenVerifier(
            session_factory=lambda: SqlOAuthSession(container.session_factory, SystemClock()),
            token_hasher=Sha256TokenHasher(),
            clock=SystemClock(),
            static_token=None,
            resource=_RESOURCE.value,
        )
        access = await verifier.verify_token(access_token)
        assert access is not None
    finally:
        await container.aclose()
        await _cleanup(isolated_iam_database_url, owner_id=owner_id, client_id=client_id)

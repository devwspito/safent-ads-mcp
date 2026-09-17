"""Cableado de `composition/app.py`/`composition/api.py` para spec 002
(mcp_oauth) tasks.md T014, contra Postgres real: las rutas del AS
(`/.well-known/*`, `/authorize`, `/token`, `/register`, `/revoke`) y las
del panel (`/api/v1/mcp-oauth/*`) llegan de verdad al router del padre --
ninguna cae en el catch-all de la SPA (C-52) -- y el CSRF de doble envio
sigue protegiendo `/api/v1/mcp-oauth/consent/*` (C-40).

`httpx.ASGITransport` en vez de `TestClient`: `Container` abre conexiones
`asyncpg` atadas al bucle en el que se usan por primera vez, y `TestClient`
corre en su propio hilo/bucle (mismo patron que
`tests/integration/brand/test_authorization_integration.py`)."""

from __future__ import annotations

import hashlib
import uuid
from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta
from pathlib import Path

import httpx
import pyotp
import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine

from safent_ads.composition.app import create_app
from safent_ads.composition.settings import ApiSettings
from safent_ads.iam.infrastructure.aesgcm_totp_cipher import (
    PURPOSE_TOTP_SECRET,
    AesGcmTotpCipher,
)
from safent_ads.iam.infrastructure.argon2_password_hasher import Argon2PasswordHasher
from safent_ads.mcp_oauth.domain.authorization import AuthorizationRequest
from safent_ads.mcp_oauth.domain.client import OAuthClient, RedirectUri, TokenEndpointAuthMethod
from safent_ads.mcp_oauth.domain.resource import ResourceIndicator
from safent_ads.mcp_oauth.domain.scope import ScopeSet
from safent_ads.mcp_oauth.infrastructure.sql_authorization_repository import (
    SqlAuthorizationRequestRepository,
)
from safent_ads.mcp_oauth.infrastructure.sql_client_repository import SqlClientRepository
from safent_ads.shared.clock import SystemClock

pytestmark = pytest.mark.integration

_VALID_32_BYTE_KEY_B64 = "MDAwMDAwMDAwMDAwMDAwMDAwMDAwMDAwMDAwMDAwMDA="
_CORRECT_PASSWORD = "correct horse battery staple"  # noqa: S105 - fixture, no secreto real
_RAW_SESSION_TOKEN = "oauth-routes-test-raw-session-token"  # noqa: S105 - idem


def _api_settings(database_url: str, *, panel_dist_dir: Path) -> ApiSettings:
    return ApiSettings(
        database_url=database_url,
        session_secret="test-session-secret-0123456789abcdef",
        totp_enc_key=_VALID_32_BYTE_KEY_B64,
        mcp_token="test-mcp-token-abc123",
        broker_socket_path="/tmp/safent-ads-test/broker.sock",
        public_base_url="https://ads.test.ts.net",
        telegram_bot_token="123456:test-bot-token",
        telegram_owner_chat_ids=[111222333],
        approval_signing_key=_VALID_32_BYTE_KEY_B64,
        mcp_oauth_enabled=True,
        panel_dist_dir=panel_dist_dir,
        # Fusion lane/003 (004 tasks.md A6/A10): `ApiSettings` exige ahora
        # exactamente uno de los dos modos de autoridad de `/mcp`.
        seat_authority_enabled=True,
        enterprise_origin="https://enterprise.test",
        enterprise_service_secret="a" * 64,
        enterprise_org_ids=frozenset({uuid.UUID("00000000-0000-0000-0000-000000000001")}),
    )


class _SeededOwner:
    def __init__(self, *, owner_id: uuid.UUID, email: str, totp_secret: str) -> None:
        self.owner_id = owner_id
        self.email = email
        self.totp_secret = totp_secret


@pytest.fixture
async def seeded_owner(database_url: str) -> AsyncIterator[_SeededOwner]:
    """Escribe con commit real por su propio motor -- `Container` abre su
    propio pool, asi que la fila tiene que estar confirmada antes de que la
    vea (mismo patron que `test_login_lockout_persists.py::seeded_owner`)."""
    engine = create_async_engine(database_url, pool_pre_ping=True)
    owner_id = uuid.uuid4()
    email = f"owner-{owner_id.hex[:10]}@safent.example"
    totp_secret = pyotp.random_base32()
    password_hash = Argon2PasswordHasher().hash(_CORRECT_PASSWORD)
    encrypted_secret = AesGcmTotpCipher(_VALID_32_BYTE_KEY_B64).encrypt(
        totp_secret, purpose=PURPOSE_TOTP_SECRET
    )
    now = datetime.now(UTC)
    session_id = uuid.uuid4()
    async with engine.begin() as connection:
        await connection.execute(
            text(
                "INSERT INTO owners "
                "(id, email, password_hash, totp_secret_encrypted, totp_confirmed_at) "
                "VALUES (:id, :email, :password_hash, :totp_secret, :now)"
            ),
            {
                "id": str(owner_id),
                "email": email,
                "password_hash": password_hash,
                "totp_secret": encrypted_secret,
                "now": now,
            },
        )
        await connection.execute(
            text(
                "INSERT INTO sessions (id, owner_id, token_hash, created_at, expires_at) "
                "VALUES (:id, :owner_id, :token_hash, :created_at, :expires_at)"
            ),
            {
                "id": str(session_id),
                "owner_id": str(owner_id),
                "token_hash": hashlib.sha256(_RAW_SESSION_TOKEN.encode("utf-8")).hexdigest(),
                "created_at": now,
                "expires_at": now + timedelta(hours=1),
            },
        )
    try:
        yield _SeededOwner(owner_id=owner_id, email=email, totp_secret=totp_secret)
    finally:
        async with engine.begin() as connection:
            await connection.execute(
                text("DELETE FROM sessions WHERE owner_id = :id"), {"id": str(owner_id)}
            )
            await connection.execute(
                text("DELETE FROM owners WHERE id = :id"), {"id": str(owner_id)}
            )
        await engine.dispose()


@pytest.fixture
def panel_dist_dir(tmp_path: Path) -> Path:
    """C-52: el catch-all de la SPA tiene que existir de verdad para que
    "las rutas del AS ganan sobre el catch-all" sea una prueba real, no un
    catch-all ausente que nunca compite (mismo patron que
    `tests/unit/test_panel_spa.py`)."""
    dist_dir = tmp_path / "dist"
    dist_dir.mkdir()
    (dist_dir / "index.html").write_text("<html>panel</html>", encoding="utf-8")
    return dist_dir


def _client(app: object, *, cookies: dict[str, str] | None = None) -> httpx.AsyncClient:
    return httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="https://test", cookies=cookies or {}
    )


@pytest.mark.parametrize(
    "path",
    [
        "/.well-known/oauth-authorization-server",
        "/.well-known/oauth-protected-resource/mcp",
        # T049 (spec 008): la ruta PELADA de RFC 9728 SS3.1 -- varios
        # clientes (Codex incluido) la prueban antes que la de `/mcp` -- sin
        # ruta propia caia en este mismo catch-all de SPA con 200 HTML.
        "/.well-known/oauth-protected-resource",
    ],
)
async def test_well_known_metadata_routes_win_over_the_spa_catch_all(
    database_url: str, panel_dist_dir: Path, path: str
) -> None:
    app = create_app(_api_settings(database_url, panel_dist_dir=panel_dist_dir))
    try:
        async with _client(app) as client:
            response = await client.get(path)

        assert response.status_code == 200
        assert response.headers["content-type"].startswith("application/json")
        assert response.text != "<html>panel</html>"
    finally:
        await app.state.container.aclose()


async def test_unsupported_well_known_document_never_falls_through_to_the_panel_shell(
    database_url: str, panel_dist_dir: Path
) -> None:
    """T049 (spec 008): este AS no publica `openid-configuration` (RFC 8414
    SS3, protocolo distinto de lo que si implementamos) -- sin una ruta
    propia bajo `/.well-known/*`, este catch-all de SPA lo serviria como
    200 HTML en vez del 404 JSON que le corresponde."""
    app = create_app(_api_settings(database_url, panel_dist_dir=panel_dist_dir))
    try:
        async with _client(app) as client:
            response = await client.get("/.well-known/openid-configuration")

        assert response.status_code == 404
        assert response.headers["content-type"].startswith("application/json")
        assert response.text != "<html>panel</html>"
    finally:
        await app.state.container.aclose()


async def test_well_known_never_falls_through_to_a_real_spa_even_with_oauth_disabled(
    database_url: str, panel_dist_dir: Path
) -> None:
    """Revision de seguridad (PR 44): `well_known_not_found_route()` se
    registra SIEMPRE (`_register_mcp_oauth_surface`), no solo cuando
    `ADS_MCP_OAUTH_ENABLED=true` -- con OAuth apagado Y el panel de verdad
    montado (`panel_dist_dir` con un `index.html` real, a diferencia de
    `tests/contracts/mcp_oauth/test_metadata_documents.py`, que lo evita a
    proposito), `/.well-known/*` sigue sin caer en el catch-all de SPA."""
    settings = _api_settings(database_url, panel_dist_dir=panel_dist_dir).model_copy(
        update={"mcp_oauth_enabled": False}
    )
    app = create_app(settings)
    try:
        async with _client(app) as client:
            response = await client.get("/.well-known/oauth-authorization-server")

        assert response.status_code == 404
        assert response.headers["content-type"].startswith("application/json")
        assert response.text != "<html>panel</html>"
    finally:
        await app.state.container.aclose()


async def test_well_known_with_an_unsupported_method_is_405_json_not_starlettes_plain_text(
    database_url: str, panel_dist_dir: Path
) -> None:
    """Revision de seguridad (PR 44): Starlette responde 405 en texto plano
    por defecto para una ruta cuyo metodo no casa -- `well_known_not_found_
    route()` registra un `methods=` amplio a proposito para poder devolver
    el MISMO sobre JSON que el resto de la API."""
    app = create_app(_api_settings(database_url, panel_dist_dir=panel_dist_dir))
    try:
        async with _client(app) as client:
            response = await client.post("/.well-known/oauth-authorization-server", json={})

        assert response.status_code == 405
        assert response.headers["content-type"].startswith("application/json")
        assert response.json()["error"]["code"] == "METHOD_NOT_ALLOWED"
    finally:
        await app.state.container.aclose()


async def test_token_endpoint_returns_an_oauth_error_json_not_the_panel_html(
    database_url: str, panel_dist_dir: Path
) -> None:
    app = create_app(_api_settings(database_url, panel_dist_dir=panel_dist_dir))
    try:
        async with _client(app) as client:
            response = await client.post("/token", data={})

        assert response.status_code != 200
        assert response.text != "<html>panel</html>"
        assert response.headers["content-type"].startswith("application/json")
        assert "error" in response.json()
    finally:
        await app.state.container.aclose()


async def test_authorize_endpoint_wins_over_the_spa_catch_all(
    database_url: str, panel_dist_dir: Path
) -> None:
    app = create_app(_api_settings(database_url, panel_dist_dir=panel_dist_dir))
    try:
        async with _client(app) as client:
            response = await client.get("/authorize")

        assert response.text != "<html>panel</html>"
    finally:
        await app.state.container.aclose()


async def test_register_and_revoke_routes_are_registered(
    database_url: str, panel_dist_dir: Path
) -> None:
    app = create_app(_api_settings(database_url, panel_dist_dir=panel_dist_dir))
    try:
        async with _client(app) as client:
            register_response = await client.post("/register", json={})
            # `client_id` ausente => `ClientAuthenticator` rechaza ANTES de
            # llegar al "token desconocido -> 200 igualmente" del RFC 7009
            # (`RevocationHandler.handle`, `sdk:handlers/revoke.py:39`) --
            # lo que prueba esta llamada es que la ruta EXISTE y responde
            # en forma OAuth, no el detalle de la 200 sin cliente.
            revoke_response = await client.post("/revoke", data={})

        assert register_response.text != "<html>panel</html>"
        assert revoke_response.text != "<html>panel</html>"
        assert revoke_response.status_code == 401
        assert revoke_response.json()["error"] == "unauthorized_client"
    finally:
        await app.state.container.aclose()


async def test_consent_mutation_without_csrf_token_is_rejected(
    database_url: str, panel_dist_dir: Path, seeded_owner: _SeededOwner
) -> None:
    """C-40/C-52: a diferencia de `/authorize`/`/token`/`/register`/
    `/revoke`, `/api/v1/mcp-oauth/consent/*` NUNCA se exime de CSRF."""
    app = create_app(_api_settings(database_url, panel_dist_dir=panel_dist_dir))
    try:
        async with _client(app, cookies={"ads_session": _RAW_SESSION_TOKEN}) as client:
            response = await client.post(f"/api/v1/mcp-oauth/consent/{uuid.uuid4()}/approve")

        assert response.status_code == 403
        assert response.json()["error"]["code"] == "CSRF_REJECTED"
    finally:
        await app.state.container.aclose()


async def test_consent_approve_with_mismatched_origin_is_rejected(
    database_url: str, panel_dist_dir: Path, seeded_owner: _SeededOwner
) -> None:
    """M4 de la revision de seguridad (16-sep, threat-model.md C-40): el
    doble envio de cookie por si solo no basta si un atacante puede leer
    `ads_csrf` (XSS en otro origen, red compartida) -- un `Origin` fuera de
    `ADS_PUBLIC_BASE_URL` corta la peticion aunque el token CSRF sea
    valido y la sesion este autenticada."""
    app = create_app(_api_settings(database_url, panel_dist_dir=panel_dist_dir))
    try:
        async with _client(app, cookies={"ads_session": _RAW_SESSION_TOKEN}) as client:
            await client.get("/api/v1/health")
            client.headers["X-CSRF-Token"] = client.cookies["ads_csrf"]
            client.headers["Origin"] = "https://evil.example"

            response = await client.post(f"/api/v1/mcp-oauth/consent/{uuid.uuid4()}/approve")

        assert response.status_code == 403
        assert response.json()["error"]["code"] == "CSRF_REJECTED"
    finally:
        await app.state.container.aclose()


async def test_consent_and_grants_routes_win_over_the_spa_catch_all(
    database_url: str, panel_dist_dir: Path, seeded_owner: _SeededOwner
) -> None:
    app = create_app(_api_settings(database_url, panel_dist_dir=panel_dist_dir))
    try:
        async with _client(app, cookies={"ads_session": _RAW_SESSION_TOKEN}) as client:
            consent_response = await client.get(f"/api/v1/mcp-oauth/consent/{uuid.uuid4()}")
            grants_response = await client.get("/api/v1/mcp-oauth/grants")

        # 404 (txn desconocido) / 200 (lista vacia): lo que importa es que
        # NINGUNA cayo en el catch-all de la SPA.
        assert consent_response.text != "<html>panel</html>"
        assert consent_response.status_code == 404
        assert grants_response.status_code == 200
        assert grants_response.json() == {"grants": []}
    finally:
        await app.state.container.aclose()


async def test_session_cookie_from_a_real_login_is_sent_to_the_consent_route(
    database_url: str, panel_dist_dir: Path, seeded_owner: _SeededOwner
) -> None:
    """`ads_session` se emite con `path=/api` (`iam/presentation/router.py::
    _set_session_cookie`); `/api/v1/mcp-oauth/consent/*` vive bajo `/api`,
    asi que un navegador real la manda ahi tambien -- se deja que el
    propio `httpx.AsyncClient` gestione las cookies (respeta `Path`) en vez
    de fijarla a mano, para probar el scope real, no solo el valor."""
    app = create_app(_api_settings(database_url, panel_dist_dir=panel_dist_dir))
    try:
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="https://test"
        ) as client:
            # `CsrfMiddleware` emite `ads_csrf` en la primera respuesta sin
            # cookie previa; toda peticion mutante posterior tiene que
            # devolverla en `X-CSRF-Token` (doble envio, mismo patron que
            # `tests/integration/brand/test_authorization_integration.py`).
            await client.get("/api/v1/health")
            client.headers["X-CSRF-Token"] = client.cookies["ads_csrf"]

            # Fusion lane/003: el login deja la cookie de sesion en un solo
            # paso (204); el reto TOTP del login se retiro con la lane.
            login = await client.post(
                "/api/v1/auth/login",
                json={"email": seeded_owner.email, "password": _CORRECT_PASSWORD},
            )
            assert login.status_code == 204, login.text
            assert "path=/api" in login.headers["set-cookie"].lower()

            consent_response = await client.get(f"/api/v1/mcp-oauth/consent/{uuid.uuid4()}")

        assert consent_response.status_code == 404
    finally:
        await app.state.container.aclose()


async def _seed_pending_request(database_url: str, *, client_id: str) -> uuid.UUID:
    engine = create_async_engine(database_url, pool_pre_ping=True)
    client = OAuthClient(
        client_id=client_id,
        client_name="Claude Code",
        redirect_uris=(RedirectUri("http://127.0.0.1:54321/callback"),),
        token_endpoint_auth_method=TokenEndpointAuthMethod.NONE,
        client_secret_hash=None,
        grant_types=("authorization_code", "refresh_token"),
        requested_scope=ScopeSet.parse("ads:read"),
        created_at=datetime.now(UTC),
    )
    now = datetime.now(UTC)
    request = AuthorizationRequest(
        txn_id=uuid.uuid4(),
        client_id=client.id,
        redirect_uri="http://127.0.0.1:54321/callback",
        code_challenge="E9Melhoa2OwvFrEMTJguCHaoeK1t8URWbuGJSstw-cM",
        client_state=None,
        scope_set=ScopeSet.parse("ads:read"),
        resource=ResourceIndicator("https://ads.test.ts.net/mcp"),
        created_at=now,
        expires_at=now + timedelta(minutes=10),
    )
    async with AsyncSession(engine, expire_on_commit=False) as session:
        await SqlClientRepository(session).save(client)
        await SqlAuthorizationRequestRepository(session, clock=SystemClock()).create(request)
        await session.commit()
    await engine.dispose()
    return request.id


async def _seed_owner_without_totp(database_url: str) -> tuple[uuid.UUID, str]:
    """Sin `totp_secret_encrypted`: el unico dueno de 002 para el que
    `methods` sale realmente vacio con el federado apagado (contracts/
    federated-login.md §2, `details` ausente == comportamiento legado).
    `seeded_owner` (arriba) SIEMPRE tiene TOTP, asi que no sirve para este
    caso -- con TOTP, `details.methods == ["totp"]` es lo correcto, no
    "sin details" (ya cubierto por `test_consent_router.py::
    test_owner_without_totp_and_federated_off_gets_the_legacy_401_without_details`,
    con el router aislado; esto lo confirma contra la app COMPLETA)."""
    engine = create_async_engine(database_url, pool_pre_ping=True)
    owner_id = uuid.uuid4()
    email = f"owner-no-totp-{owner_id.hex[:10]}@safent.example"
    password_hash = Argon2PasswordHasher().hash(_CORRECT_PASSWORD)
    async with engine.begin() as connection:
        await connection.execute(
            text(
                "INSERT INTO owners (id, email, password_hash) VALUES (:id, :email, :password_hash)"
            ),
            {"id": str(owner_id), "email": email, "password_hash": password_hash},
        )
    await engine.dispose()
    return owner_id, email


async def _delete_owner(database_url: str, owner_id: uuid.UUID) -> None:
    engine = create_async_engine(database_url, pool_pre_ping=True)
    async with engine.begin() as connection:
        await connection.execute(
            text("DELETE FROM sessions WHERE owner_id = :id"), {"id": str(owner_id)}
        )
        await connection.execute(text("DELETE FROM owners WHERE id = :id"), {"id": str(owner_id)})
    await engine.dispose()


async def test_federated_login_off_by_default_status_404_and_approve_401_without_details(
    database_url: str, panel_dist_dir: Path
) -> None:
    """T070 (SC-106), test de humo explicito: `_api_settings` de este
    fichero no fija `federated_login_enabled` (el defecto, `False`), asi
    que `/auth/federated/status` ni siquiera existe (404 por enrutado,
    contracts/federated-login.md §1) y el 401 de `approve` de un dueno sin
    TOTP sigue siendo el legado de 002 -- `details` vacio (`{}`), la forma
    que `ApiError` sirve cuando no hay ninguna via viable que anunciar."""
    client_id = f"oauth-routes-federated-off-test-{uuid.uuid4().hex[:8]}"
    txn_id = await _seed_pending_request(database_url, client_id=client_id)
    owner_id, email = await _seed_owner_without_totp(database_url)
    app = create_app(_api_settings(database_url, panel_dist_dir=panel_dist_dir))
    try:
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="https://test"
        ) as client:
            status_response = await client.get("/api/v1/auth/federated/status")

            await client.get("/api/v1/health")
            client.headers["X-CSRF-Token"] = client.cookies["ads_csrf"]
            login = await client.post(
                "/api/v1/auth/login", json={"email": email, "password": _CORRECT_PASSWORD}
            )
            assert login.status_code == 204, login.text

            approve_response = await client.post(f"/api/v1/mcp-oauth/consent/{txn_id}/approve")
    finally:
        await app.state.container.aclose()
        await _delete_owner(database_url, owner_id)
        engine = create_async_engine(database_url, pool_pre_ping=True)
        async with engine.begin() as connection:
            await connection.execute(
                text("DELETE FROM oauth_clients WHERE client_id = :id"), {"id": client_id}
            )
        await engine.dispose()

    assert status_response.status_code == 404
    assert approve_response.status_code == 401
    approve_error = approve_response.json()["error"]
    assert approve_error["code"] == "REAUTH_REQUIRED"
    assert approve_error["details"] == {}

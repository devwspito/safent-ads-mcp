"""Flujo completo del codigo de autorizacion, en proceso, contra Postgres
real (tasks.md T020, contracts/oauth.md, threat-model.md C-36..C-58):
`ASGITransport` sobre `create_app(...)` -- 401 -> metadatos del recurso
protegido -> metadatos del AS -> `POST /register` -> `GET /authorize`
(PKCE S256) -> 302 al panel -> login (email+password+TOTP) -> consentir ->
`POST /token` con `code_verifier` -> acceso+refresco -> `initialize` +
`tools/list` MCP de verdad (cliente del propio SDK, mismo patron que
`tests/integration/mcp/test_streamable_http_endpoint.py`) -> rotacion de
refresco.

Casos negativos en el mismo fichero (mismo motivo que
`tests/integration/mcp_oauth/test_consent_router.py`: comparten todo el
cableado de arranque): PKCE incorrecto, codigo reusado (revoca la familia,
C-38), `resource` ajeno en `/authorize` (invalid_target), `redirect_uri`
con puerto de bucle local distinto (pasa) vs host/ruta distintos (falla),
refresh reutilizado tras rotar (revoca la familia entera, C-43), confusion
de audiencia (C-47), interruptores `ADS_MCP_OAUTH_ENABLED`/
`ADS_MCP_STATIC_TOKEN_ENABLED` (C-53), y revocacion desde el panel (C-55).

Una `app = create_app(...)` por test (mismo patron que
`tests/integration/composition/test_oauth_routes_registered.py`): cada una
trae su propio `RateLimitMiddleware` con las cubetas llenas -- los
presupuestos reales (30/min `/authorize`, 60/min `/token`, 10/h `/register`,
20/min consentimiento) nunca se agotan entre pruebas."""

from __future__ import annotations

import base64
import hashlib
import secrets
import uuid
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

import httpx
import httpx2
import pyotp
import pytest
from mcp import ClientSession
from mcp.client.streamable_http import streamable_http_client
from sqlalchemy import text
from sqlalchemy.ext.asyncio import create_async_engine

from safent_ads.composition.app import create_app
from safent_ads.composition.settings import ApiSettings
from safent_ads.iam.infrastructure.aesgcm_totp_cipher import (
    PURPOSE_TOTP_SECRET,
    AesGcmTotpCipher,
)
from safent_ads.iam.infrastructure.argon2_password_hasher import Argon2PasswordHasher

pytestmark = pytest.mark.integration

_VALID_32_BYTE_KEY_B64 = "MDAwMDAwMDAwMDAwMDAwMDAwMDAwMDAwMDAwMDAwMDA="
_CORRECT_PASSWORD = "correct horse battery staple"  # noqa: S105 - fixture, no secreto real
_PUBLIC_BASE_URL = "https://ads.test.ts.net"
_CANONICAL_RESOURCE = f"{_PUBLIC_BASE_URL}/mcp"
_REDIRECT_URI = "http://127.0.0.1:54321/callback"
_CLIENT_NAME = "T020 Full Flow Test Client"
_SCOPE = "ads:read ads:propose"
_SSE_ACCEPT = "application/json, text/event-stream"
_EXPECTED_401_BODY = {"error": "invalid_token", "error_description": "Authentication required"}


def _api_settings(database_url: str, *, panel_dist_dir: Path, **overrides: object) -> ApiSettings:
    defaults: dict[str, object] = {
        "database_url": database_url,
        "session_secret": "test-session-secret-0123456789abcdef",
        "totp_enc_key": _VALID_32_BYTE_KEY_B64,
        "mcp_token": "test-mcp-token-abc123",
        "broker_socket_path": "/tmp/safent-ads-test/broker.sock",
        "public_base_url": _PUBLIC_BASE_URL,
        "telegram_bot_token": "123456:test-bot-token",
        "telegram_owner_chat_ids": [111222333],
        "approval_signing_key": _VALID_32_BYTE_KEY_B64,
        "mcp_oauth_enabled": True,
        "panel_dist_dir": panel_dist_dir,
        # Fusion lane/003 (004 tasks.md A6/A10): `ApiSettings` exige ahora
        # exactamente uno de los dos modos de autoridad de `/mcp`.
        "seat_authority_enabled": True,
        "enterprise_origin": "https://enterprise.test",
        "enterprise_service_secret": "a" * 64,
        "enterprise_org_ids": frozenset({uuid.UUID("00000000-0000-0000-0000-000000000001")}),
    }
    defaults.update(overrides)
    return ApiSettings(**defaults)


@pytest.fixture
def panel_dist_dir(tmp_path: Path) -> Path:
    dist_dir = tmp_path / "dist"
    dist_dir.mkdir()
    (dist_dir / "index.html").write_text("<html>panel</html>", encoding="utf-8")
    return dist_dir


@dataclass(frozen=True, slots=True)
class _SeededOwner:
    owner_id: uuid.UUID
    email: str
    totp_secret: str


@pytest.fixture
async def owner(database_url: str) -> AsyncIterator[_SeededOwner]:
    engine = create_async_engine(database_url, pool_pre_ping=True)
    owner_id = uuid.uuid4()
    email = f"owner-{owner_id.hex[:10]}@safent.example"
    totp_secret = pyotp.random_base32()
    password_hash = Argon2PasswordHasher().hash(_CORRECT_PASSWORD)
    encrypted_secret = AesGcmTotpCipher(_VALID_32_BYTE_KEY_B64).encrypt(
        totp_secret, purpose=PURPOSE_TOTP_SECRET
    )
    now = datetime.now(UTC)
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
    try:
        yield _SeededOwner(owner_id=owner_id, email=email, totp_secret=totp_secret)
    finally:
        async with engine.begin() as connection:
            await connection.execute(
                text("DELETE FROM sessions WHERE owner_id = :id"), {"id": str(owner_id)}
            )
            # 0020_totp_replay_guard: FK RESTRICT hacia `owners`.
            await connection.execute(
                text("DELETE FROM totp_reauth_confirmations WHERE owner_id = :id"),
                {"id": str(owner_id)},
            )
            await connection.execute(
                text("DELETE FROM owners WHERE id = :id"), {"id": str(owner_id)}
            )
        await engine.dispose()


@pytest.fixture(autouse=True)
async def _cleanup_seeded_clients(database_url: str) -> AsyncIterator[None]:
    """`oauth_clients.client_id -> oauth_authorization_requests`/
    `oauth_grants` son CASCADE (0035_mcp_oauth): borrar por `client_name`
    se lleva por delante todo lo que este fichero sembro, sin depender del
    `client_id` (lo asigna el SDK, `uuid4()` al registrar)."""
    yield
    engine = create_async_engine(database_url, pool_pre_ping=True)
    async with engine.begin() as connection:
        await connection.execute(
            text("DELETE FROM oauth_clients WHERE client_name = :name"), {"name": _CLIENT_NAME}
        )
    await engine.dispose()


def _pkce_pair() -> tuple[str, str]:
    verifier = base64.urlsafe_b64encode(secrets.token_bytes(32)).rstrip(b"=").decode("ascii")
    digest = hashlib.sha256(verifier.encode("ascii")).digest()
    challenge = base64.urlsafe_b64encode(digest).rstrip(b"=").decode("ascii")
    return verifier, challenge


def _totp_code(secret: str, *, step: int) -> str:
    """`step` estrictamente creciente por cada codigo TOTP consumido en la
    MISMA prueba -- login y cada `X-Reauth-Token` comparten el mismo
    contador RFC 6238 de tolerancia +/-1 paso; un `step` ya usado nunca
    vuelve a confirmar nada (`totp_reauth_confirmations`, C-41)."""
    moment = datetime.now(UTC) + timedelta(seconds=30 * step)
    return pyotp.TOTP(secret).at(moment)


async def _register_public_client(
    client: httpx.AsyncClient, *, redirect_uri: str = _REDIRECT_URI, scope: str = _SCOPE
) -> dict[str, object]:
    response = await client.post(
        "/register",
        json={
            "client_name": _CLIENT_NAME,
            "redirect_uris": [redirect_uri],
            "grant_types": ["authorization_code", "refresh_token"],
            "response_types": ["code"],
            "token_endpoint_auth_method": "none",
            "scope": scope,
        },
    )
    assert response.status_code == 201, response.text
    return response.json()


async def _authorize(
    client: httpx.AsyncClient,
    *,
    client_id: str,
    redirect_uri: str,
    code_challenge: str,
    state: str,
    resource: str = _CANONICAL_RESOURCE,
    scope: str = _SCOPE,
) -> httpx.Response:
    return await client.get(
        "/authorize",
        params={
            "client_id": client_id,
            "redirect_uri": redirect_uri,
            "response_type": "code",
            "code_challenge": code_challenge,
            "code_challenge_method": "S256",
            "state": state,
            "scope": scope,
            "resource": resource,
        },
    )


def _txn_from_redirect(response: httpx.Response) -> uuid.UUID:
    assert response.status_code == 302, response.text
    location = response.headers["location"]
    assert location.startswith(f"{_PUBLIC_BASE_URL}/oauth/autorizar?txn=")
    query = parse_qs(urlsplit(location).query)
    return uuid.UUID(query["txn"][0])


async def _login(client: httpx.AsyncClient, *, owner: _SeededOwner) -> None:
    """Fusion lane/003: `POST /api/v1/auth/login` deja la cookie de sesion
    en un solo paso (204) -- el reto TOTP del login se retiro con la lane.
    El TOTP fresco por accion (`X-Reauth-Token`) SI sigue vivo, y es lo que
    `_approve`/`_revoke` ejercitan mas abajo."""
    health = await client.get("/api/v1/health")
    assert health.status_code == 200
    client.headers["X-CSRF-Token"] = client.cookies["ads_csrf"]
    login_response = await client.post(
        "/api/v1/auth/login", json={"email": owner.email, "password": _CORRECT_PASSWORD}
    )
    assert login_response.status_code == 204, login_response.text


async def _get_consent(client: httpx.AsyncClient, *, txn_id: uuid.UUID) -> dict[str, object]:
    response = await client.get(f"/api/v1/mcp-oauth/consent/{txn_id}")
    assert response.status_code == 200, response.text
    return response.json()


async def _approve(
    client: httpx.AsyncClient, *, txn_id: uuid.UUID, owner: _SeededOwner, totp_step: int
) -> dict[str, str]:
    response = await client.post(
        f"/api/v1/mcp-oauth/consent/{txn_id}/approve",
        headers={"X-Reauth-Token": _totp_code(owner.totp_secret, step=totp_step)},
    )
    assert response.status_code == 200, response.text
    redirect_to = response.json()["redirect_to"]
    parsed = urlsplit(redirect_to)
    query = parse_qs(parsed.query)
    return {
        "code": query["code"][0],
        "state": query.get("state", [None])[0],
        "iss": query["iss"][0],
        "redirect_prefix": f"{parsed.scheme}://{parsed.netloc}{parsed.path}",
    }


async def _exchange_code(
    client: httpx.AsyncClient,
    *,
    code: str,
    redirect_uri: str,
    client_id: str,
    code_verifier: str,
    resource: str = _CANONICAL_RESOURCE,
) -> httpx.Response:
    return await client.post(
        "/token",
        data={
            "grant_type": "authorization_code",
            "code": code,
            "redirect_uri": redirect_uri,
            "client_id": client_id,
            "code_verifier": code_verifier,
            "resource": resource,
        },
    )


async def _refresh(
    client: httpx.AsyncClient,
    *,
    refresh_token: str,
    client_id: str,
    resource: str = _CANONICAL_RESOURCE,
) -> httpx.Response:
    return await client.post(
        "/token",
        data={
            "grant_type": "refresh_token",
            "refresh_token": refresh_token,
            "client_id": client_id,
            "resource": resource,
        },
    )


@asynccontextmanager
async def _mcp_client(app: object, *, access_token: str) -> AsyncIterator[httpx2.AsyncClient]:
    async with httpx2.AsyncClient(
        transport=httpx2.ASGITransport(app=app),
        base_url=_PUBLIC_BASE_URL,
        headers={"Authorization": f"Bearer {access_token}"},
    ) as client:
        yield client


async def _mcp_health(client: httpx.AsyncClient, *, access_token: str | None) -> httpx.Response:
    headers = {"Authorization": f"Bearer {access_token}"} if access_token else {}
    return await client.get("/mcp/health", headers=headers)


async def _run_initialize_and_list_tools(app: object, *, access_token: str) -> list[str]:
    async with (
        _mcp_client(app, access_token=access_token) as http_client,
        streamable_http_client(f"{_PUBLIC_BASE_URL}/mcp", http_client=http_client) as (
            read,
            write,
        ),
        ClientSession(read, write) as session,
    ):
        await session.initialize()
        tools_result = await session.list_tools()
    return [tool.name for tool in tools_result.tools]


def _panel_client(app: object) -> httpx.AsyncClient:
    return httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="https://testserver")


async def _assert_discovery_flow_reaches_the_protected_resource_metadata(
    client: httpx.AsyncClient,
) -> None:
    """Pasos 1-3 del contrato (SS1/SS2, ya probados campo a campo en
    `tests/contracts/mcp_oauth/`): aqui solo se comprueba que un cliente
    real puede LLEGAR de un 401 a los metadatos sin saber nada de
    antemano -- la forma exacta de cada documento es responsabilidad de
    los tests de contrato, no de este flujo end-to-end."""
    challenge = await client.get("/mcp/health")
    assert challenge.status_code == 401
    assert challenge.json() == _EXPECTED_401_BODY
    resource_metadata_url = (
        challenge.headers["www-authenticate"].split('resource_metadata="')[1].rstrip('"')
    )

    prm = await client.get(resource_metadata_url.removeprefix(_PUBLIC_BASE_URL))
    assert prm.status_code == 200
    assert prm.json()["authorization_servers"] == [_PUBLIC_BASE_URL]

    as_metadata = await client.get("/.well-known/oauth-authorization-server")
    assert as_metadata.status_code == 200
    assert as_metadata.json()["issuer"] == _PUBLIC_BASE_URL


async def test_full_authorization_code_flow_end_to_end(
    database_url: str, panel_dist_dir: Path, owner: _SeededOwner
) -> None:
    app = create_app(_api_settings(database_url, panel_dist_dir=panel_dist_dir))
    try:
        async with app.router.lifespan_context(app):
            async with _panel_client(app) as anon_client:
                # 1-3. 401 -> metadatos del recurso protegido -> del AS.
                await _assert_discovery_flow_reaches_the_protected_resource_metadata(anon_client)

                # 4. DCR de un cliente publico con redirect_uri de bucle local.
                registration = await _register_public_client(anon_client)
                client_id = registration["client_id"]
                assert "client_secret" not in registration

                # 5. `/authorize` con PKCE S256 -> 302 al panel.
                verifier, challenge_value = _pkce_pair()
                authorize_response = await _authorize(
                    anon_client,
                    client_id=client_id,
                    redirect_uri=_REDIRECT_URI,
                    code_challenge=challenge_value,
                    state="xyz-state-1",
                )
                txn_id = _txn_from_redirect(authorize_response)

                # 6. Login del propietario (email + password + TOTP).
                await _login(anon_client, owner=owner)

                # 7. La pantalla de consentimiento muestra client_id/nombre.
                consent = await _get_consent(anon_client, txn_id=txn_id)
                assert consent["client_id"] == client_id
                assert consent["client_name"] == _CLIENT_NAME
                assert consent["redirect_host"] == "127.0.0.1:54321"

                # 8. Aprobar con TOTP fresco -> code+state+iss.
                approved = await _approve(anon_client, txn_id=txn_id, owner=owner, totp_step=1)
                assert approved["state"] == "xyz-state-1"
                assert approved["iss"] == _PUBLIC_BASE_URL
                assert approved["redirect_prefix"] == _REDIRECT_URI

                # 9. Canjear el codigo con `code_verifier`.
                token_response = await _exchange_code(
                    anon_client,
                    code=approved["code"],
                    redirect_uri=_REDIRECT_URI,
                    client_id=client_id,
                    code_verifier=verifier,
                )
                assert token_response.status_code == 200, token_response.text
                tokens = token_response.json()
                assert tokens["token_type"] == "Bearer"  # noqa: S105 - RFC 6749 SS5.1, no un secreto
                assert set(tokens["scope"].split()) == {"ads:read", "ads:propose"}
                access_token = tokens["access_token"]
                refresh_token = tokens["refresh_token"]

                # 10. `/mcp/health` con el token recien emitido.
                health = await _mcp_health(anon_client, access_token=access_token)
                assert health.status_code == 200

            # 11. `initialize` + `tools/list` MCP de verdad con ese token.
            tool_names = await _run_initialize_and_list_tools(app, access_token=access_token)
            assert tool_names, "el catalogo de herramientas llego vacio"

            async with _panel_client(app) as anon_client:
                # 12. Rotacion de refresco: token nuevo, refresco nuevo.
                refreshed = await _refresh(
                    anon_client, refresh_token=refresh_token, client_id=client_id
                )
                assert refreshed.status_code == 200, refreshed.text
                refreshed_tokens = refreshed.json()
                assert refreshed_tokens["refresh_token"] != refresh_token
                assert refreshed_tokens["access_token"] != access_token

            rotated_tool_names = await _run_initialize_and_list_tools(
                app, access_token=refreshed_tokens["access_token"]
            )
            assert rotated_tool_names == tool_names
    finally:
        await app.state.container.aclose()


async def test_wrong_code_verifier_is_rejected(
    database_url: str, panel_dist_dir: Path, owner: _SeededOwner
) -> None:
    app = create_app(_api_settings(database_url, panel_dist_dir=panel_dist_dir))
    try:
        async with app.router.lifespan_context(app), _panel_client(app) as client:
            registration = await _register_public_client(client)
            client_id = registration["client_id"]
            _verifier, challenge_value = _pkce_pair()
            txn_id = _txn_from_redirect(
                await _authorize(
                    client,
                    client_id=client_id,
                    redirect_uri=_REDIRECT_URI,
                    code_challenge=challenge_value,
                    state="s1",
                )
            )
            await _login(client, owner=owner)
            approved = await _approve(client, txn_id=txn_id, owner=owner, totp_step=1)

            wrong_verifier, _ = _pkce_pair()
            response = await _exchange_code(
                client,
                code=approved["code"],
                redirect_uri=_REDIRECT_URI,
                client_id=client_id,
                code_verifier=wrong_verifier,
            )

        assert response.status_code == 400, response.text
        assert response.json()["error"] == "invalid_grant"
    finally:
        await app.state.container.aclose()


async def test_reused_authorization_code_fails_and_revokes_the_issued_grant(
    database_url: str, panel_dist_dir: Path, owner: _SeededOwner
) -> None:
    """threat-model.md C-38: el segundo canje del MISMO codigo falla, y la
    concesion que nacio del primero deja de servir para llamar a `/mcp`."""
    app = create_app(_api_settings(database_url, panel_dist_dir=panel_dist_dir))
    try:
        async with app.router.lifespan_context(app):
            async with _panel_client(app) as client:
                registration = await _register_public_client(client)
                client_id = registration["client_id"]
                verifier, challenge_value = _pkce_pair()
                txn_id = _txn_from_redirect(
                    await _authorize(
                        client,
                        client_id=client_id,
                        redirect_uri=_REDIRECT_URI,
                        code_challenge=challenge_value,
                        state="s1",
                    )
                )
                await _login(client, owner=owner)
                approved = await _approve(client, txn_id=txn_id, owner=owner, totp_step=1)

                first = await _exchange_code(
                    client,
                    code=approved["code"],
                    redirect_uri=_REDIRECT_URI,
                    client_id=client_id,
                    code_verifier=verifier,
                )
                assert first.status_code == 200, first.text
                first_access_token = first.json()["access_token"]

                health_before_replay = await _mcp_health(client, access_token=first_access_token)
                assert health_before_replay.status_code == 200

                second = await _exchange_code(
                    client,
                    code=approved["code"],
                    redirect_uri=_REDIRECT_URI,
                    client_id=client_id,
                    code_verifier=verifier,
                )
                assert second.status_code == 400, second.text
                assert second.json()["error"] == "invalid_grant"

                health_after_replay = await _mcp_health(client, access_token=first_access_token)
                assert health_after_replay.status_code == 401
                assert health_after_replay.json() == _EXPECTED_401_BODY
    finally:
        await app.state.container.aclose()


async def test_authorize_with_a_non_canonical_resource_is_invalid_target(
    database_url: str, panel_dist_dir: Path
) -> None:
    app = create_app(_api_settings(database_url, panel_dist_dir=panel_dist_dir))
    try:
        async with app.router.lifespan_context(app), _panel_client(app) as client:
            registration = await _register_public_client(client)
            client_id = registration["client_id"]
            _verifier, challenge_value = _pkce_pair()

            response = await _authorize(
                client,
                client_id=client_id,
                redirect_uri=_REDIRECT_URI,
                code_challenge=challenge_value,
                state="s1",
                resource="https://ads.test.ts.net/other-resource",
            )

        assert response.status_code == 302, response.text
        location = response.headers["location"]
        assert location.startswith(_REDIRECT_URI)
        query = parse_qs(urlsplit(location).query)
        assert query["error"] == ["invalid_target"]
        assert query["state"] == ["s1"]
    finally:
        await app.state.container.aclose()


async def test_registration_without_explicit_scope_defaults_to_read_only(
    database_url: str, panel_dist_dir: Path
) -> None:
    """L2 de la revision de seguridad (16-sep, minimo privilegio): un
    cliente DCR que no pide `scope` recibe SOLO `ads:read` -- `ads:propose`
    (escribir propuestas) hay que pedirlo explicitamente, nunca cae por
    omision (`ClientRegistrationOptions.default_scopes`)."""
    app = create_app(_api_settings(database_url, panel_dist_dir=panel_dist_dir))
    try:
        async with app.router.lifespan_context(app), _panel_client(app) as client:
            response = await client.post(
                "/register",
                json={
                    "client_name": _CLIENT_NAME,
                    "redirect_uris": [_REDIRECT_URI],
                    "grant_types": ["authorization_code", "refresh_token"],
                    "response_types": ["code"],
                    "token_endpoint_auth_method": "none",
                },
            )

        assert response.status_code == 201, response.text
        assert response.json()["scope"] == "ads:read"
    finally:
        await app.state.container.aclose()


async def test_redirect_uri_loopback_port_may_vary_but_host_and_path_may_not(
    database_url: str, panel_dist_dir: Path, owner: _SeededOwner
) -> None:
    app = create_app(_api_settings(database_url, panel_dist_dir=panel_dist_dir))
    try:
        async with app.router.lifespan_context(app), _panel_client(app) as client:
            registration = await _register_public_client(client)
            client_id = registration["client_id"]

            # Puerto distinto, mismo host y ruta: pasa.
            _verifier, challenge_value = _pkce_pair()
            other_port_response = await _authorize(
                client,
                client_id=client_id,
                redirect_uri="http://127.0.0.1:9999/callback",
                code_challenge=challenge_value,
                state="s-port",
            )
            assert other_port_response.status_code == 302
            assert other_port_response.headers["location"].startswith(
                f"{_PUBLIC_BASE_URL}/oauth/autorizar?txn="
            )

            # Host distinto (aunque tambien de bucle local): falla, directo.
            other_host_response = await _authorize(
                client,
                client_id=client_id,
                redirect_uri="http://localhost:54321/callback",
                code_challenge=challenge_value,
                state="s-host",
            )
            assert other_host_response.status_code == 400, other_host_response.text
            assert other_host_response.json()["error"] == "invalid_request"

            # Ruta distinta: falla, directo.
            other_path_response = await _authorize(
                client,
                client_id=client_id,
                redirect_uri="http://127.0.0.1:54321/other-path",
                code_challenge=challenge_value,
                state="s-path",
            )
            assert other_path_response.status_code == 400, other_path_response.text
            assert other_path_response.json()["error"] == "invalid_request"
    finally:
        await app.state.container.aclose()


async def test_refresh_token_reuse_after_rotation_revokes_the_whole_family(
    database_url: str, panel_dist_dir: Path, owner: _SeededOwner
) -> None:
    """threat-model.md C-43."""
    app = create_app(_api_settings(database_url, panel_dist_dir=panel_dist_dir))
    try:
        async with app.router.lifespan_context(app):
            async with _panel_client(app) as client:
                registration = await _register_public_client(client)
                client_id = registration["client_id"]
                verifier, challenge_value = _pkce_pair()
                txn_id = _txn_from_redirect(
                    await _authorize(
                        client,
                        client_id=client_id,
                        redirect_uri=_REDIRECT_URI,
                        code_challenge=challenge_value,
                        state="s1",
                    )
                )
                await _login(client, owner=owner)
                approved = await _approve(client, txn_id=txn_id, owner=owner, totp_step=1)
                first = await _exchange_code(
                    client,
                    code=approved["code"],
                    redirect_uri=_REDIRECT_URI,
                    client_id=client_id,
                    code_verifier=verifier,
                )
                assert first.status_code == 200, first.text
                original_access_token = first.json()["access_token"]
                original_refresh_token = first.json()["refresh_token"]

                rotated = await _refresh(
                    client, refresh_token=original_refresh_token, client_id=client_id
                )
                assert rotated.status_code == 200, rotated.text
                rotated_access_token = rotated.json()["access_token"]

                replay = await _refresh(
                    client, refresh_token=original_refresh_token, client_id=client_id
                )
                assert replay.status_code == 400, replay.text
                assert replay.json()["error"] == "invalid_grant"

                original_still_works = await _mcp_health(client, access_token=original_access_token)
                rotated_now_revoked = await _mcp_health(client, access_token=rotated_access_token)

            assert original_still_works.status_code == 401
            assert rotated_now_revoked.status_code == 401
    finally:
        await app.state.container.aclose()


async def test_oauth_access_token_cannot_call_the_panel_me_endpoint(
    database_url: str, panel_dist_dir: Path, owner: _SeededOwner
) -> None:
    """threat-model.md C-47: sin confusion de audiencia."""
    app = create_app(_api_settings(database_url, panel_dist_dir=panel_dist_dir))
    try:
        async with app.router.lifespan_context(app), _panel_client(app) as client:
            registration = await _register_public_client(client)
            client_id = registration["client_id"]
            verifier, challenge_value = _pkce_pair()
            txn_id = _txn_from_redirect(
                await _authorize(
                    client,
                    client_id=client_id,
                    redirect_uri=_REDIRECT_URI,
                    code_challenge=challenge_value,
                    state="s1",
                )
            )
            await _login(client, owner=owner)
            approved = await _approve(client, txn_id=txn_id, owner=owner, totp_step=1)
            token_response = await _exchange_code(
                client,
                code=approved["code"],
                redirect_uri=_REDIRECT_URI,
                client_id=client_id,
                code_verifier=verifier,
            )
            access_token = token_response.json()["access_token"]

            # Cliente NUEVO, sin la cookie de sesion del login -- solo el
            # bearer OAuth, para probar la audiencia sola.
            async with _panel_client(app) as bearer_only_client:
                me_response = await bearer_only_client.get(
                    "/api/v1/auth/me", headers={"Authorization": f"Bearer {access_token}"}
                )

        assert me_response.status_code == 401
    finally:
        await app.state.container.aclose()


async def test_session_cookie_cannot_call_mcp(
    database_url: str, panel_dist_dir: Path, owner: _SeededOwner
) -> None:
    """threat-model.md C-47, el otro sentido: la cookie de sesion del panel
    no sirve para `/mcp`."""
    app = create_app(_api_settings(database_url, panel_dist_dir=panel_dist_dir))
    try:
        async with app.router.lifespan_context(app), _panel_client(app) as client:
            await _login(client, owner=owner)
            assert client.cookies.get("ads_session") is not None

            response = await client.get("/mcp/health")

        assert response.status_code == 401
        assert response.json() == _EXPECTED_401_BODY
    finally:
        await app.state.container.aclose()


async def test_oauth_disabled_removes_the_authorization_server_but_static_token_still_works(
    database_url: str, tmp_path: Path
) -> None:
    """threat-model.md C-53/plan.md "Ajustes". Sin build del panel
    (`panel_dist_dir` inexistente, mismo patron que la `api_settings` de
    `tests/conftest.py`): `_mount_panel_spa` no registra ningun catch-all,
    asi que un 404 aqui es 404 de verdad -- ninguna ruta del AS quedo
    registrada -- y no un 200 con `index.html` de por medio.

    M6 de la revision de seguridad (16-sep): `mcp_static_token_enabled` es
    `False` por defecto -- con OAuth apagado, la via estatica es la UNICA
    forma de autenticar `/mcp`, asi que este test la enciende
    explicitamente (si no, `static_health` seria 401, no 200)."""
    settings = _api_settings(
        database_url,
        panel_dist_dir=tmp_path / "no-panel-build",
        mcp_oauth_enabled=False,
        mcp_static_token_enabled=True,
    )
    app = create_app(settings)
    try:
        async with app.router.lifespan_context(app), _panel_client(app) as client:
            as_metadata = await client.get("/.well-known/oauth-authorization-server")
            prm_metadata = await client.get("/.well-known/oauth-protected-resource/mcp")
            authorize = await client.get("/authorize")
            register = await client.post("/register", json={})

            static_token = settings.mcp_token.get_secret_value()
            static_health = await client.get(
                "/mcp/health", headers={"Authorization": f"Bearer {static_token}"}
            )

        assert as_metadata.status_code == 404
        assert prm_metadata.status_code == 404
        assert authorize.status_code == 404
        assert register.status_code == 404
        assert static_health.status_code == 200
    finally:
        await app.state.container.aclose()


async def test_static_token_rejected_when_static_token_disabled(
    database_url: str, panel_dist_dir: Path
) -> None:
    """threat-model.md C-53: `ADS_MCP_STATIC_TOKEN_ENABLED=false` apaga
    SOLO la via de emergencia, no el AS (que sigue con su default `true`)."""
    settings = _api_settings(
        database_url, panel_dist_dir=panel_dist_dir, mcp_static_token_enabled=False
    )
    app = create_app(settings)
    try:
        async with app.router.lifespan_context(app), _panel_client(app) as client:
            response = await client.get(
                "/mcp/health",
                headers={"Authorization": f"Bearer {settings.mcp_token.get_secret_value()}"},
            )

        assert response.status_code == 401
        assert response.json() == _EXPECTED_401_BODY
    finally:
        await app.state.container.aclose()


async def test_panel_revoke_kills_the_grant_and_the_next_mcp_call_is_401(
    database_url: str, panel_dist_dir: Path, owner: _SeededOwner
) -> None:
    """threat-model.md C-55: revocar desde «Agentes conectados» corta la
    concesion entera; la proxima llamada a `/mcp` con ese token es 401."""
    app = create_app(_api_settings(database_url, panel_dist_dir=panel_dist_dir))
    try:
        async with app.router.lifespan_context(app), _panel_client(app) as client:
            registration = await _register_public_client(client)
            client_id = registration["client_id"]
            verifier, challenge_value = _pkce_pair()
            txn_id = _txn_from_redirect(
                await _authorize(
                    client,
                    client_id=client_id,
                    redirect_uri=_REDIRECT_URI,
                    code_challenge=challenge_value,
                    state="s1",
                )
            )
            # `-1/0/1`, no `0/1/2`: la tolerancia de `PyotpTotpVerifier` es
            # +/-1 paso alrededor del instante REAL de cada verificacion
            # (`matched_time_step`), no relativa a la primera llamada -- tres
            # consumos casi simultaneos solo caben en esa ventana centrada.
            await _login(client, owner=owner)
            approved = await _approve(client, txn_id=txn_id, owner=owner, totp_step=0)
            token_response = await _exchange_code(
                client,
                code=approved["code"],
                redirect_uri=_REDIRECT_URI,
                client_id=client_id,
                code_verifier=verifier,
            )
            access_token = token_response.json()["access_token"]

            still_works = await _mcp_health(client, access_token=access_token)
            assert still_works.status_code == 200

            grants_response = await client.get("/api/v1/mcp-oauth/grants")
            assert grants_response.status_code == 200, grants_response.text
            grants = grants_response.json()["grants"]
            grant = next(g for g in grants if g["client_id"] == client_id)

            revoke_path = f"/api/v1/mcp-oauth/grants/{grant['grant_id']}/revoke"
            # 002b (tasks.md T060, contracts/federated-login.md §2, decision
            # 4 del dueno): revocar ahora encadena 401 (frescura) -> 428
            # (confirmacion) -> el efecto real. Un TOTP fresco marca la
            # sesion (`fresh_identification.py`), asi que el segundo POST
            # no necesita un SEGUNDO codigo -- solo `X-Action-Confirmation`.
            confirmation_required = await client.post(
                revoke_path, headers={"X-Reauth-Token": _totp_code(owner.totp_secret, step=1)}
            )
            assert confirmation_required.status_code == 428, confirmation_required.text
            confirmation_token = confirmation_required.json()["error"]["details"][
                "confirmation_token"
            ]
            revoke_response = await client.post(
                revoke_path, headers={"X-Action-Confirmation": confirmation_token}
            )
            assert revoke_response.status_code == 204, revoke_response.text

            after_revoke = await _mcp_health(client, access_token=access_token)

        assert after_revoke.status_code == 401
        assert after_revoke.json() == _EXPECTED_401_BODY
    finally:
        await app.state.container.aclose()


async def test_public_client_can_call_revoke_without_a_client_secret(
    database_url: str, panel_dist_dir: Path
) -> None:
    """Prueba de comportamiento que respalda la correccion de metadatos de
    `tests/contracts/mcp_oauth/test_metadata_documents.py`: `SdkOAuthProvider.
    _require_public_client()` nunca registra otra cosa que clientes `none`,
    y `/revoke` los autentica con solo `client_id` -- si `ClientAuthenticator`
    exigiera un secreto para este cliente, la respuesta seria 401
    `unauthorized_client`, no 200."""
    app = create_app(_api_settings(database_url, panel_dist_dir=panel_dist_dir))
    try:
        async with app.router.lifespan_context(app), _panel_client(app) as client:
            registration = await _register_public_client(client)

            # `client_secret=""` (no ausente): `RevocationRequest` del SDK
            # exige la clave presente en el formulario (`str | None` sin
            # default) -- lo que se prueba es que el AS nunca exige un
            # secreto DE VERDAD para este cliente, no la forma exacta del
            # cuerpo. Sin cabecera `Authorization` ni secreto real.
            response = await client.post(
                "/revoke",
                data={
                    "token": "unknown-token-value",
                    "client_id": registration["client_id"],
                    "client_secret": "",
                },
            )

        assert response.status_code == 200, response.text
    finally:
        await app.state.container.aclose()

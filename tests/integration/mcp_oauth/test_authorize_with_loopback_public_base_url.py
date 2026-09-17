"""T049 (spec 008): reproduce contra Postgres real la secuencia exacta que
rompia con Codex -- `POST /register` de un cliente publico de bucle local
seguido de `GET /authorize` con `resource` (RFC 8707) explicito, contra un
`ADS_PUBLIC_BASE_URL` de bucle local (`http://localhost`, el que acepta
`composition/settings.py` desde antes de este lote y el README documenta
para `127.0.0.1:8410`).

Antes de 0054_mcp_oauth_loopback_resource, `StartAuthorization.execute()`
(`mcp_oauth/application/start_authorization.py`) construia el
`AuthorizationRequest` con el `resource` CANONICO -- `ResourceIndicator.
canonical(public_base_url)`, SIEMPRE `<public_base_url>/mcp` -- y el INSERT
resultante violaba `oauth_authorization_requests_resource_check`
(0035_mcp_oauth: `resource` exigia `https://` sin excepcion). El
`IntegrityError` crudo no lo capturaba ningun `except` de `SdkOAuthProvider.
_start()` (`presentation/sdk_provider.py`) y llegaba sin traducir al
catch-all de `sdk:handlers/authorize.py` -- 500 `server_error` opaco, para
CUALQUIER cliente (Claude Code incluido) contra ese mismo `ADS_PUBLIC_
BASE_URL`, no solo Codex."""

from __future__ import annotations

from collections.abc import AsyncIterator
from pathlib import Path

import httpx
import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import create_async_engine

from safent_ads.composition.app import create_app
from tests.unit.composition.factories import build_api_settings

pytestmark = pytest.mark.integration

_LOOPBACK_PUBLIC_BASE_URL = "http://localhost:8410"
_REDIRECT_URI = "http://127.0.0.1:40955/callback"
_CLIENT_NAME = "T049 Codex-like loopback client"
_CODE_CHALLENGE = "93OdWPNfuemquB8CSsjn44Hl_0R8Zs8UYMSULhh_tXc"


@pytest.fixture
def panel_dist_dir(tmp_path: Path) -> Path:
    dist_dir = tmp_path / "dist"
    dist_dir.mkdir()
    (dist_dir / "index.html").write_text("<html>panel</html>", encoding="utf-8")
    return dist_dir


@pytest.fixture(autouse=True)
async def _cleanup_seeded_clients(database_url: str) -> AsyncIterator[None]:
    yield
    engine = create_async_engine(database_url, pool_pre_ping=True)
    async with engine.begin() as connection:
        await connection.execute(
            text("DELETE FROM oauth_clients WHERE client_name = :name"), {"name": _CLIENT_NAME}
        )
    await engine.dispose()


def _panel_client(app: object) -> httpx.AsyncClient:
    return httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="https://testserver")


async def test_authorize_against_a_loopback_public_base_url_redirects_instead_of_500(
    database_url: str, panel_dist_dir: Path
) -> None:
    settings = build_api_settings(
        database_url=database_url,
        public_base_url=_LOOPBACK_PUBLIC_BASE_URL,
        mcp_oauth_enabled=True,
        panel_dist_dir=panel_dist_dir,
    )
    app = create_app(settings)
    try:
        async with app.router.lifespan_context(app), _panel_client(app) as client:
            registration = await client.post(
                "/register",
                json={
                    "client_name": _CLIENT_NAME,
                    "redirect_uris": [_REDIRECT_URI],
                    "grant_types": ["authorization_code", "refresh_token"],
                    "response_types": ["code"],
                    "token_endpoint_auth_method": "none",
                    "scope": "ads:read ads:propose",
                },
            )
            assert registration.status_code == 201, registration.text
            client_id = registration.json()["client_id"]

            authorize_response = await client.get(
                "/authorize",
                params={
                    "client_id": client_id,
                    "redirect_uri": _REDIRECT_URI,
                    "response_type": "code",
                    "code_challenge": _CODE_CHALLENGE,
                    "code_challenge_method": "S256",
                    "state": "codex-like-state",
                    "scope": "ads:read ads:propose",
                    # RFC 8707: Codex lo manda explicito (T049); el bug
                    # reproducia igual sin el, porque `resource` NUNCA se
                    # persistia tal cual (siempre el canonico).
                    "resource": f"{_LOOPBACK_PUBLIC_BASE_URL}/mcp",
                },
            )

            assert authorize_response.status_code == 302, authorize_response.text
            location = authorize_response.headers["location"]
            assert location.startswith(f"{_LOOPBACK_PUBLIC_BASE_URL}/oauth/autorizar?txn=")
    finally:
        await app.state.container.aclose()


async def test_authorize_still_works_when_ads_public_base_url_had_mixed_case(
    database_url: str, panel_dist_dir: Path
) -> None:
    """Revision de PR 44 (T049): `ADS_PUBLIC_BASE_URL=HTTP://LOCALHOST:8410`
    pasaba `_is_allowed_public_base_url_origin` (`urlsplit` ya compara en
    minuscula) pero, sin `_lowercase_scheme_and_host`, quedaba GUARDADO con
    las mayusculas originales -- `resource` heredaba esas mayusculas y el
    CHECK case-sensitive de 0054 volvia a reventar `/authorize`, esta vez
    con un `invalid_request` opaco en vez de un 500. `ApiSettings` ya
    normaliza a la entrada: esta prueba confirma que el efecto llega hasta
    la respuesta HTTP real, no solo hasta el valor de `settings`."""
    settings = build_api_settings(
        database_url=database_url,
        public_base_url="HTTP://LOCALHOST:8410",
        mcp_oauth_enabled=True,
        panel_dist_dir=panel_dist_dir,
    )
    assert settings.public_base_url == _LOOPBACK_PUBLIC_BASE_URL
    app = create_app(settings)
    try:
        async with app.router.lifespan_context(app), _panel_client(app) as client:
            registration = await client.post(
                "/register",
                json={
                    "client_name": _CLIENT_NAME,
                    "redirect_uris": [_REDIRECT_URI],
                    "grant_types": ["authorization_code", "refresh_token"],
                    "response_types": ["code"],
                    "token_endpoint_auth_method": "none",
                    "scope": "ads:read ads:propose",
                },
            )
            assert registration.status_code == 201, registration.text
            client_id = registration.json()["client_id"]

            authorize_response = await client.get(
                "/authorize",
                params={
                    "client_id": client_id,
                    "redirect_uri": _REDIRECT_URI,
                    "response_type": "code",
                    "code_challenge": _CODE_CHALLENGE,
                    "code_challenge_method": "S256",
                    "state": "mixed-case-state",
                    "scope": "ads:read ads:propose",
                    "resource": f"{_LOOPBACK_PUBLIC_BASE_URL}/mcp",
                },
            )

            assert authorize_response.status_code == 302, authorize_response.text
            location = authorize_response.headers["location"]
            assert location.startswith(f"{_LOOPBACK_PUBLIC_BASE_URL}/oauth/autorizar?txn=")
    finally:
        await app.state.container.aclose()

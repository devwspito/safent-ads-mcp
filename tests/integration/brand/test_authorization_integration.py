"""`brand.presentation.router.require_business_access` contra Postgres
real, sobre las 7 rutas montadas por `composition/app.py::create_app`
(mismo estado de cableado real que documenta `_build_brand_router`, salvo
`ingest_brand_from_website` que usa a proposito
`_NotYetWiredWebsiteBrandDiscovery` -- I/O de red pendiente de revision de
seguridad, ver su docstring).

Complementa `tests/unit/brand/presentation/test_router.py`, que prueba el
CRUD de cada caso de uso sobre repos en memoria sin tocar `iam`; este
archivo prueba justo la pieza que ese file deliberadamente evita: la
dependencia real (cookie de sesion -> `Owner` -> `SqlBusinessDirectory.
exists`), fail closed en su borde de existencia -- una sesion valida con
un `business_id` que no pertenece a nadie (404, threat-model.md C-27,
nunca 403 -- rest-api.md §"Seguridad transversal"). Mismo patron que
`tests/integration/panel/test_authorization_integration.py`.

Usa `httpx.ASGITransport` en vez del `TestClient` sincrono por el mismo
motivo que documenta ese archivo: `Container` abre conexiones `asyncpg`
atadas al bucle en el que se usan por primera vez, y `TestClient` corre en
su propio hilo/bucle."""

from __future__ import annotations

import hashlib
import uuid
from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta

import httpx
import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import create_async_engine

from safent_ads.composition.app import create_app
from safent_ads.composition.settings import ApiSettings
from safent_ads.iam.presentation.dependencies import SESSION_COOKIE_NAME

pytestmark = pytest.mark.integration

_RAW_TOKEN = "brand-integration-test-raw-session-token"  # noqa: S105 - fixture, no secreto real
_PNG_BYTES = b"\x89PNG\r\n\x1a\n" + b"\x00" * 32

# Cada ruta se ejercita con el metodo/cuerpo/query minimo que le hace falta
# para pasar validacion de forma si `require_business_access` no la
# bloqueara antes -- para que un 404 en este sweep se pueda atribuir sin
# ambiguedad a la comprobacion de existencia del negocio, nunca a un 422.
_JSON_ROUTE_CASES: list[tuple[str, str, dict[str, object] | None]] = [
    ("GET", "/api/v1/brand", None),
    ("GET", "/api/v1/brand/assets", None),
    ("POST", "/api/v1/brand/discover", {"url": "https://example-business.test"}),
    ("GET", "/api/v1/brand/draft", None),
    (
        "POST",
        "/api/v1/brand/confirm",
        {
            "primary_font": "Poppins",
            "font_licence_note": "Google Fonts, SIL OFL 1.1",
            "tone_description": "Cercano y claro.",
        },
    ),
    (
        "PUT",
        "/api/v1/brand/claims",
        {"claims_allowlist": [], "forbidden_claims": [], "legal_disclaimers": []},
    ),
]


def _api_settings(database_url: str) -> ApiSettings:
    return ApiSettings(
        database_url=database_url,
        session_secret="test-session-secret-0123456789abcdef",
        totp_enc_key="MDAwMDAwMDAwMDAwMDAwMDAwMDAwMDAwMDAwMDAwMDA=",
        mcp_token="test-mcp-token-abc123",
        broker_socket_path="/tmp/safent-ads-test/broker.sock",
        public_base_url="https://ads.test.ts.net",
        telegram_bot_token="123456:test-bot-token",
        telegram_owner_chat_ids=[111222333],
        approval_signing_key="MDAwMDAwMDAwMDAwMDAwMDAwMDAwMDAwMDAwMDAwMDA=",
        seat_authority_enabled=True,
        enterprise_origin="https://enterprise.test",
        enterprise_service_secret="a" * 64,
        enterprise_org_ids=frozenset({uuid.UUID("00000000-0000-0000-0000-000000000001")}),
    )


class _SeededOwner:
    def __init__(self, owner_id: uuid.UUID, business_id: uuid.UUID) -> None:
        self.owner_id = owner_id
        self.business_id = business_id


@pytest.fixture
async def seeded_owner(database_url: str) -> AsyncIterator[_SeededOwner]:
    """Escribe de verdad (commit real, no savepoint): `Container` abre su
    propio motor/conexion independiente del de este fixture, asi que la
    fila tiene que estar confirmada en la base para que la vea."""
    engine = create_async_engine(database_url, pool_pre_ping=True)
    owner_id = uuid.uuid4()
    business_id = uuid.uuid4()
    session_id = uuid.uuid4()
    now = datetime.now(UTC)
    async with engine.begin() as connection:
        await connection.execute(
            text(
                "INSERT INTO owners (id, email, password_hash) "
                "VALUES (:id, :email, :password_hash)"
            ),
            {
                "id": str(owner_id),
                "email": f"owner-{owner_id.hex[:8]}@safent.example",
                "password_hash": "argon2id$fixture$not-a-real-hash",
            },
        )
        await connection.execute(
            text(
                "INSERT INTO businesses "
                "(id, slug, name, timezone, reference_currency) "
                "VALUES (:id, :slug, :name, 'Europe/Madrid', 'EUR')"
            ),
            {
                "id": str(business_id),
                "slug": f"fixture-{business_id.hex[:8]}",
                "name": "Fixture Business",
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
                "token_hash": hashlib.sha256(_RAW_TOKEN.encode("utf-8")).hexdigest(),
                "created_at": now,
                "expires_at": now + timedelta(hours=1),
            },
        )
    try:
        yield _SeededOwner(owner_id, business_id)
    finally:
        async with engine.begin() as connection:
            await connection.execute(
                text("DELETE FROM sessions WHERE id = :id"), {"id": str(session_id)}
            )
            await connection.execute(
                text("DELETE FROM businesses WHERE id = :id"), {"id": str(business_id)}
            )
            await connection.execute(
                text("DELETE FROM owners WHERE id = :id"), {"id": str(owner_id)}
            )
        await engine.dispose()


async def test_idor_sweep_foreign_business_id_is_404_never_403(
    seeded_owner: _SeededOwner, database_url: str
) -> None:
    """threat-model.md C-27: cada una de las rutas de `brand`, pedida con
    una sesion valida pero un `business_id` que no pertenece a nadie,
    responde 404 (nunca 200, nunca 403)."""
    foreign_business_id = str(uuid.uuid4())
    app = create_app(_api_settings(database_url))
    transport = httpx.ASGITransport(app=app)
    try:
        async with httpx.AsyncClient(
            transport=transport,
            base_url="https://test",
            cookies={SESSION_COOKIE_NAME: _RAW_TOKEN},
        ) as client:
            await client.get("/api/v1/health")
            client.headers["X-CSRF-Token"] = client.cookies["ads_csrf"]

            for method, path, body in _JSON_ROUTE_CASES:
                response = await client.request(
                    method, path, params={"business_id": foreign_business_id}, json=body
                )
                assert response.status_code == 404, f"{method} {path} -> {response.status_code}"

            upload_response = await client.post(
                "/api/v1/brand/assets",
                params={"business_id": foreign_business_id, "kind": "logo_raster"},
                files={"file": ("logo.png", _PNG_BYTES, "image/png")},
            )
            assert upload_response.status_code == 404, (
                f"POST /api/v1/brand/assets -> {upload_response.status_code}"
            )

            preview_response = await client.get(
                "/api/v1/brand/assets/01ARZ3NDEKTSV4RRFFQ69G5FAV/preview",
                params={"business_id": foreign_business_id},
            )
            assert preview_response.status_code == 404, (
                "GET /api/v1/brand/assets/{asset_id}/preview -> "
                f"{preview_response.status_code}"
            )
    finally:
        await app.state.container.aclose()


async def test_missing_session_cookie_is_401(seeded_owner: _SeededOwner, database_url: str) -> None:
    app = create_app(_api_settings(database_url))
    transport = httpx.ASGITransport(app=app)
    try:
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
            response = await client.get(
                "/api/v1/brand", params={"business_id": str(seeded_owner.business_id)}
            )
        assert response.status_code == 401
    finally:
        await app.state.container.aclose()

"""`panel.presentation.deps.require_business_access` contra Postgres real.

Complementa `tests/unit/panel/presentation/test_rest.py::test_idor_sweep`,
que prueba el aislamiento entre negocios inyectando un caller restringido
via `app.dependency_overrides` sin tocar la base. Este archivo prueba
justo la pieza que ese test deliberadamente evita: la dependencia real
(cookie de sesion -> `Owner` -> `SqlBusinessDirectory.exists`), fail
closed en sus dos bordes -- sin sesion (401) y con un `business_id` que no
existe (404).

Modelo de propietario unico (data-model.md,
`iam.infrastructure.sql_business_directory`): no hay tabla de asignacion
propietario-negocio, "poseer" un negocio es que la fila exista. Por eso se
siembra UN propietario con DOS negocios (via `owner_factory`/
`business_factory`) en vez de dos propietarios -- con el modelo actual no
hay forma de que un segundo propietario exista sin ampliar el esquema
(fuera del alcance de esta lane de cableado).

Usa `httpx.ASGITransport` en vez del `TestClient` sincrono: este ultimo
gestiona su propio hilo/bucle de eventos (anyio "blocking portal"), y
`Container` abre conexiones `asyncpg` -- que quedan atadas al bucle en el
que se usan por primera vez (mismo motivo que documenta
`tests.conftest.rolled_back_session`). Mezclar ambos bucles revienta al
cerrar el motor. `ASGITransport` corre la app en el mismo bucle que el
test asincrono, sin ese problema."""

from __future__ import annotations

import hashlib
import uuid
from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta

import httpx
import pytest
from fastapi import FastAPI
from sqlalchemy import text
from sqlalchemy.ext.asyncio import create_async_engine

from safent_ads.composition.container import Container
from safent_ads.composition.settings import ApiSettings
from safent_ads.iam.presentation.dependencies import SESSION_COOKIE_NAME
from safent_ads.panel.presentation.rest import BusinessIdDep

pytestmark = pytest.mark.integration

_RAW_TOKEN = "integration-test-raw-session-token"  # noqa: S105 - fixture, no secreto real


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
    def __init__(self, owner_id: uuid.UUID, business_ids: tuple[uuid.UUID, ...]) -> None:
        self.owner_id = owner_id
        self.business_ids = business_ids


@pytest.fixture
async def seeded_owner(database_url: str) -> AsyncIterator[_SeededOwner]:
    """Escribe de verdad (commit real, no savepoint): `Container` abre su
    propio motor/conexion independiente del de este fixture, asi que la
    fila tiene que estar confirmada en la base para que la vea."""
    engine = create_async_engine(database_url, pool_pre_ping=True)
    owner_id = uuid.uuid4()
    business_ids = (uuid.uuid4(), uuid.uuid4())
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
        for business_id in business_ids:
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
        yield _SeededOwner(owner_id, business_ids)
    finally:
        async with engine.begin() as connection:
            await connection.execute(
                text("DELETE FROM sessions WHERE id = :id"), {"id": str(session_id)}
            )
            for business_id in business_ids:
                await connection.execute(
                    text("DELETE FROM businesses WHERE id = :id"), {"id": str(business_id)}
                )
            await connection.execute(
                text("DELETE FROM owners WHERE id = :id"), {"id": str(owner_id)}
            )
        await engine.dispose()


def _build_app(container: Container) -> FastAPI:
    app = FastAPI()
    app.state.container = container

    @app.get("/protected")
    async def protected(business_id: BusinessIdDep) -> dict[str, str]:
        return {"business_id": business_id}

    return app


async def test_authenticated_owner_reaches_any_of_its_seeded_businesses(
    seeded_owner: _SeededOwner, database_url: str
) -> None:
    container = Container.build(_api_settings(database_url))
    try:
        app = _build_app(container)
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(
            transport=transport, base_url="http://test", cookies={SESSION_COOKIE_NAME: _RAW_TOKEN}
        ) as client:
            for business_id in seeded_owner.business_ids:
                response = await client.get(
                    "/protected", params={"business_id": str(business_id)}
                )
                assert response.status_code == 200, response.text
                assert response.json() == {"business_id": str(business_id)}
    finally:
        await container.aclose()


async def test_nonexistent_business_id_is_404_never_403(
    seeded_owner: _SeededOwner, database_url: str
) -> None:
    container = Container.build(_api_settings(database_url))
    try:
        app = _build_app(container)
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(
            transport=transport, base_url="http://test", cookies={SESSION_COOKIE_NAME: _RAW_TOKEN}
        ) as client:
            response = await client.get(
                "/protected", params={"business_id": str(uuid.uuid4())}
            )
        assert response.status_code == 404
    finally:
        await container.aclose()


async def test_missing_session_cookie_is_401(
    seeded_owner: _SeededOwner, database_url: str
) -> None:
    container = Container.build(_api_settings(database_url))
    try:
        app = _build_app(container)
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
            response = await client.get(
                "/protected", params={"business_id": str(seeded_owner.business_ids[0])}
            )
        assert response.status_code == 401
    finally:
        await container.aclose()

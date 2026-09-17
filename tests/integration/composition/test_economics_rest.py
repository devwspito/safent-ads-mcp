"""`GET /api/v1/economics/unit-economics` a traves del router real montado
sobre Postgres (composition/economics_rest.py): sin perfil sembrado, prueba
que la cadena completa (cookie -> `require_business_access` ->
`EconomicsQueryService` -> `SqlUnitEconomicsProfileRepository` -> Postgres)
responde 404 tipado en vez de 500 -- la prueba de que el adaptador SQL real
esta conectado, no uno en memoria devolviendo silenciosamente `None` sin
tocar la base."""

from __future__ import annotations

import hashlib
import uuid
from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta

import httpx
import pytest
from fastapi import FastAPI
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine
from tests.contracts.sql_fixtures import campaign_ref, seed_entity
from tests.unit.composition.factories import build_api_settings

from safent_ads.composition.container import Container
from safent_ads.composition.economics_rest import build_economics_read_router
from safent_ads.iam.presentation.dependencies import SESSION_COOKIE_NAME

pytestmark = pytest.mark.integration

_RAW_TOKEN = "integration-test-economics-rest-token"  # noqa: S105 - fixture, no secreto real


class _Seeded:
    def __init__(self, business_id: uuid.UUID) -> None:
        self.business_id = business_id


@pytest.fixture
async def seeded_business(isolated_database_url: str) -> AsyncIterator[_Seeded]:
    engine = create_async_engine(isolated_database_url, pool_pre_ping=True)
    owner_id = uuid.uuid4()
    session_id = uuid.uuid4()
    now = datetime.now(UTC)
    entity_ref = campaign_ref(f"c-{uuid.uuid4().hex[:10]}-eco")

    async with AsyncSession(engine, expire_on_commit=False) as session:
        business_id = await seed_entity(session, entity_ref)
        await session.execute(
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
        await session.execute(
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
        await session.commit()
    try:
        yield _Seeded(business_id)
    finally:
        # Mismo razonamiento que `test_execution_rest.py`: `businesses` no es
        # solo-anexable, pero esta base (`ads_isolated`) se recrea entera por
        # sesion de pytest, asi que basta con soltar la sesion de login.
        async with engine.begin() as connection:
            await connection.execute(
                text("DELETE FROM sessions WHERE id = :id"), {"id": str(session_id)}
            )
        await engine.dispose()


async def test_unit_economics_round_trips_through_the_real_sql_adapter(
    seeded_business: _Seeded, isolated_database_url: str
) -> None:
    settings = build_api_settings(
        database_url=isolated_database_url,
        broker_socket_path="/tmp/safent-ads-wiring-b-test/economics-rest.sock",
    )
    container = Container.build(settings)
    try:
        app = FastAPI()
        app.state.container = container
        app.include_router(build_economics_read_router(container.session_factory))
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(
            transport=transport,
            base_url="http://test",
            cookies={SESSION_COOKIE_NAME: _RAW_TOKEN},
        ) as client:
            response = await client.get(
                "/api/v1/economics/unit-economics",
                params={
                    "business_id": str(seeded_business.business_id),
                    "product_id": str(uuid.uuid4()),
                },
            )

        # 404 tipado (`UnitEconomicsProfileNotFoundError` -> HTTPException),
        # no un 500: sin esto, un adaptador que nunca llego a Postgres
        # (bug de cableado) y uno que si llego y no encontro nada serian
        # indistinguibles desde fuera.
        assert response.status_code == 404, response.text
    finally:
        await container.aclose()

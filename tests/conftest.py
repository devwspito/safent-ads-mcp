"""Fixtures compartidas. Los tests nunca dependen de un `.env` real: toda
`Settings` se construye explicitamente con valores dummy.

`postgres_container`/`db_session` (tasks.md T017): un unico contenedor
Postgres por sesion de pytest, migrado una vez; cada test recibe su propia
transaccion que siempre se deshace al terminar -- nunca hay que recrear el
esquema entre tests ni un test puede dejar basura para el siguiente.
`OwnerFactory`/`BusinessFactory` insertan filas minimas validas contra ese
mismo Postgres real: "los dobles no modelan CHECKs/UNIQUEs/triggers"
(data-model.md), asi que las factories tambien pasan por SQL de verdad.

Los ayudantes de Alembic (`alembic_upgrade`/`alembic_downgrade`) y las
conversiones de DSN viven aqui porque los usan tanto el contenedor
compartido como los tests de migracion (ida y vuelta head -> base -> head).
Migran en un hilo propio: `alembic/env.py` usa `asyncio.run`, que estalla si
lo llama un test asincrono con su bucle ya corriendo."""

from __future__ import annotations

import asyncio
import os
import uuid
from collections.abc import AsyncIterator, Callable, Iterator
from concurrent.futures import ThreadPoolExecutor
from contextlib import asynccontextmanager
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

import asyncpg
import pytest
from alembic import command
from alembic.config import Config
from alembic.script import ScriptDirectory
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine
from testcontainers.community.postgres import PostgresContainer

from safent_ads.composition.settings import ApiSettings
from safent_ads.shared.clock import FixedClock

_REPO_ROOT = Path(__file__).resolve().parents[1]

# 32 bytes de ceros en base64: `AesGcmTotpCipher`/`ApprovalSigner` exigen
# material de clave de longitud valida incluso en pruebas (threat-model.md
# C-24: fallo alto en el arranque, no en el primer uso).
_VALID_32_BYTE_KEY_B64 = "MDAwMDAwMDAwMDAwMDAwMDAwMDAwMDAwMDAwMDAwMDA="


@pytest.fixture
def api_settings() -> ApiSettings:
    return ApiSettings(
        database_url="postgresql+asyncpg://ads:test@localhost:5432/ads_test",
        session_secret="test-session-secret-0123456789abcdef",
        totp_enc_key=_VALID_32_BYTE_KEY_B64,
        mcp_token="test-mcp-token-abc123",
        broker_socket_path="/tmp/safent-ads-test/broker.sock",
        public_base_url="https://ads.test.ts.net",
        telegram_bot_token="123456:test-bot-token",
        telegram_owner_chat_ids=[111222333],
        approval_signing_key=_VALID_32_BYTE_KEY_B64,
        seat_authority_enabled=True,
        enterprise_origin="https://enterprise.test",
        enterprise_service_secret="a" * 64,
        enterprise_org_ids=frozenset({uuid.UUID("00000000-0000-0000-0000-000000000001")}),
    )


@pytest.fixture
def fake_clock() -> FixedClock:
    return FixedClock(datetime(2026, 9, 9, 12, 0, tzinfo=UTC))


_PSYCOPG_SCHEME = "postgresql+psycopg2://"


def to_alembic_dsn(container_url: str) -> str:
    """DSN de SQLAlchemy asincrono, el que entienden Alembic y el motor."""
    return container_url.replace(_PSYCOPG_SCHEME, "postgresql+asyncpg://")


def to_asyncpg_dsn(container_url: str) -> str:
    """DSN pelado, el que entiende `asyncpg.connect` sin SQLAlchemy."""
    return container_url.replace(_PSYCOPG_SCHEME, "postgresql://")


def with_database(dsn: str, database: str) -> str:
    return f"{dsn.rsplit('/', 1)[0]}/{database}"


def _alembic_config(database_url: str) -> Config:
    config = Config(str(_REPO_ROOT / "alembic.ini"))
    config.set_main_option("script_location", str(_REPO_ROOT / "alembic"))
    os.environ["ADS_DATABASE_URL"] = database_url
    return config


def _in_worker_thread(action: Callable[[], None]) -> None:
    with ThreadPoolExecutor(max_workers=1) as executor:
        executor.submit(action).result()


def alembic_upgrade(database_url: str, target: str = "head") -> None:
    _in_worker_thread(lambda: command.upgrade(_alembic_config(database_url), target))


def alembic_downgrade(database_url: str, target: str = "base") -> None:
    _in_worker_thread(lambda: command.downgrade(_alembic_config(database_url), target))


def alembic_head_revision() -> str:
    """Nit de la revision de seguridad (16-sep): la revision HEAD real
    segun los ficheros de migracion en disco -- nunca un literal copiado a
    mano en un test (`test_customers_revenue.py`), que se queda corto en
    silencio cada vez que se añade una migracion nueva por encima."""
    config = Config(str(_REPO_ROOT / "alembic.ini"))
    config.set_main_option("script_location", str(_REPO_ROOT / "alembic"))
    head = ScriptDirectory.from_config(config).get_current_head()
    assert head is not None, "no hay ninguna migracion en alembic/versions/"
    return head


@pytest.fixture(scope="session")
def postgres_container() -> Iterator[PostgresContainer]:
    """Un unico Postgres para toda la sesion de pytest, migrado una vez."""
    with PostgresContainer("postgres:16-alpine") as container:
        alembic_upgrade(to_alembic_dsn(container.get_connection_url()))
        yield container


@pytest.fixture(scope="session")
def database_url(postgres_container: PostgresContainer) -> str:
    return to_alembic_dsn(postgres_container.get_connection_url())


# Base aparte, migrada y sin filas de nadie mas. La piden los casos cuya
# consulta es GLOBAL por diseno: el reclamo de la cola de ejecuciones
# (`SELECT ... FOR UPDATE SKIP LOCKED` sin filtro de negocio) y las sumas de
# los topes de gasto. Sobre la base compartida, una fila que dejo otro test
# cambiaria su resultado sin que el codigo tenga nada que ver.
_ISOLATED_DATABASE = "ads_isolated"


@pytest.fixture(scope="session")
def isolated_database_url(postgres_container: PostgresContainer) -> str:
    base_url = postgres_container.get_connection_url()
    _recreate_database(base_url, _ISOLATED_DATABASE)
    isolated_url = with_database(to_alembic_dsn(base_url), _ISOLATED_DATABASE)
    alembic_upgrade(isolated_url)
    return isolated_url


def _recreate_database(container_url: str, name: str) -> None:
    """`CREATE DATABASE` no cabe en una transaccion, asi que va por asyncpg
    en crudo (autocommit) y en un hilo propio, como las migraciones: el
    fixture es sincrono y no debe tocar el bucle de eventos de los tests."""

    async def recreate() -> None:
        connection = await asyncpg.connect(to_asyncpg_dsn(container_url))
        try:
            await connection.execute(f'DROP DATABASE IF EXISTS "{name}"')
            await connection.execute(f'CREATE DATABASE "{name}"')
        finally:
            await connection.close()

    _in_worker_thread(lambda: asyncio.run(recreate()))


@asynccontextmanager
async def rolled_back_session(database_url: str) -> AsyncIterator[AsyncSession]:
    """Motor por test, no por sesion de pytest: `pytest-asyncio` crea un
    event loop nuevo por funcion de test por defecto, y un `AsyncEngine`
    (con su pool de conexiones asyncpg) queda atado al loop en el que
    nace -- reusarlo entre tests revienta con
    "Future attached to a different loop"."""
    engine = create_async_engine(database_url, pool_pre_ping=True)
    connection = await engine.connect()
    await connection.begin()
    session = AsyncSession(
        bind=connection, expire_on_commit=False, join_transaction_mode="create_savepoint"
    )
    try:
        yield session
    finally:
        await session.close()
        await connection.rollback()
        await connection.close()
        await engine.dispose()


@pytest.fixture
async def db_session(database_url: str) -> AsyncIterator[AsyncSession]:
    async with rolled_back_session(database_url) as session:
        yield session


@dataclass(frozen=True, slots=True)
class OwnerFactory:
    session: AsyncSession

    async def create(
        self,
        *,
        email: str | None = None,
        password_hash: str = "argon2id$fixture$not-a-real-hash",  # noqa: S107
        totp_secret_encrypted: bytes | None = None,
        totp_confirmed_at: datetime | None = None,
    ) -> uuid.UUID:
        owner_id = uuid.uuid4()
        resolved_email = email or f"owner-{owner_id.hex[:8]}@safent.example"
        await self.session.execute(
            text(
                "INSERT INTO owners "
                "(id, email, password_hash, totp_secret_encrypted, totp_confirmed_at) "
                "VALUES (:id, :email, :password_hash, :totp_secret_encrypted, "
                ":totp_confirmed_at)"
            ),
            {
                "id": str(owner_id),
                "email": resolved_email,
                "password_hash": password_hash,
                "totp_secret_encrypted": totp_secret_encrypted,
                "totp_confirmed_at": totp_confirmed_at,
            },
        )
        return owner_id


@pytest.fixture
def owner_factory(db_session: AsyncSession) -> OwnerFactory:
    return OwnerFactory(db_session)


@dataclass(frozen=True, slots=True)
class BusinessFactory:
    session: AsyncSession

    async def create(
        self,
        *,
        slug: str | None = None,
        name: str = "Fixture Business",
        timezone: str = "Europe/Madrid",
        reference_currency: str = "EUR",
        digest_hour: int | None = None,
    ) -> uuid.UUID:
        business_id = uuid.uuid4()
        resolved_slug = slug or f"fixture-business-{business_id.hex[:8]}"
        await self.session.execute(
            text(
                "INSERT INTO businesses "
                "(id, slug, name, timezone, reference_currency, digest_hour) "
                "VALUES (:id, :slug, :name, :timezone, :reference_currency, :digest_hour)"
            ),
            {
                "id": str(business_id),
                "slug": resolved_slug,
                "name": name,
                "timezone": timezone,
                "reference_currency": reference_currency,
                "digest_hour": digest_hour,
            },
        )
        return business_id


@pytest.fixture
def business_factory(db_session: AsyncSession) -> BusinessFactory:
    return BusinessFactory(db_session)

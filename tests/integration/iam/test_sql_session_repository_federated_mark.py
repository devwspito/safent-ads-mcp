"""`SqlSessionRepository` (T064 security review, re-verificacion de
C-82): `save()` genérico -- el que usa `current_owner` en cada petición
para deslizar `expires_at`, y `logout.py` para revocar -- ya no puede
resucitar `last_federated_auth_at`. Antes escribía `GREATEST(columna,
:valor)`; una petición concurrente que hubiese leído la sesión ANTES de
que una revocación (`grants_router.py`) invalidara la marca, y que
guardase después con la copia en memoria todavía fresca, volvía a dejarla
fresca -- exactamente lo que C-82 pretendía impedir. `save_federated_mark`
es ahora la única vía de escritura de esa columna."""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncConnection, AsyncSession, create_async_engine

from safent_ads.iam.domain.session import Session, SessionOrigin
from safent_ads.iam.infrastructure.sql_session_repository import SqlSessionRepository

pytestmark = pytest.mark.integration


async def _seed_owner(connection: AsyncConnection) -> uuid.UUID:
    owner_id = uuid.uuid4()
    await connection.execute(
        text("INSERT INTO owners (id, email, password_hash) VALUES (:id, :email, :password_hash)"),
        {
            "id": str(owner_id),
            "email": f"session-repo-test-{owner_id.hex[:8]}@safent.example",
            "password_hash": "argon2id$fixture$not-a-real-hash",  # noqa: S106
        },
    )
    return owner_id


async def _cleanup(database_url: str, owner_id: uuid.UUID) -> None:
    engine = create_async_engine(database_url, pool_pre_ping=True)
    async with engine.begin() as connection:
        await connection.execute(
            text("DELETE FROM sessions WHERE owner_id = :id"), {"id": str(owner_id)}
        )
        await connection.execute(text("DELETE FROM owners WHERE id = :id"), {"id": str(owner_id)})
    await engine.dispose()


async def test_a_generic_save_never_resurrects_an_invalidated_federated_mark(
    database_url: str,
) -> None:
    engine = create_async_engine(database_url, pool_pre_ping=True)
    now = datetime.now(UTC)
    fresh_mark = now
    async with engine.begin() as connection:
        owner_id = await _seed_owner(connection)

    try:
        # Password-origin session with a fresh mark (decision 7: refreshed
        # via Google at some point).
        async with AsyncSession(engine, expire_on_commit=False) as session:
            domain_session = Session(
                session_id=uuid.uuid4(),
                owner_id=owner_id,
                token_hash=f"session-repo-test-{uuid.uuid4().hex}",
                created_at=now - timedelta(hours=1),
                expires_at=now + timedelta(hours=1),
                revoked_at=None,
                origin=SessionOrigin.PASSWORD,
                last_federated_auth_at=fresh_mark,
            )
            await SqlSessionRepository(session).create(domain_session)
            await session.commit()

        # Concurrent request A reads the session while the mark is still
        # fresh (a stale in-memory copy it will save back later).
        async with AsyncSession(engine, expire_on_commit=False) as session:
            stale_in_memory_copy = await SqlSessionRepository(session).get_by_id(domain_session.id)
        assert stale_in_memory_copy is not None
        assert stale_in_memory_copy.last_federated_auth_at == fresh_mark

        # Meanwhile, request B (a revocation, C-82) invalidates the mark.
        async with engine.begin() as connection:
            await connection.execute(
                text("UPDATE sessions SET last_federated_auth_at = NULL WHERE id = :id"),
                {"id": str(domain_session.id)},
            )

        # Request A finishes and persists (e.g. current_owner's touch()):
        # its stale in-memory copy still carries the fresh mark.
        async with AsyncSession(engine, expire_on_commit=False) as session:
            await SqlSessionRepository(session).save(stale_in_memory_copy)
            await session.commit()

        async with engine.begin() as connection:
            mark_after = (
                await connection.execute(
                    text("SELECT last_federated_auth_at FROM sessions WHERE id = :id"),
                    {"id": str(domain_session.id)},
                )
            ).scalar_one()
        assert mark_after is None, "save() resucito la marca invalidada"
    finally:
        await _cleanup(database_url, owner_id)
        await engine.dispose()


async def test_save_federated_mark_is_the_only_writer_and_only_advances(
    database_url: str,
) -> None:
    engine = create_async_engine(database_url, pool_pre_ping=True)
    now = datetime.now(UTC)
    async with engine.begin() as connection:
        owner_id = await _seed_owner(connection)

    try:
        domain_session = Session(
            session_id=uuid.uuid4(),
            owner_id=owner_id,
            token_hash=f"session-repo-test-{uuid.uuid4().hex}",
            created_at=now - timedelta(hours=1),
            expires_at=now + timedelta(hours=1),
            revoked_at=None,
            origin=SessionOrigin.FEDERATED,
            last_federated_auth_at=now,
        )
        async with AsyncSession(engine, expire_on_commit=False) as session:
            await SqlSessionRepository(session).create(domain_session)
            await session.commit()

        async with AsyncSession(engine, expire_on_commit=False) as session:
            await SqlSessionRepository(session).save_federated_mark(domain_session.id, now)
            await session.commit()

        async with engine.begin() as connection:
            mark_after = (
                await connection.execute(
                    text("SELECT last_federated_auth_at FROM sessions WHERE id = :id"),
                    {"id": str(domain_session.id)},
                )
            ).scalar_one()
        assert abs((mark_after - now).total_seconds()) < 1

        # An older write (e.g. a stray retry) never moves it backwards.
        async with AsyncSession(engine, expire_on_commit=False) as session:
            await SqlSessionRepository(session).save_federated_mark(
                domain_session.id, now - timedelta(days=1)
            )
            await session.commit()

        async with engine.begin() as connection:
            mark_unchanged = (
                await connection.execute(
                    text("SELECT last_federated_auth_at FROM sessions WHERE id = :id"),
                    {"id": str(domain_session.id)},
                )
            ).scalar_one()
        assert abs((mark_unchanged - now).total_seconds()) < 1
    finally:
        await _cleanup(database_url, owner_id)
        await engine.dispose()

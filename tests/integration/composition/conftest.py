"""Fixtures compartidas de `tests/integration/composition/`: sesion real de
`iam` (cookie -> `Session` -> `Owner`) para las pruebas de extremo a extremo
que montan un router real sobre `Container` (mismo patron que
`tests/integration/composition/test_execution_rest.py`/
`test_idor_sweep_new_routes.py`, extraido aqui para no repetirlo por
tercera vez)."""

from __future__ import annotations

import hashlib
import uuid
from collections.abc import AsyncIterator
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import create_async_engine

from safent_ads.iam.presentation.dependencies import SESSION_COOKIE_NAME

RAW_SESSION_TOKEN = "integration-test-composition-token"  # noqa: S105 - fixture, no secreto real


@dataclass(frozen=True, slots=True)
class AuthenticatedSession:
    owner_id: uuid.UUID
    cookies: dict[str, str]


@pytest.fixture
async def authenticated_session(isolated_database_url: str) -> AsyncIterator[AuthenticatedSession]:
    engine = create_async_engine(isolated_database_url, pool_pre_ping=True)
    owner_id = uuid.uuid4()
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
                "password_hash": "argon2id$fixture$not-a-real-hash",  # noqa: S106
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
                "token_hash": hashlib.sha256(RAW_SESSION_TOKEN.encode("utf-8")).hexdigest(),
                "created_at": now,
                "expires_at": now + timedelta(hours=1),
            },
        )
    try:
        yield AuthenticatedSession(
            owner_id=owner_id, cookies={SESSION_COOKIE_NAME: RAW_SESSION_TOKEN}
        )
    finally:
        async with engine.begin() as connection:
            await connection.execute(
                text("DELETE FROM sessions WHERE id = :id"), {"id": str(session_id)}
            )
        await engine.dispose()

"""`audit_client_redirect_uris` contra Postgres real (D-11, threat-model.md
C-70 pieza 4): una fila ANTIGUA con destino remoto -- la que el dominio ya
no sabe construir, asi que solo se puede sembrar con SQL crudo, igual que
la dejo una version anterior del motor -- aparece en el aviso de arranque
por su `client_id`, y la fila sigue exactamente donde estaba."""

from __future__ import annotations

import json
from collections.abc import AsyncIterator, Iterator
from datetime import UTC, datetime

import pytest
import structlog
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from safent_ads.mcp_oauth.infrastructure import audit_redirect_uris as audit_module
from safent_ads.mcp_oauth.infrastructure.audit_redirect_uris import audit_client_redirect_uris

pytestmark = pytest.mark.integration

_NOW = datetime(2026, 6, 1, 12, 0, tzinfo=UTC)
_CLIENT_ID_PREFIX = "client-audit-test-"
_EVENT = "mcp_oauth_non_loopback_client_registrations"

_INSERT_SQL = text("""
    INSERT INTO oauth_clients
        (client_id, client_name, redirect_uris, token_endpoint_auth_method,
         client_secret_hash, grant_types, requested_scopes, created_at, last_seen_at)
    VALUES
        (:client_id, :client_name, CAST(:redirect_uris AS jsonb), 'none',
         NULL, CAST(:grant_types AS jsonb), 'ads:read', :created_at, :created_at)
""")


@pytest.fixture
def captured_logs(monkeypatch: pytest.MonkeyPatch) -> Iterator[list[dict[str, object]]]:
    """Rebindear el `logger` del modulo DENTRO de `capture_logs()`: el
    proxy ya esta atado a la configuracion global que dejo el ultimo
    `create_app()` de la sesion (mismo truco que
    `tests/unit/composition/test_mcp_static_token_warning.py`)."""
    with structlog.testing.capture_logs() as logs:
        monkeypatch.setattr(audit_module, "logger", structlog.get_logger())
        yield logs


@pytest.fixture
async def session_factory(database_url: str) -> AsyncIterator[async_sessionmaker[AsyncSession]]:
    engine = create_async_engine(database_url, pool_pre_ping=True)
    try:
        yield async_sessionmaker(engine, expire_on_commit=False)
    finally:
        await engine.dispose()


@pytest.fixture(autouse=True)
async def _cleanup_seeded_rows(database_url: str) -> AsyncIterator[None]:
    yield
    engine = create_async_engine(database_url, pool_pre_ping=True)
    async with engine.begin() as connection:
        await connection.execute(
            text("DELETE FROM oauth_clients WHERE client_id LIKE :prefix"),
            {"prefix": f"{_CLIENT_ID_PREFIX}%"},
        )
    await engine.dispose()


async def _seed(
    session_factory: async_sessionmaker[AsyncSession], *, client_id: str, redirect_uri: str
) -> None:
    async with session_factory() as session:
        await session.execute(
            _INSERT_SQL,
            {
                "client_id": client_id,
                "client_name": "Agente heredado",
                "redirect_uris": json.dumps([redirect_uri]),
                "grant_types": json.dumps(["authorization_code"]),
                "created_at": _NOW,
            },
        )
        await session.commit()


async def test_a_legacy_remote_registration_is_reported_and_left_untouched(
    session_factory: async_sessionmaker[AsyncSession],
    captured_logs: list[dict[str, object]],
) -> None:
    remote = f"{_CLIENT_ID_PREFIX}remote"
    local = f"{_CLIENT_ID_PREFIX}local"
    await _seed(session_factory, client_id=remote, redirect_uri="https://agent.example/callback")
    await _seed(session_factory, client_id=local, redirect_uri="http://127.0.0.1:54321/callback")

    offenders = await audit_client_redirect_uris(session_factory)

    assert remote in offenders
    assert local not in offenders
    assert any(entry["event"] == _EVENT for entry in captured_logs)
    async with session_factory() as session:
        survivors = (
            await session.execute(
                text("SELECT client_id FROM oauth_clients WHERE client_id LIKE :prefix"),
                {"prefix": f"{_CLIENT_ID_PREFIX}%"},
            )
        ).scalars()
        assert set(survivors) == {remote, local}

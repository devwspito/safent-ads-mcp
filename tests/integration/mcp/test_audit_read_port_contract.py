"""`AuditReadPort`: `FakeAuditReadPort` (`mcp/testing/fakes.py`) contra
`SqlAuditReadPort` (esta lane, envoltorio de `SqlDecisionLogRepository` --
`audit/infrastructure/sql_repository.py`). `get_decision_log_entry` con
`seq` desconocido es SQL-only: el fake nunca lanza `EntityNotFoundError`
para `decision_log` (mismo hueco documentado en
`test_catalog_read_port_contract.py`)."""

from __future__ import annotations

import uuid
from collections.abc import AsyncIterator
from dataclasses import dataclass
from datetime import UTC, datetime

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from safent_ads.audit.application.ports import DecisionLogRepository
from safent_ads.audit.domain.entry import ActorKind, DecisionKind, PendingDecision
from safent_ads.audit.infrastructure.sql_repository import SqlDecisionLogRepository
from safent_ads.mcp.application.errors import EntityNotFoundError
from safent_ads.mcp.infrastructure.sql_audit_read_port import SqlAuditReadPort
from safent_ads.mcp.testing.fakes import BUSINESS_A, FakeAuditReadPort
from safent_ads.shared.ids import BusinessId

_SINCE = datetime(2026, 1, 1, tzinfo=UTC)
_UNTIL = datetime(2026, 12, 31, tzinfo=UTC)


@dataclass(slots=True)
class AuditFixture:
    port: object
    business_id: str
    seq: int


async def _seed_sql_audit(factory: async_sessionmaker[AsyncSession]) -> tuple[uuid.UUID, int]:
    business_id = uuid.uuid4()
    async with factory() as session:
        await session.execute(
            text(
                "INSERT INTO businesses (id, slug, name, timezone, reference_currency) "
                "VALUES (:id, :slug, 'Negocio de auditoria', 'Europe/Madrid', 'EUR')"
            ),
            {"id": business_id, "slug": f"aud-{business_id.hex[:8]}"},
        )
        repo: DecisionLogRepository = SqlDecisionLogRepository(session)
        entry = await repo.append(
            PendingDecision(
                business_id=BusinessId(business_id),
                kind=DecisionKind.SIGNAL,
                actor_kind=ActorKind.RULE_ENGINE,
                payload={"cause_key": "cpl_over_target", "strength": 78},
            )
        )
        await session.commit()
    return business_id, entry.seq


async def _cleanup_sql_audit(
    factory: async_sessionmaker[AsyncSession], business_id: uuid.UUID
) -> None:
    """`decision_log` es solo-anexable (0002_audit_chain: un trigger rechaza
    cualquier `DELETE`, incluso en cascada desde `businesses`) -- una vez
    sembrada una entrada, el negocio de prueba queda para el resto de la
    sesion de pytest en vez de fallar la limpieza. El contenedor de Postgres
    de la sesion se descarta entero al terminar (`tests.conftest.
    postgres_container`)."""
    del factory, business_id


@pytest.fixture(
    params=[
        pytest.param("fake", id="fake"),
        pytest.param("sql", id="sql", marks=pytest.mark.integration),
    ]
)
async def audit(request: pytest.FixtureRequest) -> AsyncIterator[AuditFixture]:
    if request.param == "fake":
        yield AuditFixture(FakeAuditReadPort(), BUSINESS_A, 1)
        return

    factory: async_sessionmaker[AsyncSession] = request.getfixturevalue("mcp_session_factory")
    business_id, seq = await _seed_sql_audit(factory)
    port = SqlAuditReadPort(factory)
    try:
        yield AuditFixture(port, str(business_id), seq)
    finally:
        await _cleanup_sql_audit(factory, business_id)


async def test_search_decision_log_returns_at_least_one_entry(audit: AuditFixture) -> None:
    page = await audit.port.search_decision_log(
        audit.business_id,
        since=_SINCE,
        until=_UNTIL,
        event_type=None,
        entity_ref=None,
        limit=50,
        cursor=None,
    )

    assert len(page.items) >= 1
    assert page.items[0].event_type


async def test_get_decision_log_entry_matches_the_seeded_seq(audit: AuditFixture) -> None:
    detail = await audit.port.get_decision_log_entry(audit.business_id, audit.seq)

    assert detail.summary.seq == audit.seq
    assert isinstance(detail.payload, dict)


@pytest.mark.integration
async def test_sql_get_decision_log_entry_unknown_seq_raises_not_found(
    mcp_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    business_id, _seq = await _seed_sql_audit(mcp_session_factory)
    port = SqlAuditReadPort(mcp_session_factory)
    try:
        with pytest.raises(EntityNotFoundError):
            await port.get_decision_log_entry(str(business_id), 999_999)
    finally:
        await _cleanup_sql_audit(mcp_session_factory, business_id)


@pytest.mark.integration
async def test_sql_search_decision_log_never_leaks_another_business(
    mcp_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    business_id, _seq = await _seed_sql_audit(mcp_session_factory)
    other_business_id, _other_seq = await _seed_sql_audit(mcp_session_factory)
    port = SqlAuditReadPort(mcp_session_factory)
    try:
        page = await port.search_decision_log(
            str(business_id),
            since=_SINCE,
            until=_UNTIL,
            event_type=None,
            entity_ref=None,
            limit=50,
            cursor=None,
        )
        assert all(
            entry.entry_hash != "" for entry in page.items
        )  # sanity: hay entradas reales
        # Ninguna entrada devuelta pertenece al otro negocio: si business_id
        # no filtrara de verdad, buscar con el negocio B devolveria la fila
        # sembrada para A o viceversa.
        page_other = await port.search_decision_log(
            str(other_business_id),
            since=_SINCE,
            until=_UNTIL,
            event_type=None,
            entity_ref=None,
            limit=50,
            cursor=None,
        )
        assert {e.seq for e in page.items}.isdisjoint({e.seq for e in page_other.items})
    finally:
        await _cleanup_sql_audit(mcp_session_factory, business_id)
        await _cleanup_sql_audit(mcp_session_factory, other_business_id)

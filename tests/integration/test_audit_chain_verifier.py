"""`ChainVerifier` contra Postgres real (tasks.md T010, threat-model.md
C-19): `test_chain_verifies_after_1000_entries` inserta 1000 filas por el
repositorio SQL -- pasando por el trigger `decision_log_chain()` de
`0002_audit_chain.py` -- y comprueba que `VerifyDecisionLogChain`
recompute la cadena exactamente igual. `seq` es un `BIGSERIAL` global: no
se resetea al deshacer la transaccion entre tests (las secuencias de
Postgres no son transaccionales), asi que el test nunca asume que empieza
en 1 -- solo que es contiguo y que enlaza correctamente."""

from __future__ import annotations

import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from safent_ads.audit.application.ports import DecisionLogFilter
from safent_ads.audit.application.record_decision import RecordDecision
from safent_ads.audit.application.verify_decision_log_chain import VerifyDecisionLogChain
from safent_ads.audit.domain.chain import ChainVerifier
from safent_ads.audit.domain.entry import ActorKind, DecisionKind, PendingDecision
from safent_ads.audit.infrastructure.sql_repository import SqlDecisionLogRepository
from safent_ads.shared.clock import FixedClock
from safent_ads.shared.ids import BusinessId

pytestmark = pytest.mark.integration

_ENTRY_COUNT = 1000


async def test_chain_verifies_after_1000_entries(
    db_session: AsyncSession, fake_clock: FixedClock
) -> None:
    repository = SqlDecisionLogRepository(db_session)
    record = RecordDecision(repository)
    business_id = BusinessId.new()

    entries = [
        await record.execute(
            PendingDecision(
                business_id=business_id,
                kind=DecisionKind.SIGNAL,
                actor_kind=ActorKind.RULE_ENGINE,
                payload={"i": i, "cause_key": f"M{i % 24:02d}"},
            )
        )
        for i in range(_ENTRY_COUNT)
    ]

    report = await VerifyDecisionLogChain(repository, ChainVerifier(), fake_clock).execute()

    assert report.chain_ok is True
    assert report.verified_through_seq == entries[-1].seq
    assert entries[-1].seq - entries[0].seq + 1 == _ENTRY_COUNT


async def test_appended_entry_chains_to_the_previous_one(db_session: AsyncSession) -> None:
    repository = SqlDecisionLogRepository(db_session)
    record = RecordDecision(repository)
    business_id = BusinessId.new()

    first = await record.execute(
        PendingDecision(
            business_id=business_id,
            kind=DecisionKind.LOGIN,
            actor_kind=ActorKind.OWNER,
            payload={"outcome": "success"},
        )
    )
    second = await record.execute(
        PendingDecision(
            business_id=business_id,
            kind=DecisionKind.LOGOUT,
            actor_kind=ActorKind.OWNER,
            payload={},
        )
    )

    assert second.prev_hash == first.entry_hash
    assert second.seq == first.seq + 1


async def test_search_decision_log_reads_back_what_was_recorded(db_session: AsyncSession) -> None:
    repository = SqlDecisionLogRepository(db_session)
    record = RecordDecision(repository)
    business_id = BusinessId.new()
    await record.execute(
        PendingDecision(
            business_id=business_id,
            kind=DecisionKind.APPROVAL,
            actor_kind=ActorKind.OWNER,
            payload={"cause_key": "M05"},
        )
    )

    page = await repository.search(DecisionLogFilter(business_id=business_id))

    assert len(page.entries) == 1
    assert page.entries[0].kind == DecisionKind.APPROVAL
    assert page.entries[0].payload == {"cause_key": "M05"}


async def test_get_by_seq_returns_none_for_other_business(db_session: AsyncSession) -> None:
    repository = SqlDecisionLogRepository(db_session)
    record = RecordDecision(repository)
    owner_business = BusinessId.new()
    other_business = BusinessId.new()
    entry = await record.execute(
        PendingDecision(
            business_id=owner_business,
            kind=DecisionKind.EXECUTION,
            actor_kind=ActorKind.RULE_ENGINE,
            payload={},
        )
    )

    assert await repository.get_by_seq(other_business, entry.seq) is None
    assert await repository.get_by_seq(owner_business, entry.seq) is not None

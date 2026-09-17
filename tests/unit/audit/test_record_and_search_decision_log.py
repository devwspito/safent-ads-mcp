"""`RecordDecision`/`SearchDecisionLog`/`VerifyDecisionLogChain` (tasks.md
T010) contra el doble en memoria: orquestacion pura, sin Postgres."""

from __future__ import annotations

from datetime import UTC, datetime

import pytest

from safent_ads.audit.application.ports import DecisionLogFilter, InvalidDecisionLogFilterError
from safent_ads.audit.application.record_decision import RecordDecision
from safent_ads.audit.application.search_decision_log import SearchDecisionLog
from safent_ads.audit.application.verify_decision_log_chain import VerifyDecisionLogChain
from safent_ads.audit.domain.chain import ChainVerifier
from safent_ads.audit.domain.entry import ActorKind, DecisionKind, PendingDecision
from safent_ads.audit.infrastructure.in_memory_repository import InMemoryDecisionLogRepository
from safent_ads.shared.clock import FixedClock
from safent_ads.shared.ids import BusinessId


def _pending(business_id: BusinessId, kind: DecisionKind = DecisionKind.SIGNAL) -> PendingDecision:
    return PendingDecision(
        business_id=business_id, kind=kind, actor_kind=ActorKind.RULE_ENGINE, payload={"n": 1}
    )


async def test_record_decision_assigns_sequential_seq_and_chains_hashes() -> None:
    repository = InMemoryDecisionLogRepository()
    use_case = RecordDecision(repository)
    business_id = BusinessId.new()

    first = await use_case.execute(_pending(business_id))
    second = await use_case.execute(_pending(business_id))

    assert first.seq == 1
    assert second.seq == 2
    assert second.prev_hash == first.entry_hash
    assert first.entry_hash != second.entry_hash


async def test_search_decision_log_filters_by_business_id() -> None:
    repository = InMemoryDecisionLogRepository()
    record = RecordDecision(repository)
    business_a, business_b = BusinessId.new(), BusinessId.new()
    await record.execute(_pending(business_a))
    await record.execute(_pending(business_b))

    page = await SearchDecisionLog(repository).execute(DecisionLogFilter(business_id=business_a))

    assert len(page.entries) == 1
    assert page.entries[0].business_id == business_a


async def test_search_decision_log_filters_by_kind() -> None:
    repository = InMemoryDecisionLogRepository()
    record = RecordDecision(repository)
    business_id = BusinessId.new()
    await record.execute(_pending(business_id, DecisionKind.SIGNAL))
    await record.execute(_pending(business_id, DecisionKind.APPROVAL))

    page = await SearchDecisionLog(repository).execute(
        DecisionLogFilter(business_id=business_id, kind=DecisionKind.APPROVAL)
    )

    assert len(page.entries) == 1
    assert page.entries[0].kind == DecisionKind.APPROVAL


async def test_search_decision_log_paginates_with_cursor() -> None:
    repository = InMemoryDecisionLogRepository()
    record = RecordDecision(repository)
    business_id = BusinessId.new()
    for _ in range(3):
        await record.execute(_pending(business_id))

    first_page = await SearchDecisionLog(repository).execute(
        DecisionLogFilter(business_id=business_id, limit=2)
    )
    second_page = await SearchDecisionLog(repository).execute(
        DecisionLogFilter(business_id=business_id, limit=2, cursor_seq=first_page.next_cursor_seq)
    )

    assert [e.seq for e in first_page.entries] == [3, 2]
    assert [e.seq for e in second_page.entries] == [1]
    assert second_page.next_cursor_seq is None


def test_decision_log_filter_rejects_limit_out_of_range() -> None:
    with pytest.raises(InvalidDecisionLogFilterError):
        DecisionLogFilter(business_id=BusinessId.new(), limit=0)
    with pytest.raises(InvalidDecisionLogFilterError):
        DecisionLogFilter(business_id=BusinessId.new(), limit=201)


def test_decision_log_filter_rejects_since_after_until() -> None:
    with pytest.raises(InvalidDecisionLogFilterError):
        DecisionLogFilter(
            business_id=BusinessId.new(),
            since=datetime(2026, 1, 2, tzinfo=UTC),
            until=datetime(2026, 1, 1, tzinfo=UTC),
        )


async def test_verify_decision_log_chain_reports_ok_for_untampered_log() -> None:
    repository = InMemoryDecisionLogRepository()
    record = RecordDecision(repository)
    business_id = BusinessId.new()
    for _ in range(5):
        await record.execute(_pending(business_id))
    clock = FixedClock(datetime(2026, 9, 9, tzinfo=UTC))

    report = await VerifyDecisionLogChain(repository, ChainVerifier(), clock).execute()

    assert report.chain_ok is True
    assert report.verified_through_seq == 5
    assert report.checked_at == clock.now()

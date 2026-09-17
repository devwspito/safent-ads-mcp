"""`SqlTelegramCallbackStore` contra Postgres real (0010_notifications):
`consume` es atomica y de un solo uso, `peek` nunca gasta el nonce, y el
binding a `(chat_id, message_id)` se respeta -- lo mismo que
`tests/integration/migrations/test_notifications.py` prueba a nivel de
esquema, aqui contra el puerto de verdad."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from safent_ads.notifications.domain.callback import CallbackAction, generate_nonce
from safent_ads.notifications.infrastructure.sql_repositories import SqlTelegramCallbackStore
from safent_ads.proposals.domain.cause import Cause, CauseKey, Evidence
from safent_ads.proposals.domain.classification import Classification
from safent_ads.proposals.domain.money import Money
from safent_ads.proposals.domain.priority import Priority, Urgency
from safent_ads.proposals.domain.proposal import Proposal as ProposalAggregate
from safent_ads.proposals.domain.proposal import ProposedDiff, new_proposal_id
from safent_ads.proposals.infrastructure.sql_proposal_repository import SqlProposalRepository
from safent_ads.shared.ids import BusinessId, EntityRef
from tests.contracts.sql_fixtures import campaign_ref, seed_entity

pytestmark = pytest.mark.integration

_NOW = datetime(2026, 9, 9, 14, 0, tzinfo=UTC)
_CHAT_ID = 111222333
_MESSAGE_ID = 42


async def _seed_proposal(session: AsyncSession, *, external_id: str) -> str:
    entity_ref: EntityRef = campaign_ref(external_id, platform_value="google")
    business_id = BusinessId(await seed_entity(session, entity_ref))
    diff = ProposedDiff.build(
        entity_ref=entity_ref,
        parameter="daily_budget",
        before=Money.of("90"),
        after=Money.of("117"),
    )
    proposal = ProposalAggregate.raise_proposal(
        proposal_id=new_proposal_id(),
        business_id=business_id,
        diff=diff,
        classification=Classification.IMPORTANT,
        cause=Cause(text="CPL bajo objetivo", rule_id="G01"),
        cause_key=CauseKey(entity_ref=entity_ref, rule_id="G01", cause_type="cpl_below_target"),
        evidence=(Evidence(metric="cpl", actual=19.0, target=28.0, window_preset="7D"),),
        estimated_impact=Money.of("810"),
        priority=Priority(urgency=Urgency.RECOMMENDED),
        now=_NOW,
        expires_at=_NOW + timedelta(days=3),
    )
    await SqlProposalRepository(session).save(proposal)
    await session.flush()
    return str(proposal.proposal_id)


async def _store_with_nonce(
    session: AsyncSession, *, proposal_id: str, expires_at: datetime
) -> tuple[SqlTelegramCallbackStore, str]:
    """`expires_at` es absoluto y SIEMPRE relativo al reloj real: la
    columna `created_at` de `telegram_callbacks` es `DEFAULT now()`
    (0010_notifications), y `telegram_callbacks_ttl_check` exige
    `expires_at > created_at` -- una fecha fija de otro dia (como la `_NOW`
    de dominio que usa `_seed_proposal`, cuya `created_at` SI se manda
    explicita) rompería ese CHECK segun a que hora real corra el test."""
    store = SqlTelegramCallbackStore(session)
    nonce = generate_nonce()
    await store.create(
        nonce=nonce,
        proposal_id=proposal_id,
        chat_id=_CHAT_ID,
        message_id=_MESSAGE_ID,
        diff_hash="a" * 64,
        action=CallbackAction.APPROVE,
        expires_at=expires_at,
    )
    await session.flush()
    return store, nonce


async def test_consume_is_single_use(db_session: AsyncSession) -> None:
    proposal_id = await _seed_proposal(db_session, external_id="cb-single-use")
    reference = datetime.now(UTC)
    store, nonce = await _store_with_nonce(
        db_session, proposal_id=proposal_id, expires_at=reference + timedelta(hours=6)
    )

    first = await store.consume(
        nonce=nonce, chat_id=_CHAT_ID, message_id=_MESSAGE_ID, now=reference
    )
    second = await store.consume(
        nonce=nonce, chat_id=_CHAT_ID, message_id=_MESSAGE_ID, now=reference
    )

    assert first is not None
    assert first.proposal_id == proposal_id
    assert first.action is CallbackAction.APPROVE
    assert second is None


async def test_consume_rejects_expired_nonce(db_session: AsyncSession) -> None:
    proposal_id = await _seed_proposal(db_session, external_id="cb-expired")
    reference = datetime.now(UTC)
    store, nonce = await _store_with_nonce(
        db_session, proposal_id=proposal_id, expires_at=reference + timedelta(seconds=30)
    )

    result = await store.consume(
        nonce=nonce, chat_id=_CHAT_ID, message_id=_MESSAGE_ID, now=reference + timedelta(hours=1)
    )

    assert result is None


async def test_consume_rejects_mismatched_chat_or_message(db_session: AsyncSession) -> None:
    proposal_id = await _seed_proposal(db_session, external_id="cb-mismatch")
    reference = datetime.now(UTC)
    store, nonce = await _store_with_nonce(
        db_session, proposal_id=proposal_id, expires_at=reference + timedelta(hours=6)
    )

    wrong_chat = await store.consume(
        nonce=nonce, chat_id=999999999, message_id=_MESSAGE_ID, now=reference
    )
    wrong_message = await store.consume(
        nonce=nonce, chat_id=_CHAT_ID, message_id=999, now=reference
    )

    assert wrong_chat is None
    assert wrong_message is None


async def test_peek_never_consumes(db_session: AsyncSession) -> None:
    proposal_id = await _seed_proposal(db_session, external_id="cb-peek")
    reference = datetime.now(UTC)
    store, nonce = await _store_with_nonce(
        db_session, proposal_id=proposal_id, expires_at=reference + timedelta(hours=6)
    )

    first = await store.peek(nonce=nonce, chat_id=_CHAT_ID, message_id=_MESSAGE_ID, now=reference)
    second = await store.peek(
        nonce=nonce, chat_id=_CHAT_ID, message_id=_MESSAGE_ID, now=reference
    )
    consumed = await store.consume(
        nonce=nonce, chat_id=_CHAT_ID, message_id=_MESSAGE_ID, now=reference
    )

    assert first is not None
    assert second is not None
    assert consumed is not None  # peek no gasto el nonce


async def test_find_by_nonce_returns_the_row_even_when_expired(db_session: AsyncSession) -> None:
    proposal_id = await _seed_proposal(db_session, external_id="cb-find")
    reference = datetime.now(UTC)
    store, nonce = await _store_with_nonce(
        db_session, proposal_id=proposal_id, expires_at=reference + timedelta(seconds=30)
    )

    found = await store.find_by_nonce(nonce)

    assert found is not None
    assert found.proposal_id == proposal_id


async def test_find_by_nonce_returns_none_for_unknown_nonce(db_session: AsyncSession) -> None:
    found = await SqlTelegramCallbackStore(db_session).find_by_nonce(generate_nonce())

    assert found is None

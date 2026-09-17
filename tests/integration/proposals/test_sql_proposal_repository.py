"""`SqlProposalRepository` contra Postgres real (0008_proposals +
0017_optimization): la nueva columna `expected_contribution_delta` hace el
viaje de ida y vuelta, y `list_by_lens` ordena por ella DESC con NULLS LAST
(profitability-engine.md §8: 'la cola cambia de eje')."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from safent_ads.proposals.domain.cause import Cause, CauseKey, Evidence
from safent_ads.proposals.domain.classification import Classification
from safent_ads.proposals.domain.money import Money
from safent_ads.proposals.domain.priority import Priority, Urgency
from safent_ads.proposals.domain.proposal import (
    PostponedReason,
    Proposal,
    ProposedDiff,
    new_proposal_id,
)
from safent_ads.proposals.infrastructure.sql_proposal_repository import (
    ProposalLens,
    SqlProposalRepository,
)
from safent_ads.shared.ids import BusinessId, EntityRef
from tests.contracts.sql_fixtures import campaign_ref, seed_entity

pytestmark = pytest.mark.integration

_NOW = datetime(2026, 9, 9, 10, tzinfo=UTC)


def _diff(entity_ref: EntityRef, *, parameter: str = "daily_budget") -> ProposedDiff:
    return ProposedDiff.build(
        entity_ref=entity_ref, parameter=parameter, before=Money.of("100"), after=Money.of("70")
    )


def _proposal(
    *,
    business_id: BusinessId,
    entity_ref: EntityRef,
    parameter: str = "daily_budget",
    urgency: Urgency = Urgency.RECOMMENDED,
    expected_contribution_delta: Money | None = None,
) -> Proposal:
    diff = _diff(entity_ref, parameter=parameter)
    return Proposal.raise_proposal(
        proposal_id=new_proposal_id(),
        business_id=business_id,
        diff=diff,
        classification=Classification.ROUTINE,
        cause=Cause(text="CPL sobre objetivo en 7D", signal_id=None, rule_id=None),
        cause_key=CauseKey(entity_ref=entity_ref, rule_id="none", cause_type="cost_per_lead_high"),
        evidence=(Evidence(metric="cpl", actual=41.2, target=28.0, window_preset="7D"),),
        estimated_impact=Money.of("310"),
        priority=Priority(urgency=urgency),
        now=_NOW,
        expires_at=_NOW + timedelta(hours=72),
        expected_contribution_delta=expected_contribution_delta,
    )


class TestExpectedContributionDeltaRoundTrip:
    async def test_saved_delta_survives_the_round_trip(self, db_session: AsyncSession) -> None:
        entity_ref = campaign_ref(f"pcd-{id(db_session)}", platform_value="google")
        business_id = BusinessId(await seed_entity(db_session, entity_ref))
        repository = SqlProposalRepository(db_session)
        proposal = _proposal(
            business_id=business_id,
            entity_ref=entity_ref,
            expected_contribution_delta=Money.of("56.60"),
        )

        await repository.save(proposal)
        reloaded = await repository.get(proposal.proposal_id)

        assert reloaded is not None
        assert reloaded.expected_contribution_delta == Money.of("56.60")

    async def test_missing_delta_round_trips_as_none(self, db_session: AsyncSession) -> None:
        entity_ref = campaign_ref(f"pcd-none-{id(db_session)}", platform_value="google")
        business_id = BusinessId(await seed_entity(db_session, entity_ref))
        repository = SqlProposalRepository(db_session)
        proposal = _proposal(business_id=business_id, entity_ref=entity_ref)

        await repository.save(proposal)
        reloaded = await repository.get(proposal.proposal_id)

        assert reloaded is not None
        assert reloaded.expected_contribution_delta is None


class TestListByLensOrdersByContributionDelta:
    async def test_higher_contribution_comes_first_nulls_last(
        self, db_session: AsyncSession
    ) -> None:
        low_ref = campaign_ref(f"low-{id(db_session)}", platform_value="google")
        business_id = BusinessId(await seed_entity(db_session, low_ref))
        repository = SqlProposalRepository(db_session)

        low = _proposal(
            business_id=business_id,
            entity_ref=low_ref,
            parameter="daily_budget",
            expected_contribution_delta=Money.of("10"),
        )
        no_delta = _proposal(
            business_id=business_id,
            entity_ref=low_ref,
            parameter="targeting",
            urgency=Urgency.CRITICAL,
        )
        high = _proposal(
            business_id=business_id,
            entity_ref=low_ref,
            parameter="bid_strategy",
            expected_contribution_delta=Money.of("75"),
        )
        for proposal in (low, no_delta, high):
            await repository.save(proposal)

        results = await repository.list_by_lens(ProposalLens(business_id=business_id))

        ids_in_order = [r.proposal_id for r in results]
        assert ids_in_order == [high.proposal_id, low.proposal_id, no_delta.proposal_id]


class TestOwnerContextAndPostponedReasonRoundTrip:
    async def test_owner_context_survives_the_round_trip(self, db_session: AsyncSession) -> None:
        entity_ref = campaign_ref(f"oc-{id(db_session)}", platform_value="google")
        business_id = BusinessId(await seed_entity(db_session, entity_ref))
        repository = SqlProposalRepository(db_session)
        proposal = _proposal(business_id=business_id, entity_ref=entity_ref)
        proposal.set_owner_context("Esperar al cierre del evento de calendario.")

        await repository.save(proposal)
        reloaded = await repository.get(proposal.proposal_id)

        assert reloaded is not None
        assert reloaded.owner_context == "Esperar al cierre del evento de calendario."

    async def test_missing_owner_context_round_trips_as_none(
        self, db_session: AsyncSession
    ) -> None:
        entity_ref = campaign_ref(f"oc-none-{id(db_session)}", platform_value="google")
        business_id = BusinessId(await seed_entity(db_session, entity_ref))
        repository = SqlProposalRepository(db_session)
        proposal = _proposal(business_id=business_id, entity_ref=entity_ref)

        await repository.save(proposal)
        reloaded = await repository.get(proposal.proposal_id)

        assert reloaded is not None
        assert reloaded.owner_context is None

    async def test_postponed_reason_survives_the_round_trip(
        self, db_session: AsyncSession
    ) -> None:
        entity_ref = campaign_ref(f"pr-{id(db_session)}", platform_value="google")
        business_id = BusinessId(await seed_entity(db_session, entity_ref))
        repository = SqlProposalRepository(db_session)
        proposal = _proposal(business_id=business_id, entity_ref=entity_ref)
        proposal.postpone(_NOW + timedelta(hours=1), _NOW)

        await repository.save(proposal)
        reloaded = await repository.get(proposal.proposal_id)

        assert reloaded is not None
        assert reloaded.postponed_reason is PostponedReason.OWNER
        assert reloaded.postpone_until == _NOW + timedelta(hours=1)

    async def test_reactivating_clears_the_postponed_reason(
        self, db_session: AsyncSession
    ) -> None:
        entity_ref = campaign_ref(f"pr-clear-{id(db_session)}", platform_value="google")
        business_id = BusinessId(await seed_entity(db_session, entity_ref))
        repository = SqlProposalRepository(db_session)
        proposal = _proposal(business_id=business_id, entity_ref=entity_ref)
        proposal.postpone(_NOW + timedelta(hours=1), _NOW)
        proposal.reactivate(_NOW + timedelta(hours=1))

        await repository.save(proposal)
        reloaded = await repository.get(proposal.proposal_id)

        assert reloaded is not None
        assert reloaded.postponed_reason is None
        assert reloaded.postpone_until is None

"""Repositorios SQL de `optimization` contra Postgres real (migracion
0017_optimization): UPSERT de `marginal_estimates`/`response_curves`,
`allocation_plans` solo-anexable con sus dos FK a `ad_entities`."""

from __future__ import annotations

import uuid
from datetime import UTC, datetime

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from safent_ads.economics.domain.money import Money
from safent_ads.optimization.application.dto import StoredResponseCurve
from safent_ads.optimization.domain.allocation import (
    AllocationDirection,
    AllocationPlan,
    AllocationStep,
)
from safent_ads.optimization.domain.identifiers import AllocationPlanId
from safent_ads.optimization.domain.marginal import EstimationMethod, MarginalEstimate
from safent_ads.optimization.domain.response_curve import (
    CurveConfidence,
    HillCurve,
    ObservedSpendRange,
)
from safent_ads.optimization.infrastructure.sql_repositories import (
    SqlAllocationPlanRepository,
    SqlMarginalEstimateRepository,
    SqlResponseCurveRepository,
)
from safent_ads.shared.ids import BusinessId, EntityLevel, EntityRef, PlatformCode
from tests.contracts.sql_fixtures import seed_entity

pytestmark = pytest.mark.integration


def _campaign_ref(external_id: str) -> EntityRef:
    return EntityRef(
        platform=PlatformCode.GOOGLE, level=EntityLevel.CAMPAIGN, external_id=external_id
    )


async def _seed_second_entity_in_business(
    session: AsyncSession, *, business_id: uuid.UUID, entity_ref: EntityRef
) -> None:
    """Segunda entidad en el MISMO negocio (`seed_entity` siempre crea uno
    nuevo): credencial + cuenta + entidad, sin repetir el `INSERT INTO
    businesses`."""
    credential_id = uuid.uuid4()
    account_id = uuid.uuid4()
    suffix = account_id.hex[:12]
    await session.execute(
        text("INSERT INTO credential_refs (id, platform, alias) VALUES (:id, :platform, :alias)"),
        {"id": credential_id, "platform": entity_ref.platform.value, "alias": f"alias-{suffix}"},
    )
    await session.execute(
        text(
            """
            INSERT INTO platform_accounts (id, business_id, platform, external_account_id,
                                           currency, timezone, api_tier, credential_ref_id,
                                           status)
            VALUES (:id, :business_id, :platform, :external_account_id, 'EUR',
                    'Europe/Madrid', 'meta_full', :credential_ref_id, 'ACTIVE')
            """
        ),
        {
            "id": account_id,
            "business_id": business_id,
            "platform": entity_ref.platform.value,
            "external_account_id": f"act_{suffix}",
            "credential_ref_id": credential_id,
        },
    )
    await session.execute(
        text(
            """
            INSERT INTO ad_entities (business_id, platform_account_id, platform, level,
                                     external_id, name, status, platform_state_hash)
            VALUES (:business_id, :account_id, :platform, :level, :external_id,
                    'Campana de contrato', 'ACTIVE', :state_hash)
            """
        ),
        {
            "business_id": business_id,
            "account_id": account_id,
            "platform": entity_ref.platform.value,
            "level": entity_ref.level.value,
            "external_id": entity_ref.external_id,
            "state_hash": "a" * 64,
        },
    )
    await session.flush()


class TestMarginalEstimateRepository:
    async def test_upsert_overwrites_the_previous_estimate(self, db_session: AsyncSession) -> None:
        entity_ref = _campaign_ref(f"me-{id(db_session)}")
        business_id = BusinessId(await seed_entity(db_session, entity_ref))
        repository = SqlMarginalEstimateRepository(db_session)
        first = MarginalEstimate(
            value=0.5, ci_low=0.1, ci_high=0.9, method=EstimationMethod.PAIRED, sample_size=7
        )
        second = MarginalEstimate(
            value=3.0, ci_low=2.0, ci_high=4.0, method=EstimationMethod.MMM, sample_size=52
        )

        await repository.save(
            business_id=business_id,
            entity_ref=entity_ref,
            estimate=first,
            computed_at=datetime.now(UTC),
        )
        await repository.save(
            business_id=business_id,
            entity_ref=entity_ref,
            estimate=second,
            computed_at=datetime.now(UTC),
        )
        reloaded = await repository.get_latest(business_id=business_id, entity_ref=entity_ref)

        assert reloaded is not None
        assert reloaded.value == pytest.approx(3.0)
        assert reloaded.method is EstimationMethod.MMM

    async def test_missing_estimate_returns_none(self, db_session: AsyncSession) -> None:
        entity_ref = _campaign_ref(f"me-missing-{id(db_session)}")
        business_id = BusinessId(await seed_entity(db_session, entity_ref))
        repository = SqlMarginalEstimateRepository(db_session)

        reloaded = await repository.get_latest(business_id=business_id, entity_ref=entity_ref)

        assert reloaded is None


class TestResponseCurveRepository:
    async def test_round_trips_hill_curve(self, db_session: AsyncSession) -> None:
        entity_ref = _campaign_ref(f"rc-{id(db_session)}")
        business_id = BusinessId(await seed_entity(db_session, entity_ref))
        repository = SqlResponseCurveRepository(db_session)
        record = StoredResponseCurve(
            curve=HillCurve(e_max=100.0, k=500.0),
            observed_range=ObservedSpendRange(min_spend=300.0, max_spend=900.0),
            residual_std=2.5,
            curve_confidence=CurveConfidence.OBSERVATIONAL,
        )

        await repository.save(business_id=business_id, channel_id=str(entity_ref), record=record)
        reloaded = await repository.get_latest(business_id=business_id, entity_ref=entity_ref)

        assert reloaded is not None
        assert isinstance(reloaded.curve, HillCurve)
        assert reloaded.curve.e_max == pytest.approx(100.0)
        assert reloaded.curve.k == pytest.approx(500.0)
        assert reloaded.observed_range.min_spend == pytest.approx(300.0)


class TestAllocationPlanRepository:
    async def test_saves_a_plan_with_both_proposal_refs(self, db_session: AsyncSession) -> None:
        donor_ref = _campaign_ref(f"donor-{id(db_session)}")
        receiver_ref = _campaign_ref(f"receiver-{id(db_session)}")
        business_id = BusinessId(await seed_entity(db_session, donor_ref))
        await _seed_second_entity_in_business(
            db_session, business_id=business_id.value, entity_ref=receiver_ref
        )
        repository = SqlAllocationPlanRepository(db_session)
        plan = AllocationPlan(
            plan_id=AllocationPlanId.new(),
            business_id=business_id,
            donor_step=AllocationStep(
                entity_ref=donor_ref,
                direction=AllocationDirection.DECREASE,
                current_daily_spend=Money.of("150"),
                proposed_daily_spend=Money.of("120"),
                step_pct=0.20,
                cadence_days=3,
            ),
            receiver_step=AllocationStep(
                entity_ref=receiver_ref,
                direction=AllocationDirection.INCREASE,
                current_daily_spend=Money.of("111"),
                proposed_daily_spend=Money.of("133"),
                step_pct=0.20,
                cadence_days=7,
            ),
            expected_contribution_delta=Money.of("56.60"),
            created_at=datetime.now(UTC),
        )

        await repository.save(plan=plan, decrease_proposal_id=None, increase_proposal_id=None)

        # Sin excepcion == exito: la fila cumplio ambas FK compuestas.

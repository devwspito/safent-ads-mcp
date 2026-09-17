"""Adaptadores SQL reales de los 4 puertos de `optimization` que antes solo
tenian doble en memoria (`optimization/testing/in_memory_repositories.py`):
`ReallocationCandidateRepository`, `DiagnosisMetricsPort`,
`ContributionMarginPort` y `ReallocationProposalPort`. Contra Postgres real,
mismo patron que `test_sql_repositories.py`."""

from __future__ import annotations

import uuid
from datetime import UTC, date, datetime, timedelta

import httpx
import pytest
from fastapi import FastAPI
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from safent_ads.economics.domain.identifiers import ProductId, UnitEconomicsProfileId
from safent_ads.economics.domain.money import Money as EconomicsMoney
from safent_ads.economics.domain.rate import Rate, Theta
from safent_ads.economics.domain.unit_economics import UnitEconomicsProfile
from safent_ads.economics.infrastructure.sql_repositories import SqlUnitEconomicsProfileRepository
from safent_ads.iam.presentation.dependencies import require_business_access
from safent_ads.optimization.domain.allocation import (
    AllocationDirection,
    AllocationPlan,
    AllocationStep,
)
from safent_ads.optimization.domain.identifiers import AllocationPlanId
from safent_ads.optimization.domain.marginal import EstimationMethod, MarginalEstimate
from safent_ads.optimization.infrastructure.sql_contribution_margin_port import (
    SqlContributionMarginPort,
)
from safent_ads.optimization.infrastructure.sql_diagnosis_metrics_port import (
    SqlDiagnosisMetricsPort,
)
from safent_ads.optimization.infrastructure.sql_reallocation_candidate_repository import (
    SqlReallocationCandidateRepository,
)
from safent_ads.optimization.infrastructure.sql_reallocation_proposal_port import (
    SqlReallocationProposalPort,
)
from safent_ads.optimization.infrastructure.sql_repositories import SqlMarginalEstimateRepository
from safent_ads.optimization.presentation.rest import build_optimization_router_over_sql
from safent_ads.shared.clock import FixedClock
from safent_ads.shared.ids import BusinessId, EntityLevel, EntityRef, PlatformCode
from tests.contracts.sql_fixtures import seed_entity

pytestmark = pytest.mark.integration

_FIXED_NOW = datetime(2026, 9, 9, 12, 0, tzinfo=UTC)


def _campaign_ref(external_id: str) -> EntityRef:
    return EntityRef(
        platform=PlatformCode.GOOGLE, level=EntityLevel.CAMPAIGN, external_id=external_id
    )


async def _seed_second_campaign(
    session: AsyncSession, *, business_id: uuid.UUID, entity_ref: EntityRef
) -> uuid.UUID:
    """Segunda campana en el MISMO negocio (mismo patron que
    `test_sql_repositories.py::_seed_second_entity_in_business`)."""
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
    return account_id


async def _set_budget(session: AsyncSession, entity_ref: EntityRef, amount_minor: int) -> None:
    await session.execute(
        text(
            "UPDATE ad_entities SET budget_amount_minor = :amount, budget_currency = 'EUR', "
            "budget_kind = 'daily' WHERE entity_ref = :entity_ref"
        ),
        {"amount": amount_minor, "entity_ref": str(entity_ref)},
    )


async def _platform_account_id(session: AsyncSession, entity_ref: EntityRef) -> uuid.UUID:
    result = await session.execute(
        text("SELECT platform_account_id FROM ad_entities WHERE entity_ref = :entity_ref"),
        {"entity_ref": str(entity_ref)},
    )
    return result.scalar_one()


async def _insert_metrics_day(
    session: AsyncSession,
    *,
    business_id: uuid.UUID,
    entity_ref: EntityRef,
    platform_account_id: uuid.UUID,
    stat_date: date,
    spend: str,
    impressions: int,
    clicks: int,
) -> None:
    await session.execute(
        text(
            """
            INSERT INTO metrics_daily (business_id, entity_ref, entity_level,
                                       platform_account_id, stat_date, account_timezone,
                                       currency, spend, impressions, clicks)
            VALUES (:business_id, :entity_ref, 'campaign', :platform_account_id, :stat_date,
                    'Europe/Madrid', 'EUR', :spend, :impressions, :clicks)
            """
        ),
        {
            "business_id": business_id,
            "entity_ref": str(entity_ref),
            "platform_account_id": platform_account_id,
            "stat_date": stat_date,
            "spend": spend,
            "impressions": impressions,
            "clicks": clicks,
        },
    )


class TestReallocationCandidateRepository:
    async def test_lists_only_campaigns_with_a_materialized_estimate(
        self, db_session: AsyncSession
    ) -> None:
        with_estimate = _campaign_ref(f"cand-{id(db_session)}-a")
        without_estimate = _campaign_ref(f"cand-{id(db_session)}-b")
        business_id = await seed_entity(db_session, with_estimate)
        await _seed_second_campaign(
            db_session, business_id=business_id, entity_ref=without_estimate
        )
        await _set_budget(db_session, with_estimate, 40000)
        await _set_budget(db_session, without_estimate, 20000)
        await SqlMarginalEstimateRepository(db_session).save(
            business_id=BusinessId(business_id),
            entity_ref=with_estimate,
            estimate=MarginalEstimate(
                value=1.8, ci_low=1.2, ci_high=2.4, method=EstimationMethod.PAIRED, sample_size=14
            ),
            computed_at=_FIXED_NOW,
        )

        candidates = await SqlReallocationCandidateRepository(db_session).list_candidates(
            business_id=BusinessId(business_id)
        )

        assert len(candidates) == 1
        candidate = candidates[0]
        assert candidate.entity_ref == with_estimate
        assert candidate.current_daily_spend.amount == pytest.approx(400)
        assert candidate.min_viable_daily_spend.amount == pytest.approx(80)
        assert candidate.marginal_estimate.value == pytest.approx(1.8)


class TestContributionMarginPort:
    async def test_returns_the_confirmed_contribution_margin(
        self, db_session: AsyncSession
    ) -> None:
        business_id = uuid.uuid4()
        await db_session.execute(
            text(
                "INSERT INTO businesses (id, slug, name, timezone, reference_currency) "
                "VALUES (:id, :slug, 'Negocio de contrato', 'Europe/Madrid', 'EUR')"
            ),
            {"id": business_id, "slug": f"cm-{business_id.hex[:10]}"},
        )
        offering_id = uuid.uuid4()
        await db_session.execute(
            text(
                "INSERT INTO offerings (id, business_id, code, title) "
                "VALUES (:id, :business_id, :code, 'Oferta de contrato')"
            ),
            {"id": offering_id, "business_id": business_id, "code": f"cm-{offering_id.hex[:8]}"},
        )
        product_id = ProductId.parse(str(offering_id))
        profile = UnitEconomicsProfile.create(
            profile_id=UnitEconomicsProfileId.new(),
            business_id=BusinessId(business_id),
            product_id=product_id,
            version=1,
            effective_from=date(2026, 1, 1),
            list_price=EconomicsMoney.of("1000"),
            vat_rate=Rate.of("0.21"),
            discount_rate=Rate.zero(),
            refund_rate=Rate.of("0.05"),
            delivery_cost=EconomicsMoney.of("20"),
            sales_cost_per_close=EconomicsMoney.of("30"),
            collection_rate=Rate.of("0.9"),
            cvr_lead_to_business_conversion=Rate.of("0.2"),
            theta=Theta.default(),
            margin_horizon_days=120,
        )
        await SqlUnitEconomicsProfileRepository(db_session).save(profile)

        port = SqlContributionMarginPort(
            SqlUnitEconomicsProfileRepository(db_session), FixedClock(_FIXED_NOW)
        )
        margin = await port.get_contribution_margin_per_conversion(
            business_id=BusinessId(business_id), product_id=str(product_id)
        )

        assert margin is not None
        assert margin == profile.contribution_margin()

    async def test_returns_none_when_no_profile_exists(self, db_session: AsyncSession) -> None:
        port = SqlContributionMarginPort(
            SqlUnitEconomicsProfileRepository(db_session), FixedClock(_FIXED_NOW)
        )
        margin = await port.get_contribution_margin_per_conversion(
            business_id=BusinessId(uuid.uuid4()), product_id=str(uuid.uuid4())
        )
        assert margin is None


class TestDiagnosisMetricsPort:
    async def test_derives_real_signals_from_metrics_daily_and_entity_state(
        self, db_session: AsyncSession
    ) -> None:
        entity_ref = _campaign_ref(f"diag-{id(db_session)}")
        business_id = await seed_entity(db_session, entity_ref)
        platform_account_id = await _platform_account_id(db_session, entity_ref)
        as_of = _FIXED_NOW.date()
        for offset in range(7):
            await _insert_metrics_day(
                db_session,
                business_id=business_id,
                entity_ref=entity_ref,
                platform_account_id=platform_account_id,
                stat_date=as_of - timedelta(days=offset),
                spend="10",
                impressions=1000,
                clicks=50,
            )

        port = SqlDiagnosisMetricsPort(db_session, FixedClock(_FIXED_NOW))
        metrics = await port.get_metrics(
            business_id=BusinessId(business_id), entity_ref=entity_ref
        )

        assert metrics is not None
        assert metrics.is_suspended is False
        assert metrics.is_stale is True  # sin fila en data_freshness: "viejo" por defecto
        assert metrics.is_learning is False
        # placeholders documentados: nunca disparan el nodo 1 por un falso positivo
        assert metrics.unattributed_share == 0.0
        assert metrics.utm_valid is True
        assert metrics.bridge_has_recent_events_24h is True
        assert metrics.quality_score_below_average is False
        assert metrics.is_retargeting_audience is False

    async def test_returns_none_for_an_unknown_entity(self, db_session: AsyncSession) -> None:
        port = SqlDiagnosisMetricsPort(db_session, FixedClock(_FIXED_NOW))
        metrics = await port.get_metrics(
            business_id=BusinessId(uuid.uuid4()),
            entity_ref=_campaign_ref(f"diag-missing-{id(db_session)}"),
        )
        assert metrics is None

    async def test_derives_unattributed_share_and_delta_hat_from_real_data(
        self, db_session: AsyncSession
    ) -> None:
        """T158: `unattributed_share`/`delta_hat` dejan de ser el
        placeholder que nunca dispara el nodo 1 -- vienen de
        `lead_attributions` y `platform_divergence_snapshots` reales."""
        entity_ref = _campaign_ref(f"diag-real-{id(db_session)}")
        business_id = await seed_entity(db_session, entity_ref)
        platform_account_id = await _platform_account_id(db_session, entity_ref)

        # 3 atribuciones: 2 resueltas a la entidad, 1 `aggregate` -> 1/3.
        await db_session.execute(
            text(
                "INSERT INTO lead_attributions (business_id, hashed_identity, "
                "identity_salt_ref, entity_ref, attribution_rung, conversion_kind, "
                "value_amount, value_currency, occurred_at) VALUES "
                "(:business_id, :h1, 's', :entity_ref, 'hashed_identity', "
                "'business_conversion', 100, 'EUR', :occurred_at), "
                "(:business_id, :h2, 's', :entity_ref, 'hashed_identity', 'lead', "
                "0, 'EUR', :occurred_at), "
                "(:business_id, :h3, 's', NULL, 'aggregate', 'lead', 0, 'EUR', "
                ":occurred_at)"
            ),
            {
                "business_id": business_id,
                "entity_ref": str(entity_ref),
                "h1": "a" * 64,
                "h2": "b" * 64,
                "h3": "c" * 64,
                "occurred_at": _FIXED_NOW,
            },
        )
        await db_session.execute(
            text(
                "INSERT INTO platform_divergence_snapshots (business_id, "
                "platform_account_id, crm_conversions, platform_conversions, "
                "shrinkage_m, value, window_start, window_end) VALUES "
                "(:business_id, :platform_account_id, 1, 10, 10, 2.5, "
                "'2026-08-01', '2026-08-08')"
            ),
            {"business_id": business_id, "platform_account_id": platform_account_id},
        )
        await db_session.flush()

        port = SqlDiagnosisMetricsPort(db_session, FixedClock(_FIXED_NOW))
        metrics = await port.get_metrics(
            business_id=BusinessId(business_id), entity_ref=entity_ref
        )

        assert metrics is not None
        assert metrics.unattributed_share == pytest.approx(1 / 3)
        assert metrics.delta_hat == pytest.approx(2.5)


class TestReallocationProposalPort:
    async def test_raises_two_linked_proposals_and_logs_the_plan(
        self, db_session: AsyncSession
    ) -> None:
        donor_ref = _campaign_ref(f"donor-{id(db_session)}")
        receiver_ref = _campaign_ref(f"receiver-{id(db_session)}")
        business_id = await seed_entity(db_session, donor_ref)
        await _seed_second_campaign(db_session, business_id=business_id, entity_ref=receiver_ref)
        plan = AllocationPlan(
            plan_id=AllocationPlanId.new(),
            business_id=BusinessId(business_id),
            donor_step=AllocationStep(
                entity_ref=donor_ref,
                direction=AllocationDirection.DECREASE,
                current_daily_spend=EconomicsMoney.of("150"),
                proposed_daily_spend=EconomicsMoney.of("120"),
                step_pct=0.20,
                cadence_days=3,
            ),
            receiver_step=AllocationStep(
                entity_ref=receiver_ref,
                direction=AllocationDirection.INCREASE,
                current_daily_spend=EconomicsMoney.of("111"),
                proposed_daily_spend=EconomicsMoney.of("133"),
                step_pct=0.20,
                cadence_days=7,
            ),
            expected_contribution_delta=EconomicsMoney.of("56.60"),
            created_at=_FIXED_NOW,
        )

        port = SqlReallocationProposalPort(db_session, FixedClock(_FIXED_NOW))
        refs = await port.raise_reallocation_proposals(
            business_id=BusinessId(business_id), plan=plan
        )

        assert refs.decrease_proposal_id != refs.increase_proposal_id
        rows = (
            await db_session.execute(
                text(
                    "SELECT id, classification FROM proposals WHERE id = ANY(:ids)"
                ),
                {"ids": [refs.decrease_proposal_id, refs.increase_proposal_id]},
            )
        ).all()
        assert len(rows) == 2
        plan_row = (
            await db_session.execute(
                text(
                    "SELECT decrease_proposal_id, increase_proposal_id FROM allocation_plans "
                    "WHERE id = :id"
                ),
                {"id": plan.plan_id.value},
            )
        ).one()
        assert str(plan_row.decrease_proposal_id) == refs.decrease_proposal_id
        assert str(plan_row.increase_proposal_id) == refs.increase_proposal_id


def _parse_business_id(business_id: str) -> uuid.UUID:
    return uuid.UUID(business_id)


def _optimization_app(database_url: str) -> tuple[FastAPI, object]:
    engine = create_async_engine(database_url, pool_pre_ping=True)
    session_factory = async_sessionmaker(engine, expire_on_commit=False)
    app = FastAPI()
    app.dependency_overrides[require_business_access] = _parse_business_id
    app.include_router(
        build_optimization_router_over_sql(session_factory, FixedClock(_FIXED_NOW))
    )
    return app, engine


async def test_optimization_router_over_sql_returns_404_for_missing_marginal_estimate(
    database_url: str,
) -> None:
    app, engine = _optimization_app(database_url)
    transport = httpx.ASGITransport(app=app)
    try:
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
            response = await client.get(
                "/api/v1/optimization/marginal-roas",
                params={
                    "business_id": str(uuid.uuid4()),
                    "entity_ref": "google:campaign:doesnotexist",
                },
            )
        assert response.status_code == 404
    finally:
        await engine.dispose()


async def test_optimization_router_over_sql_reallocation_plan_returns_404_without_candidates(
    database_url: str,
) -> None:
    """Mismo router, mutacion real: sin candidatos elegibles para ese
    negocio (recien creado, sin `marginal_estimates`), `NO_CANDIDATES`."""
    app, engine = _optimization_app(database_url)
    transport = httpx.ASGITransport(app=app)
    try:
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
            response = await client.post(
                "/api/v1/optimization/reallocation-plan",
                params={"business_id": str(uuid.uuid4())},
            )
        assert response.status_code == 404
        assert response.json()["detail"] == "NO_CANDIDATES"
    finally:
        await engine.dispose()

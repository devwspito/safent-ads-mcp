"""T173 (tasks.md): humo de asignacion por ROAS marginal (`quickstart.md
§9`, profitability-engine.md §3) sobre Postgres real, mismo criterio que
`tests/integration/qa/test_us1_read_the_truth.py`: solo dobla el borde de
plataforma cuando hace falta (aqui, ninguno -- `ProposeReallocationPlan` es
lectura+propuesta, nunca toca el broker, FR-12).

`marginal_estimates` es materializada (0017_optimization: "el
`MaintenanceCycle` la recalcula"), no hay un banco de contrato que siembre
un caso NUNCA materializado a proposito para probar que un par contaminado
jamas entra al fondo de candidatos -- ese es el hueco que cierra este
fichero, junto al contrato de extremo a extremo que `quickstart.md §9`
promete en prosa: `get_marginal_roas`-like (`list_candidates`) ->
`propose_reallocation_plan` -> dos propuestas separadas -> nada ejecuta
hasta aprobar.

`test_marginal.py::test_structural_change_is_vetoed` ya prueba el veto de
dominio (`estimate_marginal_contribution_paired`); este banco NO lo repite
-- prueba la consecuencia de infraestructura: un par vetoed nunca escribe
`marginal_estimates`, y `SqlReallocationCandidateRepository.list_candidates`
por construccion (`INNER JOIN`, sql_reallocation_candidate_repository.py:49)
no puede ver lo que no esta ahi."""

from __future__ import annotations

import uuid
from collections.abc import AsyncIterator
from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine

from safent_ads.optimization.application.propose_reallocation_plan import ProposeReallocationPlan
from safent_ads.optimization.domain.allocation import AllocationDirection
from safent_ads.optimization.infrastructure.sql_reallocation_candidate_repository import (
    SqlReallocationCandidateRepository,
)
from safent_ads.optimization.infrastructure.sql_reallocation_proposal_port import (
    SqlReallocationProposalPort,
)
from safent_ads.shared.clock import FixedClock
from safent_ads.shared.ids import BusinessId, EntityRef
from tests.contracts.sql_fixtures import campaign_ref, seed_entity

pytestmark = pytest.mark.integration

_NOW = datetime.fromisoformat("2026-09-09T09:00:00+00:00")
_DONOR_BUDGET_MINOR = 20_000  # 200,00 EUR/dia
_RECEIVER_BUDGET_MINOR = 20_000
_MIN_VIABLE_FLOOR_PCT = Decimal("0.20")  # espejo de _MIN_VIABLE_SPEND_FLOOR_PCT (misma constante)


@dataclass(frozen=True, slots=True)
class _AllocationFixture:
    business_id: uuid.UUID
    donor_ref: EntityRef
    receiver_ref: EntityRef
    contaminated_ref: EntityRef


async def _seed_second_campaign_in_business(
    session: AsyncSession, *, business_id: uuid.UUID, entity_ref: EntityRef
) -> None:
    """Misma forma que `tests.contracts.sql_fixtures.seed_entity`, pero
    cuelga una campana adicional del negocio YA sembrado (necesitamos
    varias campanas activas compitiendo por el mismo euro marginal)."""
    credential_id = uuid.uuid4()
    account_id = uuid.uuid4()
    suffix = uuid.uuid4().hex[:12]
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
                    'Campana adicional de asignacion', 'ACTIVE', :state_hash)
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


async def _mark_eligible_with_budget(
    session: AsyncSession, *, entity_ref: EntityRef, budget_minor: int
) -> None:
    """`SqlReallocationCandidateRepository` exige presupuesto diario
    conocido (`budget_amount_minor IS NOT NULL`) y `LearningGate` exige un
    `learning_state` distinto de `LEARNING`/`UNKNOWN` (el `DEFAULT` de
    `ad_entities`, que `seed_entity` no toca)."""
    await session.execute(
        text(
            """
            UPDATE ad_entities
               SET budget_amount_minor = :budget_minor,
                   budget_currency = 'EUR',
                   budget_kind = 'daily',
                   learning_state = 'NOT_APPLICABLE'
             WHERE entity_ref = :entity_ref
            """
        ),
        {"budget_minor": budget_minor, "entity_ref": str(entity_ref)},
    )


async def _insert_marginal_estimate(
    session: AsyncSession,
    *,
    business_id: uuid.UUID,
    entity_ref: EntityRef,
    value: str,
    ci_low: str,
    ci_high: str,
) -> None:
    await session.execute(
        text(
            """
            INSERT INTO marginal_estimates (business_id, entity_ref, value, ci_low, ci_high,
                                            method, sample_size)
            VALUES (:business_id, :entity_ref, :value, :ci_low, :ci_high, 'paired', 7)
            """
        ),
        {
            "business_id": business_id,
            "entity_ref": str(entity_ref),
            "value": value,
            "ci_low": ci_low,
            "ci_high": ci_high,
        },
    )


@pytest.fixture
async def allocation_fixture(isolated_database_url: str) -> AsyncIterator[_AllocationFixture]:
    """Tres campanas del mismo negocio: donante (mContribution bajo),
    receptora (mContribution alto) y una CONTAMINADA -- elegible (misma
    jerarquia, mismo presupuesto) pero SIN fila en `marginal_estimates`,
    exactamente lo que deja un par vetoed por cambio estructural
    (profitability-engine.md §3: "si lo hay, el par se descarta") -- nunca
    llega a materializarse."""
    donor_ref = campaign_ref(f"donor-{uuid.uuid4().hex[:10]}", platform_value="google")
    receiver_ref = campaign_ref(f"recv-{uuid.uuid4().hex[:10]}", platform_value="google")
    contaminated_ref = campaign_ref(f"cont-{uuid.uuid4().hex[:10]}", platform_value="google")
    engine = create_async_engine(isolated_database_url, pool_pre_ping=True)
    try:
        async with AsyncSession(engine, expire_on_commit=False) as session:
            business_id = await seed_entity(session, donor_ref)
            await _seed_second_campaign_in_business(
                session, business_id=business_id, entity_ref=receiver_ref
            )
            await _seed_second_campaign_in_business(
                session, business_id=business_id, entity_ref=contaminated_ref
            )
            await _mark_eligible_with_budget(
                session, entity_ref=donor_ref, budget_minor=_DONOR_BUDGET_MINOR
            )
            await _mark_eligible_with_budget(
                session, entity_ref=receiver_ref, budget_minor=_RECEIVER_BUDGET_MINOR
            )
            await _mark_eligible_with_budget(
                session, entity_ref=contaminated_ref, budget_minor=_RECEIVER_BUDGET_MINOR
            )
            # Donante: mContribution bajo (0.5, destruye valor bajo 1.0).
            await _insert_marginal_estimate(
                session,
                business_id=business_id,
                entity_ref=donor_ref,
                value="0.5",
                ci_low="0.3",
                ci_high="0.7",
            )
            # Receptora: mContribution alto (3.0) -- IC no se solapa con el
            # del donante en el sentido erroneo (donor.ci_low < receiver.ci_high).
            await _insert_marginal_estimate(
                session,
                business_id=business_id,
                entity_ref=receiver_ref,
                value="3.0",
                ci_low="2.5",
                ci_high="3.5",
            )
            # `contaminated_ref` NUNCA recibe fila en `marginal_estimates`:
            # esa es la simulacion del par descartado por cambio estructural.
            await session.commit()
        yield _AllocationFixture(
            business_id=business_id,
            donor_ref=donor_ref,
            receiver_ref=receiver_ref,
            contaminated_ref=contaminated_ref,
        )
    finally:
        await engine.dispose()


# ---------------------------------------------------------------------------
# quickstart.md §9.1: el par contaminado (sin estimacion materializada)
# nunca entra al fondo de candidatos -- ni como donante, ni como receptor,
# ni contando para el minimo de dos candidatos elegibles.
# ---------------------------------------------------------------------------


async def test_contaminated_pair_never_reaches_the_candidate_pool(
    allocation_fixture: _AllocationFixture, isolated_database_url: str
) -> None:
    fixture = allocation_fixture
    engine = create_async_engine(isolated_database_url, pool_pre_ping=True)
    try:
        async with AsyncSession(engine, expire_on_commit=False) as session:
            candidates = await SqlReallocationCandidateRepository(session).list_candidates(
                business_id=BusinessId(fixture.business_id)
            )
    finally:
        await engine.dispose()

    candidate_refs = {candidate.entity_ref for candidate in candidates}
    assert fixture.donor_ref in candidate_refs
    assert fixture.receiver_ref in candidate_refs
    assert fixture.contaminated_ref not in candidate_refs, (
        "una campana elegible sin estimacion materializada (par contaminado descartado) "
        "nunca debe entrar al fondo de candidatos de asignacion"
    )


# ---------------------------------------------------------------------------
# quickstart.md §9.2-§9.4: `propose_reallocation_plan` (mismo camino que la
# tool MCP `optimization/presentation/mcp_tools.py`) sobre Postgres real ->
# dos propuestas SEPARADAS, bajada nunca por debajo del suelo minimo viable,
# subida siempre `important` (aprobacion humana, FR-12) -- ninguna ejecuta
# por si sola.
# ---------------------------------------------------------------------------


async def test_propose_reallocation_plan_splits_decrease_and_increase_with_approval_gate(
    allocation_fixture: _AllocationFixture, isolated_database_url: str
) -> None:
    fixture = allocation_fixture
    engine = create_async_engine(isolated_database_url, pool_pre_ping=True)
    try:
        async with AsyncSession(engine, expire_on_commit=False) as session:
            clock = FixedClock(_NOW)
            use_case = ProposeReallocationPlan(
                candidates=SqlReallocationCandidateRepository(session),
                proposals=SqlReallocationProposalPort(session, clock),
                clock=clock,
            )
            plan_view = await use_case.execute(business_id=BusinessId(fixture.business_id))
            await session.commit()

            rows = {
                str(row["id"]): row
                for row in (
                    await session.execute(
                        text(
                            "SELECT id, entity_ref, classification, state "
                            "FROM proposals WHERE id = ANY(:ids)"
                        ),
                        {
                            "ids": [
                                plan_view.decrease_proposal_id,
                                plan_view.increase_proposal_id,
                            ]
                        },
                    )
                )
                .mappings()
                .all()
            }
    finally:
        await engine.dispose()

    # Dos propuestas SEPARADAS (FR-12): nunca una decision compuesta.
    assert plan_view.decrease_proposal_id != plan_view.increase_proposal_id
    decrease_row = rows[plan_view.decrease_proposal_id]
    increase_row = rows[plan_view.increase_proposal_id]

    assert decrease_row["entity_ref"] == str(fixture.donor_ref)
    assert decrease_row["classification"] == "routine"
    assert decrease_row["state"] == "pending"

    assert increase_row["entity_ref"] == str(fixture.receiver_ref)
    assert increase_row["classification"] == "important", (
        "toda subida de gasto exige aprobacion humana (FR-12), nunca AUTO"
    )
    assert increase_row["state"] == "pending"

    # Suelo minimo viable: la bajada nunca deja el gasto por debajo del
    # 20% del actual (profitability-engine.md §3, `_build_donor_step`).
    min_viable = (Decimal(_DONOR_BUDGET_MINOR) / 100) * _MIN_VIABLE_FLOOR_PCT
    assert plan_view.donor.proposed_daily_spend.amount >= min_viable
    assert plan_view.donor.direction == AllocationDirection.DECREASE.value
    assert plan_view.receiver.direction == AllocationDirection.INCREASE.value
    assert plan_view.receiver.requires_approval is True
    assert plan_view.donor.requires_approval is False

    # Nada ha llegado a la plataforma: proponer no ejecuta -- ni la bajada
    # AUTO ni la subida con aprobacion tienen todavia un intento de
    # ejecucion (monthly_cap/guardarrailes de verdad, ya cubiertos por
    # `tests/integration/composition/test_write_path_end_to_end.py` sobre
    # el mismo `ExecutionChokepoint`, no se re-prueban aqui).
    engine2 = create_async_engine(isolated_database_url, pool_pre_ping=True)
    try:
        async with AsyncSession(engine2, expire_on_commit=False) as session:
            pending_executions = (
                await session.execute(
                    text(
                        "SELECT count(*) FROM executions WHERE proposal_id = ANY(:ids)"
                    ),
                    {
                        "ids": [
                            plan_view.decrease_proposal_id,
                            plan_view.increase_proposal_id,
                        ]
                    },
                )
            ).scalar_one()
    finally:
        await engine2.dispose()
    assert pending_executions == 0, "proponer una reasignacion nunca debe agendar una ejecucion"

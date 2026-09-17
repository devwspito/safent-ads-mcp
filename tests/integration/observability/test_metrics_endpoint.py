"""T127: `GET /metrics` real, en loopback, tras (1) una vuelta completa de
ciclo de orquestacion y (2) una escritura real contra Postgres -- prueba
de extremo a extremo de que registro de metricas, servidor HTTP y los
choques de instrumentacion (`cycle_step_runner.py`,
`sql_proposal_repository.py::save`) encajan. Las aserciones de nombre/
etiqueta en aislamiento viven en `tests/unit/observability/test_metrics.py`;
aqui solo se comprueba que la tuberia completa entrega esos mismos datos
por HTTP real."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import httpx
import pytest
from prometheus_client.parser import text_string_to_metric_families
from sqlalchemy.ext.asyncio import AsyncSession

from safent_ads.observability import metrics
from safent_ads.observability.server import start_metrics_server
from safent_ads.orchestration.application.per_business_cycle import run_per_business_cycle
from safent_ads.orchestration.testing.fakes import FakeBusinessListing, FakeStep
from safent_ads.proposals.domain.cause import Cause, CauseKey, Evidence
from safent_ads.proposals.domain.classification import Classification
from safent_ads.proposals.domain.money import Money
from safent_ads.proposals.domain.priority import Priority, Urgency
from safent_ads.proposals.domain.proposal import Proposal, ProposedDiff, new_proposal_id
from safent_ads.proposals.infrastructure.sql_proposal_repository import SqlProposalRepository
from safent_ads.shared.clock import FixedClock
from safent_ads.shared.ids import BusinessId, EntityRef, UuidIdGenerator
from tests.contracts.sql_fixtures import campaign_ref, seed_entity

pytestmark = pytest.mark.integration

_NOW = datetime(2026, 9, 9, 12, 0, tzinfo=UTC)
_TEST_CYCLE_NAME = "metrics-endpoint-proof"
_TEST_STEP_NAME = "metrics-endpoint-step"


def _proposal(*, business_id: BusinessId, entity_ref: EntityRef) -> Proposal:
    diff = ProposedDiff.build(
        entity_ref=entity_ref,
        parameter="daily_budget",
        before=Money.of("100"),
        after=Money.of("70"),
    )
    return Proposal.raise_proposal(
        proposal_id=new_proposal_id(),
        business_id=business_id,
        diff=diff,
        classification=Classification.ROUTINE,
        cause=Cause(text="CPL sobre objetivo en 7D", signal_id=None, rule_id=None),
        cause_key=CauseKey(entity_ref=entity_ref, rule_id="none", cause_type="cost_per_lead_high"),
        evidence=(Evidence(metric="cpl", actual=41.2, target=28.0, window_preset="7D"),),
        estimated_impact=Money.of("310"),
        priority=Priority(urgency=Urgency.RECOMMENDED),
        now=_NOW,
        expires_at=_NOW + timedelta(hours=72),
    )


async def test_metrics_endpoint_reports_a_finished_cycle_and_a_saved_proposal(
    db_session: AsyncSession,
) -> None:
    cycles_before = metrics.REGISTRY.get_sample_value(
        "ads_orchestration_cycles_total", {"cycle": _TEST_CYCLE_NAME, "outcome": "ok"}
    )
    steps_before = metrics.REGISTRY.get_sample_value(
        "ads_orchestration_steps_total", {"step": _TEST_STEP_NAME, "outcome": "success"}
    )

    step = FakeStep()
    await run_per_business_cycle(
        cycle_name=_TEST_CYCLE_NAME,
        step_name=_TEST_STEP_NAME,
        operation=step.run,
        businesses=FakeBusinessListing([BusinessId.new()]),
        clock=FixedClock(_NOW),
        id_generator=UuidIdGenerator(),
        cycle_id=None,
    )

    entity_ref = campaign_ref("metrics-endpoint", platform_value="google")
    business_id = BusinessId(await seed_entity(db_session, entity_ref))
    proposal = _proposal(business_id=business_id, entity_ref=entity_ref)
    proposals_before = metrics.REGISTRY.get_sample_value(
        "ads_proposals_saved_total", {"state": proposal.state.value}
    )
    await SqlProposalRepository(db_session).save(proposal)

    server, thread = start_metrics_server(0)
    try:
        port = server.server_address[1]
        response = httpx.get(f"http://127.0.0.1:{port}/metrics", timeout=5.0)
    finally:
        server.shutdown()
        thread.join(timeout=5.0)

    assert response.status_code == 200
    samples = {
        (sample.name, tuple(sorted(sample.labels.items()))): sample.value
        for family in text_string_to_metric_families(response.text)
        for sample in family.samples
    }
    assert samples[
        ("ads_orchestration_cycles_total", (("cycle", _TEST_CYCLE_NAME), ("outcome", "ok")))
    ] == (cycles_before or 0) + 1
    assert samples[
        ("ads_orchestration_steps_total", (("outcome", "success"), ("step", _TEST_STEP_NAME)))
    ] == (steps_before or 0) + 1
    assert samples[
        ("ads_proposals_saved_total", (("state", proposal.state.value),))
    ] == (proposals_before or 0) + 1

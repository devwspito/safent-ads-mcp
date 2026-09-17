"""Adaptadores SQL de T201 (profitability-engine.md §4, contracts/mcp-tools.md
P2) contra Postgres real: `SqlExperimentProposalPort`, `SqlExperimentRepository`
-- y `ProposeExperiment`/`GetExperimentStatus` de punta a punta, verificando
que la propuesta nace `pending`/`important` (nunca autonoma)."""

from __future__ import annotations

import uuid
from datetime import UTC, datetime

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from safent_ads.optimization.application.get_experiment_status import GetExperimentStatus
from safent_ads.optimization.application.propose_experiment import (
    ProposeExperiment,
    ProposeExperimentRequest,
)
from safent_ads.optimization.domain.identifiers import ExperimentId
from safent_ads.optimization.infrastructure.sql_experiment_proposal_port import (
    SqlExperimentProposalPort,
)
from safent_ads.optimization.infrastructure.sql_experiment_repository import (
    SqlExperimentRepository,
)
from safent_ads.shared.clock import FixedClock
from safent_ads.shared.ids import BusinessId
from tests.contracts.sql_fixtures import campaign_ref, seed_entity

pytestmark = pytest.mark.integration

_NOW = datetime(2026, 9, 9, 12, 0, tzinfo=UTC)


class TestProposeExperimentEndToEnd:
    async def test_creates_a_pending_important_proposal_and_a_draft_experiment(
        self, db_session: AsyncSession
    ) -> None:
        entity_ref = campaign_ref(f"exp-{id(db_session)}")
        business_id = await seed_entity(db_session, entity_ref)

        use_case = ProposeExperiment(
            proposals=SqlExperimentProposalPort(db_session, FixedClock(_NOW)),
            experiments=SqlExperimentRepository(db_session),
            clock=FixedClock(_NOW),
        )

        view = await use_case.execute(
            ProposeExperimentRequest(
                business_id=BusinessId(business_id),
                entity_ref=entity_ref,
                hypothesis="Meta generica supera a Google marca en CPL",
                metric="click_to_lead",
                baseline_rate=0.08,
                relative_mde=0.25,
                available_units_per_arm_per_week=1200,
            )
        )

        assert view.state == "draft"
        proposal_row = (
            await db_session.execute(
                text("SELECT state, classification, parameter FROM proposals WHERE id = :id"),
                {"id": uuid.UUID(view.proposal_id)},
            )
        ).one()
        assert proposal_row.state == "pending"
        assert proposal_row.classification == "important"
        assert proposal_row.parameter == "experiment_design"

        status = await GetExperimentStatus(SqlExperimentRepository(db_session)).execute(
            experiment_id=ExperimentId.parse(view.experiment_id)
        )
        assert status.hypothesis == "Meta generica supera a Google marca en CPL"
        assert status.proposal_id == view.proposal_id
        assert status.sample_per_arm > 0

"""Journey 3: `propose_budget_change`/`propose_pause` through the real MCP
transport, owner approval/rejection over the real REST router, execution
against the real broker (`GoogleAdsAdapter` with the SDK's search client
replaced by an in-memory double, contracts/platform-port.md), and undo
within grace -- same broker/approval wiring `tests/e2e/test_us2_defensive_
autonomy.py`/`test_us3_approve_without_fatigue.py` already prove end to
end; this bank exercises it from the two optimisation-proposal tools
specifically, which those banks do not call."""

from __future__ import annotations

import uuid
from collections.abc import AsyncIterator
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

import httpx
import pytest
from fastapi import FastAPI
from sqlalchemy import text

from safent_ads.composition.api import _handle_api_error
from safent_ads.composition.container import Container
from safent_ads.composition.execution_rest import build_execution_router
from safent_ads.execution.domain.execution_attempt import ExecutionStatus
from safent_ads.execution.domain.guardrails import GuardrailScope, ScopeKind
from safent_ads.iam.presentation.errors import ApiError
from safent_ads.mcp.application.caller_scope import Permission
from safent_ads.shared.clock import FixedClock
from safent_ads.shared.ids import EntityRef
from tests.contracts.execution.conftest import NOW, GuardrailLimits, seed_guardrails
from tests.contracts.sql_fixtures import campaign_ref, seed_entity
from tests.e2e.journeys.conftest import _FixedScopeResolver, mcp_session, person_scope
from tests.integration.composition.conftest import (
    authenticated_session as authenticated_session,  # noqa: PLC0414 - fixture re-export
)
from tests.integration.composition.test_write_path_end_to_end import (
    _APPROVAL_SEED_B64,
    _campaign_row,
    _campaign_state_hash,
    _FakeGoogleSearchClient,
    _running_broker,
)
from tests.unit.composition.factories import build_api_settings

pytestmark = pytest.mark.integration

_CUSTOMER_ID = "9500000001"
_SET_DAILY_BUDGET = text(
    "UPDATE ad_entities SET budget_amount_minor = 10000, budget_currency = 'EUR', "
    "budget_kind = 'daily' WHERE entity_ref = :entity_ref"
)
_SET_STATE_HASH = text(
    "UPDATE ad_entities SET platform_state_hash = :hash WHERE entity_ref = :entity_ref"
)


@dataclass(frozen=True, slots=True)
class _Rig:
    container: Container
    business_id: uuid.UUID
    entity_ref: EntityRef
    search_client: _FakeGoogleSearchClient
    client: httpx.AsyncClient


@pytest.fixture
async def rig(
    isolated_database_url: str, authenticated_session, tmp_path: Path
) -> AsyncIterator[_Rig]:
    """One fake campaign with a real daily budget, guardrails wide enough
    to never block, and a real broker socket -- shared setup so each test
    body below is only the behaviour under test."""
    entity_ref = campaign_ref(f"customers/{_CUSTOMER_ID}/campaigns/1", platform_value="google")
    row = _campaign_row(customer_id=_CUSTOMER_ID, campaign_id="1", amount_micros=100_000_000)
    search_client = _FakeGoogleSearchClient(row)
    async with _running_broker(
        tmp_path, search_client=search_client, customer_id=_CUSTOMER_ID
    ) as socket_path:
        settings = build_api_settings(
            database_url=isolated_database_url,
            broker_socket_path=str(socket_path),
            approval_signing_key=_APPROVAL_SEED_B64,
        )
        container = Container.build(settings)
        container.clock = FixedClock(NOW)
        try:
            async with container.session_factory() as session:
                business_id = await seed_entity(session, entity_ref)
                await session.execute(_SET_DAILY_BUDGET, {"entity_ref": str(entity_ref)})
                # `propose_budget_change` reads the CURRENT `platform_state_hash`
                # from `ad_entities` as `expected_state_hash`; the broker
                # compares that, at execution time, against the live hash of
                # `row` -- they must match or the chokepoint reports drift.
                await session.execute(
                    _SET_STATE_HASH,
                    {"entity_ref": str(entity_ref), "hash": _campaign_state_hash(row)},
                )
                await seed_guardrails(
                    session,
                    scope=GuardrailScope(kind=ScopeKind.ENTITY, ref=str(entity_ref)),
                    limits=GuardrailLimits(),
                    level="business",
                )
                await session.commit()

            app = FastAPI()
            app.state.container = container
            app.add_exception_handler(ApiError, _handle_api_error)
            app.include_router(build_execution_router(container))
            async with httpx.AsyncClient(
                transport=httpx.ASGITransport(app=app),
                base_url="http://test",
                cookies=authenticated_session.cookies,
            ) as client:
                yield _Rig(container, business_id, entity_ref, search_client, client)
        finally:
            await container.aclose()


async def _propose(rig: _Rig, tool: str, extra: dict) -> dict:
    resolver = _FixedScopeResolver(person_scope(Permission.PROPOSE, business_id=rig.business_id))
    async with mcp_session(rig.container, resolver) as session:
        reply = await session.call_tool(
            tool,
            {
                "args": {
                    "business_id": str(rig.business_id),
                    "entity_ref": str(rig.entity_ref),
                    "evidence": [],
                    "urgency": "recommended",
                    **extra,
                }
            },
        )
    assert reply.is_error is False, reply.content
    return reply.structured_content["result"]


async def _run_chokepoint(rig: _Rig) -> ExecutionStatus:
    async with rig.container.session_factory() as session:
        outcome = await rig.container.build_execution_use_cases(session).chokepoint.run_once()
        await session.commit()
    return outcome


async def test_optimisation_journey_propose_approve_reject_execute_undo(rig: _Rig) -> None:
    # `BUDGET_INCREASE` is always `Classification.IMPORTANT` (data-model.md
    # invariante 4) and never gets an undo grace window (`UndoGracePolicy`);
    # a decrease is `ROUTINE`, the case "undo within grace" needs.
    budget_change = await _propose(
        rig,
        "propose_budget_change",
        {
            "new_daily_budget_amount": "70",
            "new_daily_budget_currency": "EUR",
            "cause": {"text": "CPL 3 dias seguidos por encima del objetivo"},
        },
    )
    pause = await _propose(
        rig,
        "propose_pause",
        {"cause": {"text": "Sin conversiones en 5 dias a 3x el CPL objetivo"}},
    )
    # Both land as plain pending proposals (`cause.text` above is free
    # Spanish prose, no rule/jargon code -- `write_handlers.py` forwards it
    # verbatim as `Cause.text`, never re-encoding it).
    assert budget_change["estado"] == "pending"
    assert pause["estado"] == "pending"

    approval = await rig.client.post(
        f"/api/v1/proposals/{budget_change['proposal_id']}/approve",
        json={"diff_hash": budget_change["diff_hash"]},
    )
    assert approval.status_code == 200, approval.text
    rejection = await rig.client.post(
        f"/api/v1/proposals/{pause['proposal_id']}/reject", json={"diff_hash": pause["diff_hash"]}
    )
    assert rejection.status_code == 200 and rejection.json()["state"] == "rejected"

    scheduled_at = datetime.fromisoformat(approval.json()["execution_scheduled_at"])
    rig.container.clock.advance_to(scheduled_at)  # type: ignore[attr-defined]
    assert await _run_chokepoint(rig) is ExecutionStatus.EXECUTED
    budget_resource_name = f"customers/{_CUSTOMER_ID}/campaignBudgets/1"
    assert rig.search_client.budget_mutations == [
        (_CUSTOMER_ID, budget_resource_name, 70_000_000)
    ]

    undo = await rig.client.post(
        "/api/v1/executions/undo", json={"execution_ids": [approval.json()["execution_id"]]}
    )
    assert undo.status_code == 200
    assert undo.json()["results"][0]["ok"] is True

    # The restore lands scheduled with grace=0: a second chokepoint tick
    # claims and executes it (same two-tick shape as `test_us2_defensive_
    # autonomy.py::test_undo_within_grace_restores_the_original_budget`).
    # The restore diff (70 -> 100) also exceeds `GuardrailLimits().max_step_
    # pct` (0.30): the evaluator clamps it to 70 * 1.30 = 91 EUR rather
    # than blocking the restore outright (`effective_diff`).
    assert await _run_chokepoint(rig) is ExecutionStatus.EXECUTED
    assert rig.search_client.budget_mutations[-1] == (
        _CUSTOMER_ID,
        budget_resource_name,
        91_000_000,
    )

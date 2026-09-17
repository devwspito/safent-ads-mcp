"""The actual owner API edits the signed plan, never merely a display string."""

from copy import deepcopy

import pytest
from sqlalchemy import text
from tests.contracts.execution.conftest import NOW
from tests.integration.composition.test_proposal_admin_rest import _client
from tests.integration.execution.test_campaign_creation_path import seed_creations
from tests.integration.execution.test_chokepoint_budget_race_regression import (
    _ControlledPlatform,
    _run,
)
from tests.integration.execution.test_chokepoint_budget_race_regression import (
    isolated_database_url as isolated_database_url,  # noqa: PLC0414 - fixture re-export
)
from tests.unit.composition.factories import build_api_settings

from safent_ads.composition.container import Container
from safent_ads.execution.domain.execution_attempt import ExecutionStatus
from safent_ads.proposals.presentation.panel_read import proposal_detail
from safent_ads.shared.clock import FixedClock

pytestmark = pytest.mark.integration


async def test_get_patch_cas_preserves_plan_invalidates_signature_and_blocks_old_queue(
    isolated_database_url,
    authenticated_session,
):
    container = Container.build(build_api_settings(database_url=isolated_database_url))
    container.clock = FixedClock(NOW)
    platform = _ControlledPlatform()
    container.ads_platform_port = platform
    try:
        first, _ = await seed_creations(container)
        async with container.session_factory() as session:
            business = (
                await session.execute(
                    text("SELECT business_id FROM proposals WHERE id=:id"), {"id": str(first)}
                )
            ).scalar_one()
            detail = await proposal_detail(session, str(business), str(first))
            signature_before = (
                await session.execute(
                    text("SELECT signature FROM approvals WHERE proposal_id=:id"),
                    {"id": str(first)},
                )
            ).scalar_one()
        assert detail["action_kind"] == "create_campaign"
        assert detail["creation_plan_error"] is None
        assert detail["requires_expansion"] is True
        plan = deepcopy(detail["creation_plan"])
        plan["daily_budget"]["amount"] = "25.00"
        async with _client(container, authenticated_session.cookies) as client:
            endpoint = f"/api/v1/proposals/{first}"
            body = {"diff_hash": detail["diff"]["diff_hash"], "creation_plan": plan}
            response = await client.patch(endpoint, json=body)
            assert response.status_code == 200, response.text
            assert response.json()["diff_hash"] != detail["diff"]["diff_hash"]
            stale = await client.patch(endpoint, json=body)
            assert stale.status_code == 409
            assert stale.json()["error"]["code"] == "DIFF_CHANGED"
        async with container.session_factory() as session:
            updated = await proposal_detail(session, str(business), str(first))
            assert updated["creation_plan"] == plan
            assert updated["state"] == "pending"
            assert (
                await session.execute(
                    text("SELECT signature FROM approvals WHERE proposal_id=:id"),
                    {"id": str(first)},
                )
            ).scalar_one() == signature_before
        assert await _run(container, first) == ExecutionStatus.FAILED
        assert platform.mutations == []
    finally:
        await container.aclose()


async def test_unknown_creation_cannot_be_edited(isolated_database_url, authenticated_session):
    container = Container.build(build_api_settings(database_url=isolated_database_url))
    container.clock = FixedClock(NOW)
    container.ads_platform_port = _ControlledPlatform(fail_first=True)
    try:
        first, _ = await seed_creations(container)
        assert await _run(container, first) == ExecutionStatus.UNKNOWN
        async with _client(container, authenticated_session.cookies) as client:
            response = await client.patch(
                f"/api/v1/proposals/{first}", json={"diff_hash": "a" * 64, "creation_plan": {}}
            )
        assert response.status_code == 409
        assert response.json()["error"]["code"] == "EXECUTION_UNRESOLVED"
    finally:
        await container.aclose()

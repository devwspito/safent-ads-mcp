"""Real PostgreSQL: no fake leases, approvals, foreign keys or draft persistence."""

import asyncio
import json
from unittest.mock import AsyncMock
from uuid import UUID, uuid4

import httpx
import pytest
from fastapi import FastAPI
from sqlalchemy import text
from tests.integration.composition.test_offerings_rest import (
    container as container,  # noqa: PLC0414
)
from tests.integration.composition.test_offerings_rest import (
    two_businesses as two_businesses,  # noqa: PLC0414
)

from safent_ads.composition.api import CsrfMiddleware, _handle_api_error
from safent_ads.iam.presentation.errors import ApiError
from safent_ads.launches.approval import LaunchApprovalStore
from safent_ads.mcp.application.caller_scope import CallerScope, Permission
from safent_ads.mcp.presentation.runtime_tools import build_runtime_tools
from safent_ads.opportunities.domain.campaign_draft import DraftError, DraftFields
from safent_ads.opportunities.infrastructure.campaign_drafts_sql import CampaignDraftStore
from safent_ads.runtime.connections import RuntimeConnections
from safent_ads.runtime.contracts import RuntimeResult
from safent_ads.runtime.rest import build_runtime_router
from safent_ads.runtime.store import RuntimeJobError, RuntimeJobStore, enqueue_job

pytestmark = pytest.mark.integration


def store(container):
    return RuntimeJobStore(
        container.session_factory, CampaignDraftStore(container.session_factory, container.clock)
    )


def plan(revision="a" * 64):
    return {
        "slug": "opening",
        "revision": revision,
        "title": "Opening",
        "documents": [],
        "blockers": ["Confirm opening time"],
        "video_slots": [],
    }


async def enqueue(container, business, revision="a" * 64):
    async with container.session_factory.begin() as session:
        return await enqueue_job(session, business, plan(revision))


def result(**changes):
    return RuntimeResult(
        outcome="blocked",
        summary="Draft saved; information needed",
        blockers=["Confirm budget"],
        campaign=DraftFields(title="Opening", **changes),
    )


async def test_approval_atomically_enqueues_and_deduplicates(
    container, two_businesses, authenticated_session
):
    business = str(two_businesses.business_a)
    approvals = LaunchApprovalStore(container.session_factory)
    for _ in range(2):
        await approvals.approve(
            business, "opening", "a" * 64, authenticated_session.owner_id, plan=plan()
        )
    jobs = (await store(container).list(business))["items"]
    assert len(jobs) == 1 and jobs[0]["state"] == "queued"
    assert "lease_hash" not in jobs[0] and "holder" not in jobs[0]
    assert (await approvals.status(business, "opening", "a" * 64))["approved"]
    async with container.session_factory() as session:
        for table in ("proposals", "executions"):
            count = await session.scalar(
                text(f"SELECT count(*) FROM {table} WHERE business_id=:b"),  # noqa: S608 - closed tuple
                {"b": two_businesses.business_a},
            )
            assert count == 0


async def test_concurrent_claim_persists_one_draft_and_ack_is_idempotent(container, two_businesses):
    business, jobs = str(two_businesses.business_a), store(container)
    created = await enqueue(container, business)
    claims = await asyncio.gather(
        jobs.claim(business, "worker-a"), jobs.claim(business, "worker-b")
    )
    assert sum(item["job"] is not None for item in claims) == 1
    index = next(i for i, value in enumerate(claims) if value["job"])
    claimed, holder = claims[index]["job"], ("worker-a", "worker-b")[index]
    assert claimed["id"] == created["id"]
    report = result()
    first = await jobs.report(business, claimed["id"], holder, claimed["lease_token"], report)
    second = await jobs.report(business, claimed["id"], holder, claimed["lease_token"], report)
    assert first == second
    drafts = (await jobs.drafts.list(business))["items"]
    assert len(drafts) == 1
    assert drafts[0]["draft_id"] == first["result"]["draft_id"]
    assert drafts[0]["proposal_id"] is None and not drafts[0]["executable"]


async def test_cross_tenant_holder_and_expired_lease_are_fenced(container, two_businesses):
    business, jobs = str(two_businesses.business_a), store(container)
    await enqueue(container, business)
    claimed = (await jobs.claim(business, "first"))["job"]
    with pytest.raises(RuntimeJobError, match="NOT_FOUND"):
        await jobs.get(str(two_businesses.business_b), claimed["id"])
    with pytest.raises(RuntimeJobError, match="LEASE_LOST"):
        await jobs.report(business, claimed["id"], "other", claimed["lease_token"], result())
    async with container.session_factory.begin() as session:
        await session.execute(
            text("UPDATE runtime_jobs SET lease_until=now()-interval '1 second' WHERE id=:id"),
            {"id": UUID(claimed["id"])},
        )
    second = (await jobs.claim(business, "second"))["job"]
    assert second["id"] == claimed["id"] and second["lease_token"] != claimed["lease_token"]
    with pytest.raises(RuntimeJobError, match="LEASE_LOST"):
        await jobs.report(business, claimed["id"], "first", claimed["lease_token"], result())
    await jobs.cancel(business, claimed["id"])
    with pytest.raises(RuntimeJobError):
        await jobs.report(business, second["id"], "second", second["lease_token"], result())


async def test_result_and_draft_rollback_together_on_invalid_refs(container, two_businesses):
    business, jobs = str(two_businesses.business_a), store(container)
    await enqueue(container, business)
    claimed = (await jobs.claim(business, "worker"))["job"]
    with pytest.raises(DraftError, match="NOT_FOUND"):
        await jobs.report(
            business,
            claimed["id"],
            "worker",
            claimed["lease_token"],
            result(offering_id=str(two_businesses.offering_b)),
        )
    assert (await jobs.drafts.list(business))["items"] == []
    assert (await jobs.get(business, claimed["id"]))["state"] == "running"


async def test_retry_requires_fresh_draft_revision_and_never_duplicates(container, two_businesses):
    business, jobs = str(two_businesses.business_a), store(container)
    await enqueue(container, business)
    claimed = (await jobs.claim(business, "first"))["job"]
    first = await jobs.report(business, claimed["id"], "first", claimed["lease_token"], result())
    await jobs.retry(business, claimed["id"])
    claimed = (await jobs.claim(business, "second"))["job"]
    changed = result(notes="Confirmed information")
    with pytest.raises(DraftError, match="CHANGED"):
        await jobs.report(business, claimed["id"], "second", claimed["lease_token"], changed)
    changed.expected_draft_revision = first["result"]["draft_revision"]
    second = await jobs.report(business, claimed["id"], "second", claimed["lease_token"], changed)
    assert first["result"]["draft_id"] == second["result"]["draft_id"]
    assert second["result"]["draft_revision"] == 2


async def test_superseded_or_changed_plan_never_accepts_late_result(container, two_businesses):
    business, jobs = str(two_businesses.business_a), store(container)
    await enqueue(container, business)
    old = (await jobs.claim(business, "first"))["job"]
    await enqueue(container, business, "b" * 64)
    with pytest.raises(RuntimeJobError, match="PLAN_CHANGED"):
        await jobs.report(business, old["id"], "first", old["lease_token"], result())
    jobs.current_revision = lambda _slug, _business: "c" * 64
    assert (await jobs.claim(business, "second"))["job"] is None
    assert all(item["state"] == "cancelled" for item in (await jobs.list(business))["items"])


async def test_three_lost_leases_require_manual_retry(container, two_businesses):
    business, jobs = str(two_businesses.business_a), store(container)
    await enqueue(container, business)
    for _ in range(3):
        claimed = (await jobs.claim(business, "worker"))["job"]
        assert claimed
        async with container.session_factory.begin() as session:
            await session.execute(
                text("UPDATE runtime_jobs SET lease_until=now()-interval '1 second' WHERE id=:id"),
                {"id": UUID(claimed["id"])},
            )
    assert (await jobs.claim(business, "worker"))["job"] is None
    assert (await jobs.get(business, claimed["id"]))["state"] == "failed"


async def test_bridge_revocation_is_tenant_bound_and_stops_running_job(container, two_businesses):
    business, jobs = str(two_businesses.business_a), store(container)
    connections = RuntimeConnections(container.session_factory)
    credential = await connections.create(business, "Local", "codex")
    resolved, holder = await connections.authenticate(credential["token"])
    assert resolved == business
    assert credential["token"] not in json.dumps(await connections.list(business))
    with pytest.raises(RuntimeJobError, match="NOT_FOUND"):
        await connections.revoke(str(two_businesses.business_b), credential["id"])
    await enqueue(container, business)
    claimed = (await jobs.claim(business, holder))["job"]
    await connections.revoke(business, credential["id"])
    with pytest.raises(RuntimeJobError, match="INVALID"):
        await connections.authenticate(credential["token"])
    assert (await jobs.get(business, claimed["id"]))["state"] == "failed"


async def test_bearer_bridge_is_not_panel_owner_and_panel_requires_csrf(
    container, two_businesses, authenticated_session
):
    business, jobs = str(two_businesses.business_a), store(container)
    connections = RuntimeConnections(container.session_factory)
    app = FastAPI()
    app.state.container = container
    app.add_exception_handler(ApiError, _handle_api_error)
    app.add_middleware(CsrfMiddleware)
    app.include_router(build_runtime_router(jobs, connections))
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="https://test"
    ) as client:
        assert (await client.post("/runtime/v1/claim")).status_code == 401
        credential = await connections.create(business, "Local", "codex")
        bearer = {"Authorization": "Bearer " + credential["token"]}
        assert (await client.post("/runtime/v1/claim", headers=bearer)).json() == {"job": None}
        # A bridge token cannot mint another token or impersonate an owner.
        response = await client.post(
            "/api/v1/runtime/connections",
            params={"business_id": business},
            json={"runtime": "codex", "label": "bad"},
            headers=bearer,
        )
        assert response.status_code in {401, 403}
        client.cookies.update(authenticated_session.cookies)
        response = await client.post(
            "/api/v1/runtime/connections",
            params={"business_id": business},
            json={"runtime": "codex", "label": "bad"},
        )
        assert response.status_code == 403
        client.cookies.set("ads_csrf", "synthetic")
        response = await client.post(
            "/api/v1/runtime/connections",
            params={"business_id": business},
            json={"runtime": "claude", "label": "Owner"},
            headers={"X-CSRF-Token": "synthetic"},
        )
        assert response.status_code == 200, response.text


async def test_mcp_result_errors_are_structured(container, two_businesses):
    jobs = store(container)
    tool = next(tool for tool in build_runtime_tools(jobs) if tool.name == "get_runtime_job")
    caller = CallerScope(
        "test", frozenset({str(two_businesses.business_a)}), Permission.PROPOSE, "test"
    )
    args = tool.args_model.model_validate(
        {"business_id": str(two_businesses.business_a), "job_id": str(uuid4())}
    )
    with pytest.raises(Exception, match="NOT_FOUND"):
        await tool.handler(args, caller)


async def test_approval_rolls_back_if_enqueue_fails(
    container, two_businesses, authenticated_session, monkeypatch
):
    business = str(two_businesses.business_a)
    monkeypatch.setattr(
        "safent_ads.launches.approval.enqueue_job", AsyncMock(side_effect=RuntimeError("test"))
    )
    approvals = LaunchApprovalStore(container.session_factory)
    with pytest.raises(RuntimeError):
        await approvals.approve(
            business, "opening", "a" * 64, authenticated_session.owner_id, plan=plan()
        )
    assert not (await approvals.status(business, "opening", "a" * 64))["approved"]

"""Isolated QA server: test Postgres, real routers/auth/CSRF, no workers/providers."""

import asyncio
import json
from datetime import UTC, datetime, timedelta

import uvicorn
from fastapi import FastAPI, status
from sqlalchemy import text
from testcontainers.community.postgres import PostgresContainer
from tests.conftest import alembic_upgrade, to_alembic_dsn
from tests.contracts.execution.conftest import NOW
from tests.integration.composition.conftest import authenticated_session
from tests.integration.composition.test_proposal_admin_rest import _client
from tests.integration.composition.test_write_path_end_to_end import _raise_budget_proposal
from tests.integration.execution.test_campaign_creation_path import seed_creations
from tests.unit.composition.factories import build_api_settings

from safent_ads.composition.api import CsrfMiddleware, _handle_api_error
from safent_ads.composition.container import Container
from safent_ads.composition.execution_rest import build_execution_router
from safent_ads.composition.execution_undo_adapter import ContainerSingleExecutionUndoAdapter
from safent_ads.execution.infrastructure.sql_execution_read_port import (
    RequestScopedExecutionReadPort,
)
from safent_ads.execution.presentation.rest import build_execution_read_router
from safent_ads.iam.presentation.errors import ApiError
from safent_ads.iam.presentation.router import build_auth_router
from safent_ads.proposals.domain.proposal import ProposedDiff
from safent_ads.proposals.presentation.panel_read import proposal_detail
from safent_ads.proposals.presentation.rest import build_proposal_admin_router
from safent_ads.shared.clock import FixedClock, SystemClock
from safent_ads.shared.ids import BusinessId


async def serve(dsn):  # noqa: PLR0915 - linear, isolated fixture/server lifecycle
    settings = build_api_settings(database_url=dsn, public_base_url="http://127.0.0.1:5211")
    container = Container.build(settings)
    container.clock = FixedClock(NOW)
    session_fixture = authenticated_session.__wrapped__(dsn)
    owner_session = await anext(session_fixture)
    first, second = await seed_creations(container)
    now = datetime.now(UTC)
    async with container.session_factory() as session:
        await session.execute(
            text("UPDATE proposals SET expires_at=:expiry"), {"expiry": now + timedelta(hours=2)}
        )
        business = str(
            (
                await session.execute(
                    text("SELECT business_id FROM proposals WHERE id=:id"), {"id": str(first)}
                )
            ).scalar_one()
        )
        await session.commit()
    container.clock = SystemClock()
    async with _client(container, owner_session.cookies) as client:
        for proposal_id in (first, second):
            async with container.session_factory() as session:
                detail = await proposal_detail(session, business, str(proposal_id))
            plan = detail["creation_plan"]
            plan["daily_budget"]["amount"] = "21.00"
            response = await client.patch(
                f"/api/v1/proposals/{proposal_id}",
                json={"diff_hash": detail["diff"]["diff_hash"], "creation_plan": plan},
            )
            if response.status_code != status.HTTP_200_OK:
                raise RuntimeError("Could not prepare an isolated pending campaign via PATCH")
    async with container.session_factory() as session:
        cases = container.build_execution_use_cases(session)
        existing = await cases.proposals.get(first)
        invalid = _raise_budget_proposal(BusinessId.parse(business), existing.diff.entity_ref)
        raw = dict(existing.diff.after)
        raw.pop("creation_plan")
        invalid.diff = ProposedDiff.build(
            entity_ref=existing.diff.entity_ref,
            parameter="new_campaign:invalid-qa",
            before=None,
            after=raw,
        )
        invalid.expires_at = now + timedelta(hours=2)
        await cases.proposals.save(invalid)
        await session.commit()
    app = FastAPI()
    app.state.container = container
    app.state.settings = settings
    app.add_exception_handler(ApiError, _handle_api_error)
    app.add_middleware(CsrfMiddleware)
    app.include_router(build_auth_router(settings))
    app.include_router(build_execution_router(container))
    app.include_router(build_proposal_admin_router(container.session_factory, container.clock))
    app.include_router(
        build_execution_read_router(
            RequestScopedExecutionReadPort(container.session_factory),
            ContainerSingleExecutionUndoAdapter(container),
        )
    )

    @app.get("/qa-info")
    async def info():
        return {
            "business_id": business,
            "first_id": str(first),
            "second_id": str(second),
            "invalid_id": str(invalid.proposal_id),
            "isolated": True,
        }

    @app.get("/qa-counts")
    async def counts():
        async with container.session_factory() as session:
            result = await session.execute(
                text(
                    "SELECT (SELECT count(*) FROM approvals) approvals, "
                    "(SELECT count(*) FROM executions) executions"
                )
            )
            return dict(result.mappings().one())

    print(json.dumps({"qa": "ready", "port": 5211, "business_id": business}), flush=True)
    try:
        await uvicorn.Server(
            uvicorn.Config(app, host="127.0.0.1", port=5211, log_level="warning")
        ).serve()
    finally:
        try:
            await session_fixture.aclose()
        finally:
            await container.aclose()


if __name__ == "__main__":
    try:
        with PostgresContainer("postgres:16-alpine") as postgres:
            dsn = to_alembic_dsn(postgres.get_connection_url())
            alembic_upgrade(dsn)
            asyncio.run(serve(dsn))
    except KeyboardInterrupt:
        # Normal operator cancellation still exits the testcontainer context above.
        pass

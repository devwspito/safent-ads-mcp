"""Journey 2: `propose_campaign_draft`/`propose_campaign_from_draft`
(campaign packages) through to the owner's panel and the real broker.
Pins the three real bugs the owner hit in companion 0.2.19 (`hotfix/
0.2.20`); reproductions verified empirically against a live Postgres
before writing these assertions (see this file's commit message).

`test_promoted_draft_appears_in_the_panel_read_api` also pins bug 2 (now
fixed): `proposals.proposed_by` used to be always `NULL` for
campaign-package proposals -- `ProposeCampaignRequest`
(`opportunities/application/propose_campaign.py`) had no `proposed_by`
field and `campaign_draft_tools.py::promote` never threaded `caller_scope`
into `CampaignDraftStore.promote`, unlike `write_handlers.py`'s
`propose_budget_change`/`propose_pause`, which already did."""

from __future__ import annotations

import uuid
from collections.abc import AsyncIterator
from dataclasses import dataclass
from datetime import timedelta
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
from safent_ads.panel.infrastructure.sql_read_model import RequestScopedPanelReadPort
from safent_ads.proposals.infrastructure.sql_proposal_repository import SqlProposalRepository
from safent_ads.shared.clock import FixedClock
from tests.contracts.execution.conftest import NOW, GuardrailLimits, seed_guardrails
from tests.contracts.sql_fixtures import campaign_ref, seed_entity
from tests.e2e.journeys.conftest import _FixedScopeResolver, mcp_session, person_scope
from tests.integration.composition.conftest import (
    authenticated_session as authenticated_session,  # noqa: PLC0414 - fixture re-export
)
from tests.integration.composition.test_write_path_end_to_end import (
    _APPROVAL_SEED_B64,
    _campaign_row,
    _FakeGoogleSearchClient,
    _raise_budget_proposal,
    _running_broker,
)
from tests.unit.composition.factories import build_api_settings
from tests.unit.execution.test_campaign_creation_budget import creation_payload

pytestmark = pytest.mark.integration

_STRUCTURED_FIELDS = {
    "platform": "google",
    "objective": "Vender plazas",
    "duration_days": 7,
    "success_criterion": "20 leads",
    "kill_criterion": "CPL > 60",
    "angle": "Vuelta al cole",
    "targeting_seed": "padres 30-45",
    "geo": "ES",
    "landing_url": "https://example.com/reserve",
    "meta_page_id": "1234567890",
}


async def _seeded_account(container: Container) -> tuple[uuid.UUID, uuid.UUID, str]:
    """Business + one offering + one connected Google account, real rows
    (FKs/generated `account_ref` enforced by Postgres, never a double)."""
    business_id = uuid.uuid4()
    offering_id = uuid.uuid4()
    connection_id = uuid.uuid4()
    owner_id = uuid.uuid4()
    async with container.session_factory() as session:
        await session.execute(
            text(
                "INSERT INTO businesses (id, slug, name, timezone, reference_currency) "
                "VALUES (:id, :slug, 'Negocio campana', 'Europe/Madrid', 'EUR')"
            ),
            {"id": business_id, "slug": f"camp-{business_id.hex[:10]}"},
        )
        await session.execute(
            text(
                "INSERT INTO offerings (id, business_id, code, title, price_amount, "
                "price_currency) VALUES (:id, :business, 'off', 'Oferta', 1200, 'EUR')"
            ),
            {"id": offering_id, "business": business_id},
        )
        await session.execute(
            text("INSERT INTO owners (id, email, password_hash) VALUES (:id, :email, 'x')"),
            {"id": owner_id, "email": f"o-{owner_id.hex[:8]}@x.example"},
        )
        await session.execute(
            text(
                "INSERT INTO platform_connections(id,business_id,owner_id,platform) "
                "VALUES(:id,:business,:owner,'google')"
            ),
            {"id": connection_id, "business": business_id, "owner": owner_id},
        )
        account_ref = (
            await session.execute(
                text(
                    "INSERT INTO platform_accounts(business_id,platform,connection_id,"
                    "external_account_id,currency,timezone,api_tier,status) "
                    "VALUES(:business,'google',:connection,'1234567890','EUR',"
                    "'Europe/Madrid','google_standard','ACTIVE') RETURNING account_ref"
                ),
                {"business": business_id, "connection": connection_id},
            )
        ).scalar_one()
        await session.commit()
    return business_id, offering_id, account_ref


async def _propose_draft(container: Container, business_id: uuid.UUID, changes: dict) -> dict:
    resolver = _FixedScopeResolver(person_scope(Permission.PROPOSE, business_id=business_id))
    async with mcp_session(container, resolver) as session:
        reply = await session.call_tool(
            "propose_campaign_draft",
            {"args": {"business_id": str(business_id), "draft_key": "otono", "changes": changes}},
        )
    assert reply.is_error is False, reply.content
    return reply.structured_content["result"]


async def _promote_draft(container: Container, business_id: uuid.UUID, draft: dict) -> dict:
    resolver = _FixedScopeResolver(person_scope(Permission.PROPOSE, business_id=business_id))
    async with mcp_session(container, resolver) as session:
        reply = await session.call_tool(
            "propose_campaign_from_draft",
            {
                "args": {
                    "business_id": str(business_id),
                    "draft_id": draft["draft_id"],
                    "expected_revision": draft["revision"],
                }
            },
        )
    return reply.structured_content


def _execution_app(container: Container) -> FastAPI:
    app = FastAPI()
    app.state.container = container
    app.add_exception_handler(ApiError, _handle_api_error)
    app.include_router(build_execution_router(container))
    return app


async def test_missing_creation_plan_blocks_conversion_despite_structured_fields(
    container: Container,
) -> None:
    """Bug A (hotfix 0.2.20, now fixed): `creation_plan` is derived from the
    draft's own structured fields (`opportunities/domain/campaign_draft.py::
    default_creation_plan`) instead of being a required input -- a draft with
    every OTHER structured field filled has no missing fields at all, and
    promotion succeeds without the caller ever writing a `creation_plan`."""
    business_id, offering_id, account_ref = await _seeded_account(container)
    changes = {
        **_STRUCTURED_FIELDS,
        "title": "Campana otono",
        "account_ref": account_ref,
        "offering_id": str(offering_id),
        "daily_budget": {"amount": "50.00", "currency": "EUR"},
        "notes": "budget 50 EUR search paused manual cpc EU political no networks search only",
    }
    draft = await _propose_draft(container, business_id, changes)
    assert draft["missing_fields"] == []

    payload = await _promote_draft(container, business_id, draft)
    assert payload["result"]["proposal_id"], payload


async def test_invalid_creation_plan_reports_exact_fields(container: Container) -> None:
    """Bug A (hotfix 0.2.20, now fixed): an explicit but invalid
    `creation_plan` reaches the same `INVALID_ARGUMENTS` envelope bug 3 pins
    (`test_journey_errors_are_precise.py::test_malformed_arguments_reach_
    the_clean_envelope`) -- `campaign_draft_tools.py::
    _validate_explicit_creation_plan_shape` runs inside `DraftSaveArgs`'s
    own `model_validator`, so the SDK's own argument validation (never our
    wrapper) raises it, and `mount.py::_gate_call_tool` translates it before
    it ever becomes a raw pydantic dump."""
    business_id, offering_id, account_ref = await _seeded_account(container)
    bad_plan = {  # missing "native": creation_budget() rejects the key set
        "schema_version": 1,
        "platform": "google",
        "name": "Campana otono",
        "status": "PAUSED",
        "daily_budget": {"amount": "50.00", "currency": "EUR"},
    }
    changes = {
        **_STRUCTURED_FIELDS,
        "title": "Campana otono",
        "account_ref": account_ref,
        "offering_id": str(offering_id),
        "daily_budget": {"amount": "50.00", "currency": "EUR"},
        "creation_plan": bad_plan,
    }
    resolver = _FixedScopeResolver(person_scope(Permission.PROPOSE, business_id=business_id))
    async with mcp_session(container, resolver) as session:
        reply = await session.call_tool(
            "propose_campaign_draft",
            {"args": {"business_id": str(business_id), "draft_key": "malo", "changes": changes}},
        )
    error = reply.structured_content["error"]
    assert error["code"] == "INVALID_ARGUMENTS"
    assert "pydantic.dev" not in error["message"]
    assert len(error["fields"]) == 1
    field_message = error["fields"][0]["message"]
    assert "pydantic.dev" not in field_message
    assert "creation_plan" in field_message
    assert "ejemplo válido" in field_message


async def test_promoted_draft_appears_in_the_panel_read_api(
    container: Container, authenticated_session
) -> None:
    business_id, offering_id, account_ref = await _seeded_account(container)
    plan = creation_payload()["creation_plan"]
    plan["daily_budget"] = {"amount": "50.00", "currency": "EUR"}
    changes = {
        **_STRUCTURED_FIELDS,
        "title": "Campana otono",
        "account_ref": account_ref,
        "offering_id": str(offering_id),
        "daily_budget": {"amount": "50.00", "currency": "EUR"},
        "creation_plan": plan,
    }
    draft = await _propose_draft(container, business_id, changes)
    promoted = (await _promote_draft(container, business_id, draft))["result"]

    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=_execution_app(container)),
        base_url="http://test",
        cookies=authenticated_session.cookies,
    ) as client:
        response = await client.get(
            f"/api/v1/proposals/{promoted['proposal_id']}",
            params={"business_id": str(business_id)},
        )

    assert response.status_code == 200, response.text
    detail = response.json()
    assert detail["action_kind"] == "create_campaign"
    # Bug 2 (hotfix 0.2.20, see module docstring): the `proponer` person who
    # promoted the draft is now threaded through, not lost as NULL.
    assert detail["proposed_by"] == {"kind": "person", "label": None}


@dataclass(frozen=True, slots=True)
class _ExecutedCampaign:
    container: Container
    business_id: uuid.UUID
    account_ref: str


@pytest.fixture
async def executed_campaign(
    isolated_database_url: str, authenticated_session, tmp_path: Path
) -> AsyncIterator[_ExecutedCampaign]:
    """A real budget-change proposal, approved through the owner REST route
    and actually EXECUTED against the real broker -- proves "owner
    approval executes on the fake platform" before the bug-C assertion
    reads it back."""
    customer_id = "9400000002"
    entity_ref = campaign_ref(f"customers/{customer_id}/campaigns/1", platform_value="google")
    row = _campaign_row(customer_id=customer_id, campaign_id="1", amount_micros=70_000_000)
    search_client = _FakeGoogleSearchClient(row)

    async with _running_broker(
        tmp_path, search_client=search_client, customer_id=customer_id
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
                await seed_guardrails(
                    session,
                    scope=GuardrailScope(kind=ScopeKind.ENTITY, ref=str(entity_ref)),
                    limits=GuardrailLimits(),
                    level="business",
                )
                proposal = _raise_budget_proposal(business_id, entity_ref)
                await SqlProposalRepository(session).save(proposal)
                await session.commit()

            async with httpx.AsyncClient(
                transport=httpx.ASGITransport(app=_execution_app(container)),
                base_url="http://test",
                cookies=authenticated_session.cookies,
            ) as client:
                approval = await client.post(
                    f"/api/v1/proposals/{proposal.proposal_id}/approve",
                    json={"diff_hash": proposal.diff.diff_hash},
                )
            assert approval.status_code == 200, approval.text

            container.clock.advance_to(NOW + timedelta(seconds=21))  # type: ignore[attr-defined]
            async with container.session_factory() as session:
                outcome = await container.build_execution_use_cases(session).chokepoint.run_once()
                await session.commit()
            assert outcome is ExecutionStatus.EXECUTED  # owner approval really executes

            async with container.session_factory() as session:
                account_ref = (
                    await session.execute(
                        text("SELECT account_ref FROM platform_accounts WHERE business_id = :b"),
                        {"b": business_id},
                    )
                ).scalar_one()
            yield _ExecutedCampaign(container, business_id, account_ref)
        finally:
            await container.aclose()


@pytest.mark.xfail(
    strict=True,
    reason="hotfix 0.2.20: /portfolio debe usar el account_ref canonico, no el UUID interno",
)
async def test_portfolio_disagrees_with_platform_accounts_on_platform_account_id(
    executed_campaign: _ExecutedCampaign,
) -> None:
    panel_port = RequestScopedPanelReadPort(
        executed_campaign.container.session_factory, executed_campaign.container.clock
    )
    portfolio = await panel_port.get_portfolio(str(executed_campaign.business_id), window="7D")
    assert portfolio.rows[0].platform_account_id == executed_campaign.account_ref


async def test_freshness_is_not_stale_right_after_connecting_with_zero_metrics(
    container: Container,
) -> None:
    """hotfix 0.2.20 (now fixed): `/freshness` no longer reports "never
    ingested" when the account synced minutes ago and simply has no metrics
    yet."""
    business_id, _offering_id, _account_ref = await _seeded_account(container)
    async with container.session_factory() as session:
        await session.execute(
            text(
                "UPDATE platform_accounts SET last_synced_at = now() - interval '5 minutes' "
                "WHERE business_id = :b"
            ),
            {"b": business_id},
        )
        await session.commit()

    panel_port = RequestScopedPanelReadPort(container.session_factory)
    freshness = (await panel_port.get_freshness(str(business_id)))[0]

    assert freshness.is_stale is False

"""`propose_campaign`/`list_opportunities` (tasks.md T114) a traves del
`ToolDispatcher` real: args pydantic -> `OpportunityToolServices` ->
`RequestScoped*` (`opportunities/infrastructure/request_scoped_repositories.py`)
-> Postgres. Misma costura que `test_write_tools.py` prueba para US2/US3."""

from __future__ import annotations

import uuid
from datetime import UTC, datetime

import pytest
from sqlalchemy import text
from tests.unit.composition.factories import build_api_settings

from safent_ads.composition.container import Container
from safent_ads.mcp.application.caller_scope import CallerScope, Permission
from safent_ads.mcp.application.errors import EntityNotFoundError, ToolDispatchError
from safent_ads.mcp.infrastructure.rate_limiter import InMemoryQuota
from safent_ads.mcp.presentation.catalog import build_default_registry
from safent_ads.mcp.presentation.dispatcher import ToolDispatcher
from safent_ads.mcp.presentation.opportunity_tools import OpportunityToolServices
from safent_ads.mcp.presentation.read_model_ports import ReadModelPorts
from safent_ads.opportunities.application.list_opportunities import ListOpportunities
from safent_ads.opportunities.application.propose_campaign import ProposeCampaign
from safent_ads.opportunities.infrastructure.request_scoped_repositories import (
    RequestScopedAccountDailyCap,
    RequestScopedActiveAccountLookup,
    RequestScopedCampaignProposals,
    RequestScopedOfferingExists,
    RequestScopedOpenOpportunities,
)
from safent_ads.shared.clock import FixedClock

pytestmark = pytest.mark.integration

def _caller(business_id: object) -> CallerScope:
    return CallerScope(
        caller_id="test-agent",
        allowed_business_ids=frozenset({str(business_id)}),
        permission=Permission.PROPOSE,
        person_label="Agente de prueba",
    )
_NOW = datetime(2026, 9, 10, 9, 0, tzinfo=UTC)


class _UnusedReadPort:
    def __getattr__(self, _name: str) -> object:
        raise AssertionError("puerto de lectura no usado en este test de escritura")


def _minimal_read_model_ports() -> ReadModelPorts:
    unused = _UnusedReadPort()
    return ReadModelPorts(
        business_directory=unused,  # type: ignore[arg-type]
        portfolio=unused,  # type: ignore[arg-type]
        entity=unused,  # type: ignore[arg-type]
        gaql=unused,  # type: ignore[arg-type]
        signal=unused,  # type: ignore[arg-type]
        rule=unused,  # type: ignore[arg-type]
        proposal=unused,  # type: ignore[arg-type]
        catalog=unused,  # type: ignore[arg-type]
        audit=unused,  # type: ignore[arg-type]
        creative=unused,  # type: ignore[arg-type]
        brand=unused,  # type: ignore[arg-type]
        capability=unused,  # type: ignore[arg-type]
    )


def _opportunity_services(container: Container) -> OpportunityToolServices:
    return OpportunityToolServices(
        propose_campaign=ProposeCampaign(
            offerings=RequestScopedOfferingExists(container.session_factory),
            accounts=RequestScopedActiveAccountLookup(container.session_factory),
            daily_caps=RequestScopedAccountDailyCap(container.session_factory),
            campaign_proposals=RequestScopedCampaignProposals(
                container.session_factory, container.clock
            ),
            clock=container.clock,
        ),
        list_opportunities=ListOpportunities(
            opportunities=RequestScopedOpenOpportunities(container.session_factory)
        ),
    )


async def _seed_business_account_and_offering(container: Container, *, suffix: str) -> str:
    business_id = uuid.uuid4()
    credential_id = uuid.uuid4()
    account_id = uuid.uuid4()
    offering_id = uuid.uuid4()
    async with container.session_factory() as session:
        await session.execute(
            text(
                "INSERT INTO businesses (id, slug, name, timezone, reference_currency) "
                "VALUES (:id, :slug, 'Negocio T114', 'Europe/Madrid', 'EUR')"
            ),
            {"id": business_id, "slug": f"t114-{suffix}"},
        )
        await session.execute(
            text(
                "INSERT INTO credential_refs (id, platform, alias) VALUES (:id, 'google', :alias)"
            ),
            {"id": credential_id, "alias": f"alias-{suffix}"},
        )
        await session.execute(
            text(
                "INSERT INTO platform_accounts (id, business_id, platform, external_account_id, "
                "currency, timezone, api_tier, credential_ref_id, status) "
                "VALUES (:id, :business_id, 'google', :external_account_id, 'EUR', "
                "'Europe/Madrid', 'google_standard', :credential_ref_id, 'ACTIVE')"
            ),
            {
                "id": account_id,
                "business_id": business_id,
                "external_account_id": f"act-{suffix}",
                "credential_ref_id": credential_id,
            },
        )
        await session.execute(
            text(
                "INSERT INTO offerings (id, business_id, code, title, price_amount, "
                "price_currency) VALUES (:id, :business_id, :code, 'Oferta T114', 1200, 'EUR')"
            ),
            {"id": offering_id, "business_id": business_id, "code": f"off-{suffix}"},
        )
        await session.commit()
    return f"{business_id}|{offering_id}"


def _build_dispatcher(container: Container) -> ToolDispatcher:
    registry = build_default_registry(
        _minimal_read_model_ports(),
        container.clock,
        opportunity_services=_opportunity_services(container),
    )
    return ToolDispatcher(registry=registry, quota=InMemoryQuota(clock=container.clock))


def _campaign_args(business_id: str, offering_id: str) -> dict[str, object]:
    return {
        "business_id": business_id,
        "platform": "google",
        "objective": "Cubrir demanda de prueba",
        "offering_id": offering_id,
        "daily_budget_amount": "20.00",
        "duration_days": 7,
        "success_criterion": "CPL bajo objetivo 3 dias seguidos",
        "kill_criterion": "Sin conversiones en 5 dias a 3x el CPL objetivo",
        "angle": "angulo de prueba",
        "targeting_seed": "semilla de prueba",
    }


async def test_propose_campaign_creates_a_pending_proposal_through_the_dispatcher(
    isolated_database_url: str,
) -> None:
    settings = build_api_settings(
        database_url=isolated_database_url,
        broker_socket_path="/tmp/safent-ads-wiring-t114-test/mcp-write.sock",
    )
    container = Container.build(settings)
    container.clock = FixedClock(_NOW)
    business_offering = await _seed_business_account_and_offering(container, suffix="ok")
    business_id, offering_id = business_offering.split("|")
    dispatcher = _build_dispatcher(container)

    envelope = await dispatcher.dispatch(
        "propose_campaign",
        _campaign_args(business_id, offering_id),
        caller_scope=_caller(business_id),
    )

    result = envelope["result"]
    assert result["estado"] == "pending"
    async with container.session_factory() as session:
        row = await session.execute(
            text("SELECT state FROM proposals WHERE id = :id"), {"id": result["proposal_id"]}
        )
        assert row.scalar_one() == "pending"


async def test_propose_campaign_with_unknown_offering_raises_entity_not_found(
    isolated_database_url: str,
) -> None:
    settings = build_api_settings(
        database_url=isolated_database_url,
        broker_socket_path="/tmp/safent-ads-wiring-t114-test/mcp-write-2.sock",
    )
    container = Container.build(settings)
    business_offering = await _seed_business_account_and_offering(container, suffix="bad")
    business_id, _offering_id = business_offering.split("|")
    dispatcher = _build_dispatcher(container)

    with pytest.raises(ToolDispatchError) as excinfo:
        await dispatcher.dispatch(
            "propose_campaign",
            _campaign_args(business_id, str(uuid.uuid4())),
            caller_scope=_caller(business_id),
        )

    assert isinstance(excinfo.value, EntityNotFoundError)


async def test_list_opportunities_returns_the_proposed_campaign(
    isolated_database_url: str,
) -> None:
    settings = build_api_settings(
        database_url=isolated_database_url,
        broker_socket_path="/tmp/safent-ads-wiring-t114-test/mcp-write-3.sock",
    )
    container = Container.build(settings)
    business_offering = await _seed_business_account_and_offering(container, suffix="list")
    business_id, offering_id = business_offering.split("|")
    dispatcher = _build_dispatcher(container)
    await dispatcher.dispatch(
        "propose_campaign",
        _campaign_args(business_id, offering_id),
        caller_scope=_caller(business_id),
    )

    envelope = await dispatcher.dispatch(
        "list_opportunities", {"business_id": business_id}, caller_scope=_caller(business_id)
    )

    opportunities = envelope["result"]
    assert len(opportunities) == 1
    assert opportunities[0]["brief"]["offering_id"] == offering_id

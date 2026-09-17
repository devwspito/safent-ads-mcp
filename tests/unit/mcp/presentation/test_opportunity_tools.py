"""`opportunity_tools.py` (tasks.md T114): registro en el `ToolRegistry`
real, nombres verbo-primero validos, y que `build_default_registry` solo
las anade cuando se le pasan `opportunity_services` (retrocompatible con
el resto de tests que construyen el registro sin ellos)."""

from __future__ import annotations

from datetime import UTC, datetime

import pytest

from safent_ads.mcp.application.errors import ToolValidationError
from safent_ads.mcp.presentation.catalog import build_default_registry
from safent_ads.mcp.presentation.opportunity_tools import (
    OpportunityToolServices,
    ProposeCampaignArgs,
    build_opportunity_tool_definitions,
)
from safent_ads.mcp.presentation.read_model_ports import ReadModelPorts
from safent_ads.mcp.presentation.registry import ToolClass
from safent_ads.opportunities.application.list_opportunities import ListOpportunities
from safent_ads.opportunities.application.propose_campaign import ProposeCampaign
from safent_ads.opportunities.testing.in_memory_repositories import (
    InMemoryAccountDailyCapPort,
    InMemoryActiveAccountLookupPort,
    InMemoryCampaignProposalPort,
    InMemoryOfferingExistsPort,
)
from safent_ads.shared.clock import FixedClock
from tests.unit.opportunities.test_propose_campaign import _BUSINESS_ID, _Harness, _scoped_accounts

_EXPECTED_NAMES = frozenset({"propose_campaign", "list_opportunities"})
_NOW = datetime(2026, 9, 9, tzinfo=UTC)


class _EmptyOpenOpportunityPort:
    async def list_open(self, *, business_id: object) -> tuple[object, ...]:
        del business_id
        return ()


def _services() -> OpportunityToolServices:
    return OpportunityToolServices(
        propose_campaign=ProposeCampaign(
            offerings=InMemoryOfferingExistsPort(),
            accounts=InMemoryActiveAccountLookupPort(),
            daily_caps=InMemoryAccountDailyCapPort(),
            campaign_proposals=InMemoryCampaignProposalPort(),
            clock=FixedClock(_NOW),
        ),
        list_opportunities=ListOpportunities(opportunities=_EmptyOpenOpportunityPort()),
    )


def test_build_opportunity_tool_definitions_registers_the_two_verbs() -> None:
    definitions = build_opportunity_tool_definitions(_services())

    names = {d.name for d in definitions}
    assert names == _EXPECTED_NAMES


def test_only_propose_campaign_is_a_proposal_class_tool() -> None:
    definitions = build_opportunity_tool_definitions(_services())

    by_name = {d.name: d for d in definitions}
    assert by_name["propose_campaign"].tool_class is ToolClass.PROPOSAL
    assert by_name["list_opportunities"].tool_class is ToolClass.READ


def test_default_registry_omits_opportunity_tools_without_services(
    read_model_ports: ReadModelPorts,
) -> None:
    registry = build_default_registry(read_model_ports, FixedClock(_NOW))

    for name in _EXPECTED_NAMES:
        assert registry.get(name) is None


def test_default_registry_includes_opportunity_tools_when_wired(
    read_model_ports: ReadModelPorts,
) -> None:
    registry = build_default_registry(
        read_model_ports, FixedClock(_NOW), opportunity_services=_services()
    )

    for name in _EXPECTED_NAMES:
        assert registry.get(name) is not None


@pytest.mark.parametrize("name", sorted(_EXPECTED_NAMES))
def test_tool_names_pass_the_registry_naming_guard(name: str) -> None:
    # Si el nombre violara la convencion verbo-primero, ToolRegistry ya
    # habria lanzado al construir el registro en los tests de arriba.
    definitions = build_opportunity_tool_definitions(_services())
    assert any(d.name == name for d in definitions)


@pytest.mark.parametrize("explicit", [False, True])
async def test_mcp_campaign_selector_reaches_real_use_case_and_ambiguity_is_validation_error(
    explicit: bool,
) -> None:
    harness = _Harness()
    _, selected = _scoped_accounts(harness)
    services = OpportunityToolServices(harness.use_case, _services().list_opportunities)
    tool = next(
        d for d in build_opportunity_tool_definitions(services) if d.name == "propose_campaign"
    )
    args = ProposeCampaignArgs(
        business_id=str(_BUSINESS_ID.value),
        platform="google",
        account_ref=str(selected) if explicit else None,
        objective="Propuesta revisable",
        offering_id="offering-1",
        daily_budget_amount="20",
        duration_days=7,
        success_criterion="Conversiones",
        kill_criterion="Sin conversiones",
        angle="Oferta",
        targeting_seed="Audiencia",
    )
    if not explicit:
        with pytest.raises(ToolValidationError):
            await tool.handler(args, None)
        assert harness.proposals.accepted == []
    else:
        outcome = await tool.handler(args, None)
        assert outcome.proposal_id
        assert harness.proposals.accepted[0].account_ref == selected

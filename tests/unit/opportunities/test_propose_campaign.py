"""`ProposeCampaign` (tasks.md T114): validado (oferta existe, cuenta
activa, presupuesto dentro de guardarrailes), deduplicado por FR-20."""

from __future__ import annotations

from dataclasses import replace
from datetime import UTC, datetime
from uuid import uuid4

import pytest

from safent_ads.opportunities.application.errors import (
    AmbiguousActiveAccountForPlatformError,
    ChannelTypeNotEnabledError,
    DailyBudgetExceedsCapError,
    NoActiveAccountForPlatformError,
    OfferingNotFoundError,
)
from safent_ads.opportunities.application.propose_campaign import (
    ProposeCampaign,
    ProposeCampaignRequest,
)
from safent_ads.opportunities.domain.campaign_brief import CampaignBrief
from safent_ads.opportunities.testing.in_memory_repositories import (
    InMemoryAccountDailyCapPort,
    InMemoryActiveAccountLookupPort,
    InMemoryCampaignProposalPort,
    InMemoryOfferingExistsPort,
)
from safent_ads.proposals.domain.google_channel_spec import GoogleAdvertisingChannelType
from safent_ads.proposals.domain.money import Money
from safent_ads.shared.clock import FixedClock
from safent_ads.shared.ids import BusinessId, EntityLevel, EntityRef, PlatformCode

_NOW = datetime(2026, 9, 10, tzinfo=UTC)
_BUSINESS_ID = BusinessId.new()
_ACCOUNT_REF = EntityRef(platform=PlatformCode.GOOGLE, level=EntityLevel.ACCOUNT, external_id="1")


def _brief(**overrides: object) -> CampaignBrief:
    defaults: dict[str, object] = {
        "objective": "Cubrir demanda de Producto X",
        "platform": PlatformCode.GOOGLE,
        "offering_id": "offering-1",
        "daily_budget": Money.of("20.00", "EUR"),
        "duration_days": 7,
        "success_criterion": "CPL bajo objetivo 3 dias seguidos",
        "kill_criterion": "Sin conversiones en 5 dias a 3x el CPL objetivo",
        "angle": "angulo",
        "targeting_seed": "producto x",
    }
    defaults.update(overrides)
    return CampaignBrief(**defaults)  # type: ignore[arg-type]


_SEARCH_ONLY = frozenset({GoogleAdvertisingChannelType.SEARCH})


class _Harness:
    def __init__(
        self, enabled_google_channels: frozenset[GoogleAdvertisingChannelType] = _SEARCH_ONLY
    ) -> None:
        self.offerings = InMemoryOfferingExistsPort(existing_offering_ids=frozenset({"offering-1"}))
        self.accounts = InMemoryActiveAccountLookupPort()
        self.accounts.seed(
            business_id=_BUSINESS_ID, platform=PlatformCode.GOOGLE, account_ref=_ACCOUNT_REF
        )
        self.daily_caps = InMemoryAccountDailyCapPort()
        self.proposals = InMemoryCampaignProposalPort()
        self.use_case = ProposeCampaign(
            offerings=self.offerings,
            accounts=self.accounts,
            daily_caps=self.daily_caps,
            campaign_proposals=self.proposals,
            clock=FixedClock(_NOW),
            enabled_google_channels=enabled_google_channels,
        )


async def test_proposes_a_pending_campaign_when_everything_validates() -> None:
    h = _Harness()

    outcome = await h.use_case.execute(
        ProposeCampaignRequest(business_id=_BUSINESS_ID, brief=_brief())
    )

    assert outcome.proposal_id
    assert outcome.classification == "important"
    assert len(h.proposals.accepted) == 1
    accepted = h.proposals.accepted[0]
    assert accepted.account_ref == _ACCOUNT_REF
    assert accepted.candidate_key == f"agent:google:offering-1:{_ACCOUNT_REF}"


async def test_proposed_by_threads_through_to_the_accepted_proposal() -> None:
    """Bug 2 (hotfix 0.2.20): `proposed_by` viajaba hasta aqui para
    `propose_budget_change`/`propose_pause` (`write_handlers.py`) pero
    `ProposeCampaignRequest` no tenia el campo -- se perdia siempre en esta
    frontera para las propuestas de campana (`propose_campaign_from_draft`)."""
    h = _Harness()

    await h.use_case.execute(
        ProposeCampaignRequest(
            business_id=_BUSINESS_ID, brief=_brief(), proposed_by="person:ana"
        )
    )

    assert h.proposals.accepted[0].proposed_by == "person:ana"


async def test_proposed_by_defaults_to_none_when_not_a_person_caller() -> None:
    h = _Harness()

    await h.use_case.execute(ProposeCampaignRequest(business_id=_BUSINESS_ID, brief=_brief()))

    assert h.proposals.accepted[0].proposed_by is None


async def test_calendar_event_id_reuses_the_deterministic_candidate_key() -> None:
    h = _Harness()

    await h.use_case.execute(
        ProposeCampaignRequest(business_id=_BUSINESS_ID, brief=_brief(calendar_event_id="event-1"))
    )

    assert h.proposals.accepted[0].candidate_key == f"calendar_event:event-1:{_ACCOUNT_REF}"


async def test_rejects_an_unknown_offering() -> None:
    h = _Harness()

    with pytest.raises(OfferingNotFoundError):
        await h.use_case.execute(
            ProposeCampaignRequest(business_id=_BUSINESS_ID, brief=_brief(offering_id="unknown"))
        )
    assert h.proposals.accepted == []


async def test_rejects_when_no_active_account_for_platform() -> None:
    h = _Harness()

    with pytest.raises(NoActiveAccountForPlatformError):
        await h.use_case.execute(
            ProposeCampaignRequest(
                business_id=_BUSINESS_ID, brief=_brief(platform=PlatformCode.META)
            )
        )
    assert h.proposals.accepted == []


async def test_rejects_when_budget_exceeds_the_configured_cap() -> None:
    h = _Harness()
    h.daily_caps.seed(account_ref=_ACCOUNT_REF, daily_cap=Money.of("15.00", "EUR"))

    with pytest.raises(DailyBudgetExceedsCapError):
        await h.use_case.execute(ProposeCampaignRequest(business_id=_BUSINESS_ID, brief=_brief()))
    assert h.proposals.accepted == []


async def test_allows_budget_within_the_configured_cap() -> None:
    h = _Harness()
    h.daily_caps.seed(account_ref=_ACCOUNT_REF, daily_cap=Money.of("25.00", "EUR"))

    await h.use_case.execute(ProposeCampaignRequest(business_id=_BUSINESS_ID, brief=_brief()))

    assert len(h.proposals.accepted) == 1


async def test_no_configured_cap_does_not_block_the_minimum_test_budget() -> None:
    """T066: sin `config/caps.yaml` real para cuentas reales todavia --
    ausencia de guardarrail nunca bloquea, el suelo de `CampaignBrief`
    (20 EUR/dia, FR-36 nace PAUSED) es la red de seguridad."""
    h = _Harness()

    await h.use_case.execute(ProposeCampaignRequest(business_id=_BUSINESS_ID, brief=_brief()))

    assert len(h.proposals.accepted) == 1


def _scoped_accounts(h: _Harness) -> tuple[EntityRef, EntityRef]:
    refs = tuple(
        EntityRef(
            PlatformCode.GOOGLE, EntityLevel.ACCOUNT, "same-remote", _BUSINESS_ID.value, uuid4()
        )
        for _ in range(2)
    )
    for ref in refs:
        h.accounts.seed(business_id=_BUSINESS_ID, platform=PlatformCode.GOOGLE, account_ref=ref)
    return refs


async def test_multiple_connections_require_explicit_selection() -> None:
    h = _Harness()
    _scoped_accounts(h)
    with pytest.raises(AmbiguousActiveAccountForPlatformError):
        await h.use_case.execute(ProposeCampaignRequest(business_id=_BUSINESS_ID, brief=_brief()))
    assert h.proposals.accepted == []


async def test_explicit_selection_binds_proposal_and_budget_cap_to_exact_connection() -> None:
    h = _Harness()
    first, second = _scoped_accounts(h)
    h.daily_caps.seed(account_ref=first, daily_cap=Money.of("15.00", "EUR"))
    h.daily_caps.seed(account_ref=second, daily_cap=Money.of("25.00", "EUR"))
    with pytest.raises(DailyBudgetExceedsCapError):
        await h.use_case.execute(
            ProposeCampaignRequest(business_id=_BUSINESS_ID, brief=_brief(), account_ref=first)
        )
    await h.use_case.execute(
        ProposeCampaignRequest(business_id=_BUSINESS_ID, brief=_brief(), account_ref=second)
    )
    assert len(h.proposals.accepted) == 1
    assert h.proposals.accepted[0].account_ref == second


@pytest.mark.parametrize("invalid", ["business", "platform", "connection", "level", "legacy"])
async def test_explicit_invalid_scope_never_falls_back_to_another_account(invalid: str) -> None:
    h = _Harness()
    first, _ = _scoped_accounts(h)
    ref = {
        "business": replace(first, business_id=uuid4()),
        "platform": replace(first, platform=PlatformCode.META),
        "connection": replace(first, connection_id=uuid4()),
        "level": replace(first, level=EntityLevel.CAMPAIGN),
        "legacy": _ACCOUNT_REF,
    }[invalid]
    with pytest.raises(NoActiveAccountForPlatformError):
        await h.use_case.execute(
            ProposeCampaignRequest(business_id=_BUSINESS_ID, brief=_brief(), account_ref=ref)
        )
    assert h.proposals.accepted == []


_PERFORMANCE_MAX_PLAN: dict[str, object] = {
    "schema_version": 1,
    "platform": "google",
    "name": "Reserva de citas",
    "status": "PAUSED",
    "daily_budget": {"amount": "20.00", "currency": "EUR"},
    "native": {
        "advertising_channel_type": "PERFORMANCE_MAX",
        "bidding_strategy": {"kind": "MAXIMIZE_CONVERSIONS"},
        "contains_eu_political_advertising": "DOES_NOT_CONTAIN_EU_POLITICAL_ADVERTISING",
        "conversion_goals": [{"resource_name": "customers/1234567890/conversionActions/1"}],
        "url_expansion_opt_out": True,
        "text_asset_automation_enabled": False,
    },
}


class TestChannelNotEnabled:
    """tasks.md T076 (POLISH): defence in depth at `ProposeCampaign.execute`
    -- the draft promotion path (`CampaignDraftStore.promote`) already
    rejects this at save time, but a draft can sit for days between save
    and promote, and the installation's enabled channels can change in
    that time. `propose_campaign` (`mcp.presentation.opportunity_tools`)
    reaches the same gate for free, since it shares this use case."""

    async def test_performance_max_denied_when_only_search_is_enabled(self) -> None:
        h = _Harness()

        with pytest.raises(ChannelTypeNotEnabledError) as excinfo:
            await h.use_case.execute(
                ProposeCampaignRequest(
                    business_id=_BUSINESS_ID, brief=_brief(creation_plan=_PERFORMANCE_MAX_PLAN)
                )
            )
        assert excinfo.value.channel == "PERFORMANCE_MAX"
        assert h.proposals.accepted == []

    async def test_performance_max_accepted_when_enabled(self) -> None:
        h = _Harness(
            enabled_google_channels=frozenset(
                {GoogleAdvertisingChannelType.SEARCH, GoogleAdvertisingChannelType.PERFORMANCE_MAX}
            )
        )

        outcome = await h.use_case.execute(
            ProposeCampaignRequest(
                business_id=_BUSINESS_ID, brief=_brief(creation_plan=_PERFORMANCE_MAX_PLAN)
            )
        )

        assert outcome.proposal_id
        assert len(h.proposals.accepted) == 1

    async def test_search_stays_allowed_when_only_search_is_enabled(self) -> None:
        h = _Harness()

        outcome = await h.use_case.execute(
            ProposeCampaignRequest(business_id=_BUSINESS_ID, brief=_brief())
        )

        assert outcome.proposal_id

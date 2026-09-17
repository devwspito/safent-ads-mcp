"""`ListOpportunities` (tasks.md T114): lectura trivial sobre el puerto,
mismo patron que `GetCalibrationReport`."""

from __future__ import annotations

from datetime import UTC, datetime

from safent_ads.opportunities.application.list_opportunities import ListOpportunities
from safent_ads.opportunities.application.ports import OpenOpportunityView
from safent_ads.opportunities.domain.campaign_brief import CampaignBrief
from safent_ads.proposals.domain.money import Money
from safent_ads.shared.ids import BusinessId, EntityLevel, EntityRef, PlatformCode

_BUSINESS_ID = BusinessId.new()
_ACCOUNT_REF = EntityRef(platform=PlatformCode.GOOGLE, level=EntityLevel.ACCOUNT, external_id="1")


class _FakeOpenOpportunityPort:
    def __init__(self, views: tuple[OpenOpportunityView, ...]) -> None:
        self._views = views
        self.requested_business_id: BusinessId | None = None

    async def list_open(self, *, business_id: BusinessId) -> tuple[OpenOpportunityView, ...]:
        self.requested_business_id = business_id
        return self._views


def _view(proposal_id: str) -> OpenOpportunityView:
    brief = CampaignBrief(
        objective="Cubrir demanda",
        platform=PlatformCode.GOOGLE,
        offering_id="offering-1",
        daily_budget=Money.of("20.00", "EUR"),
        duration_days=7,
        success_criterion="CPL bajo objetivo",
        kill_criterion="Sin conversiones en 5 dias",
        angle="angulo",
        targeting_seed="semilla",
    )
    return OpenOpportunityView(
        proposal_id=proposal_id,
        state="pending",
        account_ref=_ACCOUNT_REF,
        brief=brief,
        expected_contribution_delta=Money.of("50.00", "EUR"),
        cause_sentence="causa",
        expires_at=datetime(2026, 9, 20, tzinfo=UTC),
    )


async def test_delegates_to_the_port_for_the_given_business() -> None:
    port = _FakeOpenOpportunityPort((_view("p1"), _view("p2")))
    use_case = ListOpportunities(opportunities=port)

    result = await use_case.execute(business_id=_BUSINESS_ID)

    assert [view.proposal_id for view in result] == ["p1", "p2"]
    assert port.requested_business_id == _BUSINESS_ID


async def test_empty_when_nothing_is_open() -> None:
    port = _FakeOpenOpportunityPort(())
    use_case = ListOpportunities(opportunities=port)

    result = await use_case.execute(business_id=_BUSINESS_ID)

    assert result == ()

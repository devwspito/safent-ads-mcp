"""`GenerateOpportunities` (tasks.md T113) contra los dobles en memoria de
`opportunities.testing` -- ranking, presupuesto de atencion (NFR-11) y el
caso honesto de oferta sin economia unitaria todavia."""

from __future__ import annotations

from datetime import UTC, date, datetime

from safent_ads.opportunities.application.generate_opportunities import GenerateOpportunities
from safent_ads.opportunities.application.ports import CalendarEventGap
from safent_ads.opportunities.testing.in_memory_repositories import (
    InMemoryCalendarEventGapPort,
    InMemoryCampaignProposalPort,
    InMemoryDailyCandidateBudgetPort,
    InMemoryOfferingContributionPort,
)
from safent_ads.proposals.domain.money import Money
from safent_ads.shared.ids import BusinessId, EntityLevel, EntityRef, PlatformCode

_BUSINESS_ID = BusinessId.new()
_ACCOUNT_REF = EntityRef(platform=PlatformCode.GOOGLE, level=EntityLevel.ACCOUNT, external_id="1")
_NOW = datetime(2026, 9, 10, 8, 0, tzinfo=UTC)


def _gap(offering_id: str, calendar_event_id: str) -> CalendarEventGap:
    return CalendarEventGap(
        calendar_event_id=calendar_event_id,
        offering_id=offering_id,
        offering_name="Producto de temporada",
        account_ref=_ACCOUNT_REF,
        window_end=date(2026, 9, 20),
        region=None,
    )


def _candidate_key(calendar_event_id: str) -> str:
    return f"calendar_event:{calendar_event_id}:{_ACCOUNT_REF}"


class _FixedClock:
    def now(self) -> datetime:
        return _NOW


class _Harness:
    def __init__(self, *, already_accepted_today: int = 0) -> None:
        self.gaps = InMemoryCalendarEventGapPort()
        self.contribution = InMemoryOfferingContributionPort()
        self.daily_budget = InMemoryDailyCandidateBudgetPort(
            already_accepted_today=already_accepted_today
        )
        self.proposals = InMemoryCampaignProposalPort()
        self.use_case = GenerateOpportunities(
            calendar_event_gaps=self.gaps,
            offering_contribution=self.contribution,
            daily_budget=self.daily_budget,
            campaign_proposals=self.proposals,
            clock=_FixedClock(),
            max_new_candidates_per_day=2,
        )

    def seed_gaps(self, *gaps: CalendarEventGap) -> None:
        self.gaps.seed(business_id=_BUSINESS_ID, gaps=list(gaps))

    def seed_contribution(self, offering_id: str, amount: str) -> None:
        self.contribution.seed(
            offering_id=offering_id, expected_contribution_delta=Money.of(amount, "EUR")
        )


async def test_accepts_candidates_with_economics_within_the_daily_cap() -> None:
    h = _Harness()
    h.seed_gaps(_gap("offering-1", "event-1"))
    h.seed_contribution("offering-1", "300")

    report = await h.use_case.execute(business_id=_BUSINESS_ID)

    assert report.accepted == 1
    assert report.deferred == 0
    assert report.skipped_without_economics == 0
    assert len(h.proposals.accepted) == 1
    accepted = h.proposals.accepted[0]
    assert accepted.candidate_key == _candidate_key("event-1")
    assert accepted.brief.calendar_event_id == "event-1"


async def test_skips_offerings_without_a_unit_economics_profile() -> None:
    h = _Harness()
    h.seed_gaps(_gap("offering-1", "event-1"))
    # Sin seed en `contribution`: economics no tiene perfil todavia.

    report = await h.use_case.execute(business_id=_BUSINESS_ID)

    assert report.accepted == 0
    assert report.skipped_without_economics == 1
    assert h.proposals.accepted == []
    assert h.proposals.deferred == []


async def test_defers_candidates_beyond_the_daily_attention_budget() -> None:
    h = _Harness()
    h.seed_gaps(
        _gap("offering-1", "event-1"), _gap("offering-2", "event-2"), _gap("offering-3", "event-3")
    )
    h.seed_contribution("offering-1", "100")
    h.seed_contribution("offering-2", "300")
    h.seed_contribution("offering-3", "200")

    report = await h.use_case.execute(business_id=_BUSINESS_ID)

    assert report.accepted == 2
    assert report.deferred == 1
    accepted_keys = {p.candidate_key for p in h.proposals.accepted}
    assert accepted_keys == {_candidate_key("event-2"), _candidate_key("event-3")}
    assert h.proposals.deferred[0].candidate_key == _candidate_key("event-1")
    assert h.proposals.deferred[0].postpone_until > _NOW


async def test_already_accepted_today_reduces_remaining_slots() -> None:
    h = _Harness(already_accepted_today=2)
    h.seed_gaps(_gap("offering-1", "event-1"))
    h.seed_contribution("offering-1", "100")

    report = await h.use_case.execute(business_id=_BUSINESS_ID)

    assert report.accepted == 0
    assert report.deferred == 1


async def test_deferred_candidate_with_a_live_equivalent_is_left_untouched() -> None:
    h = _Harness(already_accepted_today=2)
    h.seed_gaps(_gap("offering-1", "event-1"))
    h.seed_contribution("offering-1", "100")
    h.proposals.seed_already_live(_candidate_key("event-1"))

    report = await h.use_case.execute(business_id=_BUSINESS_ID)

    assert report.deferred == 1
    assert h.proposals.deferred == []

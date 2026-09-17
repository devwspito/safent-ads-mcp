"""Factories compartidas para los tests de `proposals`."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from safent_ads.proposals.domain.cause import Cause, CauseKey, Evidence
from safent_ads.proposals.domain.classification import Classification
from safent_ads.proposals.domain.money import Money
from safent_ads.proposals.domain.priority import Priority, Urgency
from safent_ads.proposals.domain.proposal import Proposal, ProposedDiff, new_proposal_id
from safent_ads.shared.ids import BusinessId, EntityLevel, EntityRef, PlatformCode

NOW = datetime(2026, 9, 9, 10, 0, tzinfo=UTC)


def entity_ref(external_id: str = "1234567890") -> EntityRef:
    return EntityRef(PlatformCode.META, EntityLevel.CAMPAIGN, external_id)


def budget_diff(
    before: str = "100", after: str = "70", ref: EntityRef | None = None
) -> ProposedDiff:
    return ProposedDiff.build(
        entity_ref=ref or entity_ref(),
        parameter="daily_budget",
        before=Money.of(before),
        after=Money.of(after),
    )


def cause(text: str = "CPL sobre objetivo en 7D", rule_id: str | None = "M05") -> Cause:
    return Cause(text=text, signal_id=None, rule_id=rule_id)


def cause_key(ref: EntityRef | None = None) -> CauseKey:
    return CauseKey(entity_ref=ref or entity_ref(), rule_id="M05", cause_type="cost_per_lead_high")


def evidence() -> tuple[Evidence, ...]:
    return (Evidence(metric="cpl", actual=41.2, target=28.0, window_preset="7D"),)


def make_proposal(
    *,
    diff: ProposedDiff | None = None,
    classification: Classification = Classification.ROUTINE,
    urgency: Urgency = Urgency.RECOMMENDED,
    now: datetime = NOW,
    ttl_hours: int = 72,
    expected_state_hash: str | None = None,
    expected_contribution_delta: Money | None = None,
) -> Proposal:
    resolved_diff = diff or budget_diff()
    return Proposal.raise_proposal(
        proposal_id=new_proposal_id(),
        business_id=BusinessId.new(),
        diff=resolved_diff,
        classification=classification,
        cause=cause(),
        cause_key=cause_key(resolved_diff.entity_ref),
        evidence=evidence(),
        estimated_impact=Money.of("310"),
        priority=Priority(urgency=urgency),
        now=now,
        expires_at=now + timedelta(hours=ttl_hours),
        expected_state_hash=expected_state_hash,
        expected_contribution_delta=expected_contribution_delta,
    )

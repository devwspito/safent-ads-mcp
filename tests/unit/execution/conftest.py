"""Factories compartidas para los tests de `execution`."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from safent_ads.execution.domain.guardrails import (
    GuardrailChange,
    GuardrailEvaluator,
    GuardrailScope,
    GuardrailSet,
    GuardrailVerdict,
    LedgerSnapshot,
    ScopeKind,
    effective_diff,
    money_pair_from_diff,
)
from safent_ads.proposals.domain.authorization import (
    Authorization,
    AuthorizationChannel,
    AuthorizationKind,
    sign_authorization,
)
from safent_ads.proposals.domain.cause import Cause, CauseKey, Evidence
from safent_ads.proposals.domain.classification import Classification
from safent_ads.proposals.domain.identifiers import AuthorizationId
from safent_ads.proposals.domain.money import Money
from safent_ads.proposals.domain.priority import Priority, Urgency
from safent_ads.proposals.domain.proposal import Proposal, ProposedDiff, new_proposal_id
from safent_ads.proposals.testing.fakes import FakeSignerPort
from safent_ads.shared.ids import BusinessId, EntityLevel, EntityRef, PlatformCode

NOW = datetime(2026, 9, 9, 10, 0, tzinfo=UTC)


def entity_ref(external_id: str = "1234567890") -> EntityRef:
    return EntityRef(PlatformCode.META, EntityLevel.CAMPAIGN, external_id)


def account_scope(ref: str = "meta:account:act_1") -> GuardrailScope:
    return GuardrailScope(kind=ScopeKind.PLATFORM_ACCOUNT, ref=ref)


def entity_scope(ref: EntityRef | None = None) -> GuardrailScope:
    return GuardrailScope(kind=ScopeKind.ENTITY, ref=str(ref or entity_ref()))


def guardrail_set(
    *,
    scope: GuardrailScope | None = None,
    daily_cap: str = "500",
    monthly_cap: str = "10000",
    floor: str = "10",
    ceiling: str = "300",
    max_step_pct: float = 0.30,
    max_changes_per_entity_day: int = 2,
) -> GuardrailSet:
    return GuardrailSet(
        scope=scope or account_scope(),
        daily_cap=Money.of(daily_cap),
        monthly_cap=Money.of(monthly_cap),
        floor=Money.of(floor),
        ceiling=Money.of(ceiling),
        max_step_pct=max_step_pct,
        max_changes_per_entity_day=max_changes_per_entity_day,
    )


def empty_ledger(
    spend_today: str = "0", spend_mtd: str = "0", applied_today: str = "0", changes_today: int = 0
) -> LedgerSnapshot:
    return LedgerSnapshot(
        platform_spend_today=Money.of(spend_today),
        platform_spend_month_to_date=Money.of(spend_mtd),
        applied_changes_today=Money.of(applied_today),
        changes_count_today_for_entity=changes_today,
    )


def budget_diff(
    before: str = "100", after: str = "70", ref: EntityRef | None = None
) -> ProposedDiff:
    return ProposedDiff.build(
        entity_ref=ref or entity_ref(),
        parameter="daily_budget",
        before=Money.of(before),
        after=Money.of(after),
    )


def make_pending_proposal(
    *,
    diff: ProposedDiff | None = None,
    classification: Classification = Classification.ROUTINE,
    expected_state_hash: str | None = None,
) -> Proposal:
    """Propuesta recien nacida en `PENDING` (T067: punto de partida de
    `AuthorizeRuleAction`)."""
    resolved_diff = diff or budget_diff()
    proposal = Proposal.raise_proposal(
        proposal_id=new_proposal_id(),
        business_id=BusinessId.new(),
        diff=resolved_diff,
        classification=classification,
        cause=Cause(text="CPL sobre objetivo en 7D", rule_id="M05"),
        cause_key=CauseKey(
            entity_ref=resolved_diff.entity_ref, rule_id="M05", cause_type="cost_per_lead_high"
        ),
        evidence=(Evidence(metric="cpl", actual=41.2, target=28.0, window_preset="7D"),),
        estimated_impact=Money.of("310"),
        priority=Priority(urgency=Urgency.RECOMMENDED),
        now=NOW,
        expires_at=NOW + timedelta(hours=72),
        expected_state_hash=expected_state_hash,
    )
    proposal.pull_events()
    return proposal


def make_scheduled_proposal(
    *,
    diff: ProposedDiff | None = None,
    classification: Classification = Classification.ROUTINE,
    expected_state_hash: str | None = None,
    grace_period_seconds: int = 0,
) -> Proposal:
    """Propuesta en `SCHEDULED`, lista para que el chokepoint la reclame."""
    proposal = make_pending_proposal(
        diff=diff, classification=classification, expected_state_hash=expected_state_hash
    )
    proposal.approve(proposal.diff.diff_hash, NOW)
    proposal.schedule_execution(grace_period_seconds, NOW)
    proposal.pull_events()
    return proposal


def evaluate_guardrails(
    proposal: Proposal,
    scope: GuardrailScope,
    guardrails: GuardrailSet,
    ledger: LedgerSnapshot,
    kind: AuthorizationKind,
) -> GuardrailVerdict:
    # `money_pair_from_diff` (not the raw `diff.before`/`diff.after`): a
    # `new_campaign:`/`new_ad_set:`/`new_ad:` diff carries a `dict` payload,
    # never `Money` (T035 security re-check 2026-09-15, channel-gate tests
    # in `test_chokepoint.py`) -- same conversion the chokepoint itself
    # applies before evaluating.
    before, after = money_pair_from_diff(proposal.diff)
    change = GuardrailChange(
        scope=scope,
        entity_ref=proposal.diff.entity_ref,
        authorization_kind=kind,
        before=before,
        after=after,
        is_creation=proposal.diff.parameter.startswith("new_campaign:"),
    )
    return GuardrailEvaluator().evaluate(change, guardrails, ledger)


def sign_matching_authorization(
    proposal: Proposal,
    verdict: GuardrailVerdict,
    *,
    kind: AuthorizationKind = AuthorizationKind.HUMAN_APPROVAL,
    signer: FakeSignerPort | None = None,
    expires_at: datetime | None = None,
) -> Authorization:
    """Firma una `Authorization` cuyo `diff_hash`/`guardrail_verdict_hash`
    coinciden EXACTAMENTE con los que el chokepoint recalculara en vivo --
    `effective_diff` ya devuelve el diff sin recortar cuando `not
    verdict.allowed` (no hay diff recortado real que firmar)."""
    return sign_authorization(
        authorization_id=AuthorizationId.new(),
        proposal_id=proposal.proposal_id,
        kind=kind,
        proposal_classification=proposal.classification,
        diff_hash=effective_diff(proposal.diff, verdict).diff_hash,
        guardrail_verdict_hash=verdict.verdict_hash,
        issued_by="owner-1" if kind is AuthorizationKind.HUMAN_APPROVAL else "M05",
        channel=(
            AuthorizationChannel.PANEL
            if kind is AuthorizationKind.HUMAN_APPROVAL
            else AuthorizationChannel.RULE_ENGINE
        ),
        decided_at=NOW,
        expires_at=expires_at or NOW + timedelta(hours=1),
        signer=signer or FakeSignerPort(),
    )

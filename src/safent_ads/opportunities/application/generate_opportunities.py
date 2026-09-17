"""`GenerateOpportunities` (tasks.md T113, FR-35): por negocio, a partir de
huecos de calendario sin cobertura, produce `OpportunityCandidate`s
rankeados por `expected_contribution_delta` y los materializa como
`Proposal`s `CREATE_CAMPAIGN` (nacen `pending`, requieren aprobacion --
FR-36 las deja `PAUSED` cuando se ejecutan), respetando el presupuesto de
atencion diario (NFR-11).

**Alcance declarado (Assumption)**: esta version cubre UNA fuente de
evidencia -- hitos de `catalog.CalendarEvent` abiertos o proximos sin
`Proposal` viva que los cubra ya. Las otras dos fuentes que tasks.md T113
menciona (geos/emplazamientos con CPA probado bajo objetivo, huecos de
competencia en terminos de busqueda) no tienen hoy el dato que las
sustente (`ad_entities`/`metrics_daily` no llevan desglose de geo ni
emplazamiento; no existe un `DecisionKind` de hueco de competencia) --
anadirlas es una extension de `CalendarEventGapPort`/nuevos puertos
hermanos, no un cambio de `rank_by_expected_contribution`/
`split_by_attention_budget`."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import timedelta

from safent_ads.opportunities.application.ports import (
    CalendarEventGap,
    CalendarEventGapPort,
    CampaignProposalPort,
    DailyCandidateBudgetPort,
    OfferingContributionPort,
)
from safent_ads.opportunities.domain.campaign_brief_policy import (
    default_brief_for_calendar_event_gap,
)
from safent_ads.opportunities.domain.opportunity_candidate import (
    DEFAULT_MAX_NEW_CANDIDATES_PER_DAY,
    OpportunityCandidate,
    rank_by_expected_contribution,
    split_by_attention_budget,
)
from safent_ads.shared.clock import Clock
from safent_ads.shared.ids import BusinessId

CANDIDATE_HORIZON_DAYS = 30  # tool-surface.md `calendar-event-launch`: horizonte de deteccion
_CAUSE_MAX_LENGTH = 140


@dataclass(frozen=True, slots=True)
class GenerateOpportunitiesReport:
    accepted: int
    deferred: int
    skipped_without_economics: int


class GenerateOpportunities:
    def __init__(
        self,
        *,
        calendar_event_gaps: CalendarEventGapPort,
        offering_contribution: OfferingContributionPort,
        daily_budget: DailyCandidateBudgetPort,
        campaign_proposals: CampaignProposalPort,
        clock: Clock,
        max_new_candidates_per_day: int = DEFAULT_MAX_NEW_CANDIDATES_PER_DAY,
    ) -> None:
        self._calendar_event_gaps = calendar_event_gaps
        self._offering_contribution = offering_contribution
        self._daily_budget = daily_budget
        self._campaign_proposals = campaign_proposals
        self._clock = clock
        self._max_new_candidates_per_day = max_new_candidates_per_day

    async def execute(self, *, business_id: BusinessId) -> GenerateOpportunitiesReport:
        now = self._clock.now()
        gaps = await self._calendar_event_gaps.list_gaps(
            business_id=business_id, today=now.date(), horizon_days=CANDIDATE_HORIZON_DAYS
        )
        candidates, skipped = await self._build_candidates(business_id, gaps)
        ranked = rank_by_expected_contribution(candidates)

        already_accepted_today = await self._daily_budget.count_accepted_today(
            business_id=business_id, today=now.date()
        )
        remaining_slots = self._max_new_candidates_per_day - already_accepted_today
        split = split_by_attention_budget(ranked, remaining_slots=remaining_slots)

        for candidate in split.accepted:
            await self._campaign_proposals.accept(
                business_id=candidate.business_id,
                account_ref=candidate.account_ref,
                candidate_key=candidate.candidate_key,
                brief=candidate.brief,
                expected_contribution_delta=candidate.expected_contribution_delta,
                cause_sentence=candidate.cause_sentence,
                now=now,
            )
        for candidate in split.deferred:
            await self._campaign_proposals.defer(
                business_id=candidate.business_id,
                account_ref=candidate.account_ref,
                candidate_key=candidate.candidate_key,
                brief=candidate.brief,
                expected_contribution_delta=candidate.expected_contribution_delta,
                cause_sentence=candidate.cause_sentence,
                now=now,
                postpone_until=now + timedelta(days=1),
            )
        return GenerateOpportunitiesReport(
            accepted=len(split.accepted),
            deferred=len(split.deferred),
            skipped_without_economics=skipped,
        )

    async def _build_candidates(
        self, business_id: BusinessId, gaps: tuple[CalendarEventGap, ...]
    ) -> tuple[tuple[OpportunityCandidate, ...], int]:
        candidates: list[OpportunityCandidate] = []
        skipped = 0
        for gap in gaps:
            brief = default_brief_for_calendar_event_gap(
                platform=gap.account_ref.platform,
                offering_id=gap.offering_id,
                offering_name=gap.offering_name,
                calendar_event_id=gap.calendar_event_id,
                window_end=gap.window_end,
                region=gap.region,
            )
            expected_contribution_delta = (
                await self._offering_contribution.estimate_contribution_delta(
                    business_id=business_id,
                    offering_id=gap.offering_id,
                    daily_budget=brief.daily_budget,
                    duration_days=brief.duration_days,
                )
            )
            if expected_contribution_delta is None:
                skipped += 1
                continue
            candidates.append(
                OpportunityCandidate(
                    business_id=business_id,
                    candidate_key=f"calendar_event:{gap.calendar_event_id}:{gap.account_ref}",
                    account_ref=gap.account_ref,
                    brief=brief,
                    expected_contribution_delta=expected_contribution_delta,
                    cause_sentence=(
                        f"Hito de calendario {gap.offering_name} abierto hasta "
                        f"{gap.window_end.isoformat()} sin campana viva"
                    )[:_CAUSE_MAX_LENGTH],
                )
            )
        return tuple(candidates), skipped

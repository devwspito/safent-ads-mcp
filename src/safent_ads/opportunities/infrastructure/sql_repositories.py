"""Adaptadores SQL de `opportunities` (tasks.md T113/T114).

`SqlCampaignProposalPort` importa `proposals.application`/`proposals.domain`
directamente en vez de vivir en `composition/`: misma excepcion ya
documentada por `optimization.infrastructure.sql_experiment_proposal_port`
-- el puerto de salida hacia `proposals` (por encima de este contexto en
el grafo) esta declarado aqui porque esta lane no tiene autorizado anadir
ficheros a `composition/`; `proposals` no importa `opportunities` en ningun
punto, sin ciclo real.

`SqlOfferingContributionPort` reusa `economics.application.get_unit_economics.
GetUnitEconomics` (T156, ya construido) -- no se duplica el calculo de
contribucion, solo se traduce el resultado a la formula de este contexto
(profitability-engine.md §1).

`SqlCampaignProposalPort` construye `account_ref` como `entity_ref` de la
`Proposal` `CREATE_CAMPAIGN` sin insertar ninguna fila en `ad_entities`
(esa tabla modela solo campaign/ad_set/ad/creative -- `accounts.domain.
ad_entity.AdEntity.__post_init__` lo exige). `0027_us5_opportunities`
sustituyo `proposals_entity_fk` por un trigger que tambien acepta un
`entity_ref` de ambito de cuenta ya presente en `platform_accounts`."""

from __future__ import annotations

import uuid
from copy import deepcopy
from datetime import date, datetime, timedelta
from decimal import Decimal
from typing import Final

from sqlalchemy import RowMapping, text
from sqlalchemy.ext.asyncio import AsyncSession

from safent_ads.economics.application.errors import UnitEconomicsProfileNotFoundError
from safent_ads.economics.application.get_unit_economics import GetUnitEconomics
from safent_ads.economics.domain.identifiers import ProductId
from safent_ads.economics.infrastructure.sql_repositories import SqlUnitEconomicsProfileRepository
from safent_ads.opportunities.application.errors import AmbiguousActiveAccountForPlatformError
from safent_ads.opportunities.application.ports import (
    CalendarEventGap,
    CampaignProposalOutcome,
    OpenOpportunityView,
)
from safent_ads.opportunities.domain.campaign_brief import CampaignBrief
from safent_ads.opportunities.domain.opportunity_candidate import (
    PROPOSAL_CAUSE_TYPE,
    proposal_parameter_for,
)
from safent_ads.proposals.domain.campaign_creation import CampaignCreationError
from safent_ads.proposals.domain.cause import Cause, CauseKey
from safent_ads.proposals.domain.classification import ClassificationPolicy, ProposalKind
from safent_ads.proposals.domain.identifiers import ProposalId
from safent_ads.proposals.domain.money import Money as ProposalsMoney
from safent_ads.proposals.domain.priority import ExpiryPolicy, Priority, Urgency
from safent_ads.proposals.domain.proposal import (
    PostponedReason,
    Proposal,
    ProposalInvariantError,
    ProposedDiff,
    new_proposal_id,
)
from safent_ads.proposals.infrastructure.sql_proposal_repository import SqlProposalRepository
from safent_ads.proposals.infrastructure.value_codec import decode_value
from safent_ads.shared.clock import Clock
from safent_ads.shared.ids import BusinessId, EntityLevel, EntityRef, PlatformCode
from safent_ads.shared.physical_ads_sql import PHYSICAL_ACCOUNT_LOCK_SQL

__all__ = [
    "SqlAccountDailyCapPort",
    "SqlActiveAccountLookupPort",
    "SqlCalendarEventGapPort",
    "SqlCampaignProposalPort",
    "SqlDailyCandidateBudgetPort",
    "SqlOfferingContributionPort",
    "SqlOfferingExistsPort",
    "SqlOpenOpportunityPort",
]

# Mismo umbral que `optimization.infrastructure.sql_experiment_proposal_port`
# (tres lineas iguales pesan menos que una dependencia de composition).
_CRITICAL_IMPACT_THRESHOLD = ProposalsMoney.of("2000")
_RULE_ID = "opportunity_cycle"

_SELECT_CALENDAR_EVENT_GAPS: Final = """
    SELECT ce.id AS calendar_event_id, ce.offering_id, o.title AS offering_name,
           ce.window_end, ce.region, pa.platform, pa.external_account_id, pa.connection_id
      FROM calendar_events AS ce
      JOIN offerings AS o ON o.id = ce.offering_id AND o.business_id = ce.business_id
      JOIN (
          SELECT DISTINCT ON (business_id,platform,external_account_id) *
          FROM platform_accounts WHERE status='ACTIVE'
          ORDER BY business_id,platform,external_account_id,created_at,id
      ) AS pa ON pa.business_id = ce.business_id
     WHERE ce.business_id = :business_id
       AND o.is_active = true
       AND ce.window_end >= :today
       AND ce.window_start <= :horizon
     ORDER BY ce.window_end, ce.id, pa.platform, pa.external_account_id, pa.id
"""

# `proposals.cause_key` guarda `CauseKey.as_grouping_key()`
# ("cause_type:rule_id:entity_ref", proposals/domain/cause.py) -- no hay
# columna `cause_type` propia, de ahi el `LIKE` sobre el prefijo.
_COUNT_ACCEPTED_TODAY: Final = """
    SELECT count(*) FROM proposals
     WHERE business_id = :business_id AND cause_key LIKE :cause_key_prefix
       AND created_at >= :day_start AND created_at < :day_end
"""


class SqlCalendarEventGapPort:
    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def list_gaps(
        self, *, business_id: BusinessId, today: date, horizon_days: int
    ) -> tuple[CalendarEventGap, ...]:
        rows = (
            await self._session.execute(
                text(_SELECT_CALENDAR_EVENT_GAPS),
                {
                    "business_id": business_id.value,
                    "today": today,
                    "horizon": today + timedelta(days=horizon_days),
                },
            )
        ).mappings()
        return tuple(
            CalendarEventGap(
                calendar_event_id=str(row["calendar_event_id"]),
                offering_id=str(row["offering_id"]),
                offering_name=row["offering_name"],
                account_ref=EntityRef(
                    platform=PlatformCode(row["platform"]),
                    level=EntityLevel.ACCOUNT,
                    external_id=row["external_account_id"],
                    business_id=business_id.value if row["connection_id"] else None,
                    connection_id=row["connection_id"],
                ),
                window_end=row["window_end"],
                region=row["region"],
            )
            for row in rows
        )


class SqlOfferingContributionPort:
    """`estimate_contribution_delta`: `contribution_margin x conversiones
    esperadas` a partir de `target_cost_per_lead` (T156, ya calculado por
    `EconomicsCycle`) -- sin perfil todavia (oferta nueva sin historial),
    `None` (profitability-engine.md §1: 'el motor no propone ninguna
    subida de gasto' sin medicion)."""

    def __init__(self, session: AsyncSession, clock: Clock) -> None:
        self._get_unit_economics = GetUnitEconomics(
            SqlUnitEconomicsProfileRepository(session), clock
        )

    async def estimate_contribution_delta(
        self,
        *,
        business_id: BusinessId,
        offering_id: str,
        daily_budget: ProposalsMoney,
        duration_days: int,
    ) -> ProposalsMoney | None:
        try:
            view = await self._get_unit_economics.execute(
                business_id=business_id, product_id=ProductId.parse(offering_id)
            )
        except UnitEconomicsProfileNotFoundError:
            return None
        target_cost_per_lead = view.target_cost_per_lead.amount
        if target_cost_per_lead <= 0:
            return None
        total_budget = daily_budget.amount * Decimal(duration_days)
        expected_leads = total_budget / target_cost_per_lead
        delta = view.contribution_margin.amount * expected_leads
        return ProposalsMoney.of(delta, daily_budget.currency)


class SqlDailyCandidateBudgetPort:
    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def count_accepted_today(self, *, business_id: BusinessId, today: date) -> int:
        result = await self._session.execute(
            text(_COUNT_ACCEPTED_TODAY),
            {
                "business_id": business_id.value,
                "cause_key_prefix": f"{PROPOSAL_CAUSE_TYPE}:%",
                "day_start": datetime(today.year, today.month, today.day),
                "day_end": datetime(today.year, today.month, today.day) + timedelta(days=1),
            },
        )
        return int(result.scalar_one())


class SqlCampaignProposalPort:
    """`CampaignProposalPort`: `accept` siempre deja la propuesta `pending`
    (o la reactiva desde `postponed`, FR-20); `defer` solo crea una fila
    NUEVA en `postponed` -- si ya hay una equivalente viva (en cualquier
    estado), no la toca, para no revivir por accidente una que el
    presupuesto de atencion todavia no libera."""

    def __init__(self, session: AsyncSession, *, clock: Clock) -> None:
        self._session = session
        self._proposals = SqlProposalRepository(session)
        self._classification_policy = ClassificationPolicy(
            critical_impact_threshold=_CRITICAL_IMPACT_THRESHOLD
        )
        self._expiry_policy = ExpiryPolicy()
        self._clock = clock

    async def accept(
        self,
        *,
        business_id: BusinessId,
        account_ref: EntityRef,
        candidate_key: str,
        brief: CampaignBrief,
        expected_contribution_delta: ProposalsMoney | None,
        cause_sentence: str,
        now: datetime,
        proposed_by: str | None = None,
    ) -> CampaignProposalOutcome:
        brief.validate_creation_plan(account_ref)
        covered = await self._physical_coverage(business_id, account_ref, brief)
        if covered is not None and covered.diff.managed_binding is not None:
            # This producer has no admitted Enterprise actor. Coverage must not
            # disclose or adopt a proposal belonging to a managed principal.
            raise ProposalInvariantError("physical_opportunity_conflict")
        if covered is not None and (
            covered.diff.entity_ref != account_ref or covered.state.value in ("executed", "failed")
        ):
            if covered.diff.after != _brief_payload(brief):
                raise ProposalInvariantError("physical_opportunity_conflict")
            # Preserve the original approved identity/hash, even after revocation.
            # Failed/unknown remote outcomes are NOT evidence of no campaign.
            return _outcome_of(covered)
        diff, cause, impact = self._build_common(account_ref, candidate_key, brief, cause_sentence)
        existing = covered or await self._proposals.find_live_equivalent(
            account_ref, diff.parameter
        )
        if existing is not None and existing.diff.managed_binding != diff.managed_binding:
            raise ProposalInvariantError("physical_opportunity_conflict")
        if existing is not None and existing.diff.parameter != diff.parameter:
            return _outcome_of(existing)
        if existing is not None:
            outcome = await self._update_existing(
                existing, diff=diff, cause=cause, impact=impact, now=now
            )
            if outcome is not None:
                return outcome
        return await self._raise_fresh(
            business_id=business_id,
            diff=diff,
            cause=cause,
            impact=impact,
            brief=brief,
            now=now,
            expected_contribution_delta=expected_contribution_delta,
            proposed_by=proposed_by,
        )

    async def defer(
        self,
        *,
        business_id: BusinessId,
        account_ref: EntityRef,
        candidate_key: str,
        brief: CampaignBrief,
        expected_contribution_delta: ProposalsMoney | None,
        cause_sentence: str,
        now: datetime,
        postpone_until: datetime,
    ) -> str | None:
        brief.validate_creation_plan(account_ref)
        if await self._physical_coverage(business_id, account_ref, brief) is not None:
            return None
        diff, cause, impact = self._build_common(account_ref, candidate_key, brief, cause_sentence)
        existing = await self._proposals.find_live_equivalent(account_ref, diff.parameter)
        if existing is not None:
            return None
        proposal = self._new_proposal(
            business_id=business_id,
            diff=diff,
            cause=cause,
            impact=impact,
            brief=brief,
            now=now,
            expected_contribution_delta=expected_contribution_delta,
        )
        proposal.postpone(postpone_until, now, reason=PostponedReason.ATTENTION_BUDGET)
        await self._proposals.save(proposal)
        return str(proposal.proposal_id)

    async def _physical_coverage(
        self, business_id: BusinessId, account_ref: EntityRef, brief: CampaignBrief
    ) -> Proposal | None:
        """Serialize candidates on a server-resolved physical account, not OAuth.

        Existing hashes are not recalculated: old candidate keys are covered
        by the durable account identity and the original brief's source.
        """
        account_id = (
            await self._session.execute(
                text("""
            SELECT id FROM platform_accounts WHERE business_id=:business
              AND platform=:platform AND external_account_id=:external
              AND connection_id IS NOT DISTINCT FROM :connection
        """),
                {
                    "business": business_id.value,
                    "platform": account_ref.platform.value,
                    "external": account_ref.external_id,
                    "connection": account_ref.connection_id,
                },
            )
        ).scalar_one_or_none()
        if (
            account_id is None
            or account_ref.level != EntityLevel.ACCOUNT
            or account_ref.business_id not in (None, business_id.value)
            or account_ref.platform != brief.platform
        ):
            raise ProposalInvariantError("opportunity_account_scope_mismatch")
        await self._session.execute(
            text(PHYSICAL_ACCOUNT_LOCK_SQL), {"platform_account_id": account_id}
        )
        proposal_id = (
            await self._session.execute(
                text("""
            SELECT p.id FROM proposals p
            JOIN platform_accounts a ON a.business_id=p.business_id
              AND (a.account_ref=p.entity_ref OR
                   (a.connection_id IS NULL AND
                    a.platform || ':account:' || a.external_account_id=p.entity_ref))
            WHERE p.business_id=:business AND a.platform=:platform
              AND a.external_account_id=:external
              AND p.cause_key LIKE 'opportunity_candidate:%'
              AND p.state IN ('pending','postponed','approved','scheduled','executed','failed')
              AND ((CAST(:event AS TEXT) IS NOT NULL AND p.calendar_event_id::text=:event)
                OR (CAST(:event AS TEXT) IS NULL AND p.calendar_event_id IS NULL
                    AND p.proposed_value->'value'->>'offering_id'=:offering))
            ORDER BY p.created_at,p.id LIMIT 1
        """),
                {
                    "business": business_id.value,
                    "platform": account_ref.platform.value,
                    "external": account_ref.external_id,
                    "event": brief.calendar_event_id,
                    "offering": brief.offering_id,
                },
            )
        ).scalar_one_or_none()
        return None if proposal_id is None else await self._proposals.get(ProposalId(proposal_id))

    def _build_common(
        self,
        account_ref: EntityRef,
        candidate_key: str,
        brief: CampaignBrief,
        cause_sentence: str,
    ) -> tuple[ProposedDiff, Cause, ProposalsMoney]:
        diff = ProposedDiff.build(
            entity_ref=account_ref,
            parameter=proposal_parameter_for(candidate_key),
            before=None,
            after=_brief_payload(brief),
        )
        cause = Cause(text=cause_sentence)
        # `estimated_impact`: dinero en juego si se aprueba (todo el
        # presupuesto de prueba) -- `expected_contribution_delta` (aparte,
        # T156) es la estimacion de contribucion que ordena `list_proposals`.
        impact = brief.daily_budget.scaled_by(Decimal(brief.duration_days))
        return diff, cause, impact

    async def _update_existing(
        self,
        existing: Proposal,
        *,
        diff: ProposedDiff,
        cause: Cause,
        impact: ProposalsMoney,
        now: datetime,
    ) -> CampaignProposalOutcome | None:
        if existing.diff.managed_binding != diff.managed_binding:
            raise ProposalInvariantError("physical_opportunity_conflict")
        if (
            isinstance(existing.diff.after, dict)
            and existing.diff.after.get("creation_plan") is not None
            and isinstance(diff.after, dict)
            and diff.after.get("creation_plan") is None
        ):
            # A recurring brief-only suggestion cannot erase explicit native
            # choices already reviewed. Changes require a complete new plan.
            return _outcome_of(existing)
        if existing.state.value not in ("pending", "postponed"):
            if existing.diff.after != diff.after:
                raise ProposalInvariantError("physical_opportunity_conflict")
            return _outcome_of(existing)
        try:
            existing.update_with_equivalent(
                new_diff=diff,
                new_cause=cause,
                new_evidence=(),
                new_estimated_impact=impact,
                new_expires_at=self._expiry_policy.expires_at(Urgency.MINOR, now),
                now=now,
            )
        except ProposalInvariantError:
            # Ya aprobada/ejecutandose/ejecutada: cubierta de verdad, no se toca.
            return _outcome_of(existing)
        await self._proposals.save(existing)
        return _outcome_of(existing)

    async def _raise_fresh(
        self,
        *,
        business_id: BusinessId,
        diff: ProposedDiff,
        cause: Cause,
        impact: ProposalsMoney,
        brief: CampaignBrief,
        now: datetime,
        expected_contribution_delta: ProposalsMoney | None,
        proposed_by: str | None = None,
    ) -> CampaignProposalOutcome:
        proposal = self._new_proposal(
            business_id=business_id,
            diff=diff,
            cause=cause,
            impact=impact,
            brief=brief,
            now=now,
            expected_contribution_delta=expected_contribution_delta,
            proposed_by=proposed_by,
        )
        await self._proposals.save(proposal)
        return _outcome_of(proposal)

    def _new_proposal(
        self,
        *,
        business_id: BusinessId,
        diff: ProposedDiff,
        cause: Cause,
        impact: ProposalsMoney,
        brief: CampaignBrief,
        now: datetime,
        expected_contribution_delta: ProposalsMoney | None,
        proposed_by: str | None = None,
    ) -> Proposal:
        classification = self._classification_policy.classify(ProposalKind.CREATE_CAMPAIGN, impact)
        return Proposal.raise_proposal(
            proposal_id=new_proposal_id(),
            business_id=business_id,
            diff=diff,
            classification=classification,
            cause=cause,
            cause_key=CauseKey(
                entity_ref=diff.entity_ref, rule_id=_RULE_ID, cause_type=PROPOSAL_CAUSE_TYPE
            ),
            evidence=(),
            expected_contribution_delta=expected_contribution_delta,
            estimated_impact=impact,
            priority=Priority(urgency=Urgency.MINOR, calendar_event_id=brief.calendar_event_id),
            now=now,
            expires_at=self._expiry_policy.expires_at(Urgency.MINOR, now),
            proposed_by=proposed_by,
        )


def _outcome_of(proposal: Proposal) -> CampaignProposalOutcome:
    return CampaignProposalOutcome(
        proposal_id=str(proposal.proposal_id),
        estado=proposal.state.value,
        diff_hash=proposal.diff.diff_hash,
        expires_at=proposal.expires_at,
        classification=proposal.classification.value,
    )


def _brief_payload(brief: CampaignBrief) -> dict[str, object]:
    brief.validate_creation_plan()
    payload: dict[str, object] = {
        "objective": brief.objective,
        "platform": brief.platform.value,
        "offering_id": brief.offering_id,
        "daily_budget_amount": str(brief.daily_budget.amount),
        "daily_budget_currency": brief.daily_budget.currency,
        "duration_days": brief.duration_days,
        "success_criterion": brief.success_criterion,
        "kill_criterion": brief.kill_criterion,
        "angle": brief.angle,
        "targeting_seed": brief.targeting_seed,
        "geo": brief.geo,
        "calendar_event_id": brief.calendar_event_id,
    }
    if brief.creation_plan is not None:
        payload["creation_plan"] = deepcopy(brief.creation_plan)
    return payload


def _payload_to_brief(payload: dict[str, object]) -> CampaignBrief:
    """Inverso de `_brief_payload`: `decode_value` ya desenvuelve el
    `{"type": "json", "value": ...}` de `proposals.infrastructure.
    value_codec`, esto solo reconstruye el VO."""
    platform_raw = payload["platform"]
    assert isinstance(platform_raw, str)  # noqa: S101 - invariante del propio escritor, no input externo
    duration_raw = payload["duration_days"]
    assert isinstance(duration_raw, int)  # noqa: S101
    plan = payload.get("creation_plan")
    if plan is not None and not isinstance(plan, dict):
        raise CampaignCreationError("campaign_creation_plan_invalid")
    return CampaignBrief(
        objective=str(payload["objective"]),
        platform=PlatformCode(platform_raw),
        offering_id=str(payload["offering_id"]),
        daily_budget=ProposalsMoney.of(
            str(payload["daily_budget_amount"]), str(payload["daily_budget_currency"])
        ),
        duration_days=duration_raw,
        success_criterion=str(payload["success_criterion"]),
        kill_criterion=str(payload["kill_criterion"]),
        angle=str(payload["angle"]),
        targeting_seed=str(payload["targeting_seed"]),
        geo=None if payload["geo"] is None else str(payload["geo"]),
        calendar_event_id=(
            None if payload["calendar_event_id"] is None else str(payload["calendar_event_id"])
        ),
        creation_plan=plan,
    )


_SELECT_OPEN_OPPORTUNITIES: Final = """
    SELECT id, entity_ref, state, proposed_value::text AS proposed_value, cause,
           expected_contribution_delta_amount, expected_contribution_delta_currency, expires_at
      FROM proposals
     WHERE business_id = :business_id AND cause_key LIKE :cause_key_prefix
       AND state IN ('pending', 'postponed')
     ORDER BY expected_contribution_delta_amount DESC NULLS LAST
"""


class SqlOpenOpportunityPort:
    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def list_open(self, *, business_id: BusinessId) -> tuple[OpenOpportunityView, ...]:
        rows = (
            await self._session.execute(
                text(_SELECT_OPEN_OPPORTUNITIES),
                {
                    "business_id": business_id.value,
                    "cause_key_prefix": f"{PROPOSAL_CAUSE_TYPE}:%",
                },
            )
        ).mappings()
        return tuple(_row_to_view(row) for row in rows)


def _row_to_view(row: RowMapping) -> OpenOpportunityView:
    payload = decode_value(row["proposed_value"])
    if not isinstance(payload, dict):
        raise ValueError(f"proposed_value de una oportunidad sin forma de brief: {payload!r}")
    delta_amount = row["expected_contribution_delta_amount"]
    delta_currency = row["expected_contribution_delta_currency"]
    return OpenOpportunityView(
        proposal_id=str(row["id"]),
        state=row["state"],
        account_ref=EntityRef.parse(row["entity_ref"]),
        brief=_payload_to_brief(payload),
        expected_contribution_delta=(
            None if delta_amount is None else ProposalsMoney.of(delta_amount, delta_currency)
        ),
        cause_sentence=row["cause"],
        expires_at=row["expires_at"],
    )


# --- T114: adaptadores de validacion de `propose_campaign` -------------------

_EXISTS_ACTIVE_OFFERING: Final = """
    SELECT 1 FROM offerings WHERE business_id = :business_id AND id = :offering_id
       AND is_active = true
"""

_FIND_ACTIVE_ACCOUNT: Final = """
    SELECT platform, external_account_id, business_id, connection_id FROM platform_accounts
     WHERE business_id = :business_id AND platform = :platform AND status = 'ACTIVE'
       AND (CAST(:account_ref AS TEXT) IS NULL OR account_ref = :account_ref)
     ORDER BY created_at
     LIMIT 2
"""

_FIND_ACCOUNT_DAILY_CAP: Final = """
    SELECT g.daily_cap_minor, g.currency
      FROM guardrails AS g
      JOIN platform_accounts AS pa ON pa.id = g.platform_account_id
     WHERE g.scope = 'platform_account'
       AND pa.account_ref = :account_ref
       AND g.daily_cap_minor IS NOT NULL
     LIMIT 1
"""

_MINOR_UNITS_PER_MAJOR: Final = 100


class SqlOfferingExistsPort:
    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def exists(self, *, business_id: BusinessId, offering_id: str) -> bool:
        try:
            offering_uuid = uuid.UUID(offering_id)
        except ValueError:
            return False
        result = await self._session.execute(
            text(_EXISTS_ACTIVE_OFFERING),
            {"business_id": business_id.value, "offering_id": offering_uuid},
        )
        return result.first() is not None


class SqlActiveAccountLookupPort:
    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def find_active_account(
        self,
        *,
        business_id: BusinessId,
        platform: PlatformCode,
        account_ref: EntityRef | None = None,
    ) -> EntityRef | None:
        rows = (
            (
                await self._session.execute(
                    text(_FIND_ACTIVE_ACCOUNT),
                    {
                        "business_id": business_id.value,
                        "platform": platform.value,
                        "account_ref": str(account_ref) if account_ref else None,
                    },
                )
            )
            .mappings()
            .all()
        )
        if not rows:
            return None
        if len(rows) > 1:
            raise AmbiguousActiveAccountForPlatformError(
                f"{business_id.value}/{platform.value}: multiple ACTIVE accounts"
            )
        row = rows[0]
        return EntityRef(
            platform=PlatformCode(row["platform"]),
            level=EntityLevel.ACCOUNT,
            external_id=row["external_account_id"],
            business_id=row["business_id"] if row["connection_id"] else None,
            connection_id=row["connection_id"],
        )


class SqlAccountDailyCapPort:
    """`None` cuando la cuenta todavia no tiene un guardarrail de ambito
    `platform_account` configurado -- ver Assumption en `AccountDailyCapPort`."""

    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def get_daily_cap(self, *, account_ref: EntityRef) -> ProposalsMoney | None:
        params = {
            "platform": account_ref.platform.value,
            "external_account_id": account_ref.external_id,
            "account_ref": (
                str(account_ref)
                if account_ref.connection_id
                else f"{account_ref.platform.value}:{account_ref.external_id}"
            ),
        }
        result = await self._session.execute(text(_FIND_ACCOUNT_DAILY_CAP), params)
        row = result.mappings().first()
        if row is None:
            return None
        major_amount = Decimal(row["daily_cap_minor"]) / _MINOR_UNITS_PER_MAJOR
        return ProposalsMoney.of(major_amount, row["currency"])

"""Dobles en memoria de los puertos de `opportunities` (tasks.md T113/T114),
mismo patron que `optimization.testing.in_memory_repositories`: `seed*`
puebla el estado, el caso de uso solo ve los puertos."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, datetime

from safent_ads.opportunities.application.errors import AmbiguousActiveAccountForPlatformError
from safent_ads.opportunities.application.ports import CalendarEventGap, CampaignProposalOutcome
from safent_ads.opportunities.domain.campaign_brief import CampaignBrief
from safent_ads.proposals.domain.money import Money
from safent_ads.shared.ids import BusinessId, EntityRef, PlatformCode


class InMemoryCalendarEventGapPort:
    def __init__(self) -> None:
        self._gaps: dict[BusinessId, list[CalendarEventGap]] = {}

    def seed(self, *, business_id: BusinessId, gaps: list[CalendarEventGap]) -> None:
        self._gaps.setdefault(business_id, []).extend(gaps)

    async def list_gaps(
        self, *, business_id: BusinessId, today: date, horizon_days: int
    ) -> tuple[CalendarEventGap, ...]:
        del today, horizon_days
        return tuple(self._gaps.get(business_id, []))


class InMemoryOfferingContributionPort:
    def __init__(self) -> None:
        self._by_offering: dict[str, Money] = {}

    def seed(self, *, offering_id: str, expected_contribution_delta: Money) -> None:
        self._by_offering[offering_id] = expected_contribution_delta

    async def estimate_contribution_delta(
        self,
        *,
        business_id: BusinessId,
        offering_id: str,
        daily_budget: Money,
        duration_days: int,
    ) -> Money | None:
        del business_id, daily_budget, duration_days
        return self._by_offering.get(offering_id)


class InMemoryDailyCandidateBudgetPort:
    def __init__(self, *, already_accepted_today: int = 0) -> None:
        self._already_accepted_today = already_accepted_today

    async def count_accepted_today(self, *, business_id: BusinessId, today: date) -> int:
        del business_id, today
        return self._already_accepted_today


@dataclass(frozen=True, slots=True)
class RecordedProposal:
    business_id: BusinessId
    account_ref: EntityRef
    candidate_key: str
    brief: CampaignBrief
    expected_contribution_delta: Money | None
    cause_sentence: str
    proposed_by: str | None = None


@dataclass(frozen=True, slots=True)
class RecordedDeferral(RecordedProposal):
    postpone_until: datetime = field(default=datetime.min)


class InMemoryCampaignProposalPort:
    def __init__(self) -> None:
        self.accepted: list[RecordedProposal] = []
        self.deferred: list[RecordedDeferral] = []
        self._already_live: set[str] = set()

    def seed_already_live(self, candidate_key: str) -> None:
        """Simula que `candidate_key` ya tiene una propuesta viva (en
        cualquier estado): `defer` debe dejarla intacta."""
        self._already_live.add(candidate_key)

    async def accept(
        self,
        *,
        business_id: BusinessId,
        account_ref: EntityRef,
        candidate_key: str,
        brief: CampaignBrief,
        expected_contribution_delta: Money | None,
        cause_sentence: str,
        now: datetime,
        proposed_by: str | None = None,
    ) -> CampaignProposalOutcome:
        del now
        self.accepted.append(
            RecordedProposal(
                business_id=business_id,
                account_ref=account_ref,
                candidate_key=candidate_key,
                brief=brief,
                expected_contribution_delta=expected_contribution_delta,
                cause_sentence=cause_sentence,
                proposed_by=proposed_by,
            )
        )
        return CampaignProposalOutcome(
            proposal_id=candidate_key,
            estado="pending",
            diff_hash="0" * 64,
            expires_at=datetime.max,
            classification="important",
        )

    async def defer(
        self,
        *,
        business_id: BusinessId,
        account_ref: EntityRef,
        candidate_key: str,
        brief: CampaignBrief,
        expected_contribution_delta: Money | None,
        cause_sentence: str,
        now: datetime,
        postpone_until: datetime,
    ) -> str | None:
        del now
        if candidate_key in self._already_live:
            return None
        self.deferred.append(
            RecordedDeferral(
                business_id=business_id,
                account_ref=account_ref,
                candidate_key=candidate_key,
                brief=brief,
                expected_contribution_delta=expected_contribution_delta,
                cause_sentence=cause_sentence,
                postpone_until=postpone_until,
            )
        )
        return candidate_key


class InMemoryOfferingExistsPort:
    def __init__(self, *, existing_offering_ids: frozenset[str] = frozenset()) -> None:
        self._existing = existing_offering_ids

    async def exists(self, *, business_id: BusinessId, offering_id: str) -> bool:
        del business_id
        return offering_id in self._existing


class InMemoryActiveAccountLookupPort:
    """Mismo contrato que `SqlActiveAccountLookupPort`: `seed` acumula (no
    sobrescribe) para poder modelar mas de una cuenta `ACTIVE` por negocio y
    plataforma -- el caso que debe levantar
    `AmbiguousActiveAccountForPlatformError` en vez de elegir en silencio."""

    def __init__(self) -> None:
        self._accounts: dict[tuple[BusinessId, PlatformCode], list[EntityRef]] = {}

    def seed(
        self, *, business_id: BusinessId, platform: PlatformCode, account_ref: EntityRef
    ) -> None:
        candidates = self._accounts.setdefault((business_id, platform), [])
        if account_ref not in candidates:
            candidates.append(account_ref)

    async def find_active_account(
        self,
        *,
        business_id: BusinessId,
        platform: PlatformCode,
        account_ref: EntityRef | None = None,
    ) -> EntityRef | None:
        candidates = self._accounts.get((business_id, platform), [])
        if account_ref is not None:
            candidates = [candidate for candidate in candidates if candidate == account_ref]
        if not candidates:
            return None
        if len(candidates) > 1:
            raise AmbiguousActiveAccountForPlatformError(
                f"{business_id.value}/{platform.value}: multiple ACTIVE accounts"
            )
        return candidates[0]


class InMemoryAccountDailyCapPort:
    def __init__(self) -> None:
        self._caps: dict[EntityRef, Money] = {}

    def seed(self, *, account_ref: EntityRef, daily_cap: Money) -> None:
        self._caps[account_ref] = daily_cap

    async def get_daily_cap(self, *, account_ref: EntityRef) -> Money | None:
        return self._caps.get(account_ref)

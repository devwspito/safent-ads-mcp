"""`ProposeCampaign` (tasks.md T114, `mcp/presentation/catalog.py:80-82`):
el modelo propone un brief de campana -> validado (presupuesto dentro de
guardarrailes, la oferta existe, sin propuesta abierta duplicada) ->
`Proposal` `CREATE_CAMPAIGN` pendiente; nunca toca una plataforma
(contracts/mcp-tools.md regla 3).

Comparte `CampaignProposalPort.accept` con `GenerateOpportunities` (T113):
misma deduplicacion FR-20 por `(account_ref, parameter)`, sin
reimplementarla aqui. `candidate_key`: si el brief trae `calendar_event_id`,
el MISMO que usaria el ciclo deterministico para ese hito+cuenta -- una
propuesta del agente para un hito ya detectado por el ciclo actualiza la
existente en vez de duplicarla; sin hito, se deduplica por
`(platform, offering_id, account_ref)`."""

from __future__ import annotations

from dataclasses import dataclass

from safent_ads.opportunities.application.errors import (
    ChannelTypeNotEnabledError,
    DailyBudgetExceedsCapError,
    NoActiveAccountForPlatformError,
    OfferingNotFoundError,
)
from safent_ads.opportunities.application.ports import (
    AccountDailyCapPort,
    ActiveAccountLookupPort,
    CampaignProposalOutcome,
    CampaignProposalPort,
    OfferingExistsPort,
)
from safent_ads.opportunities.domain.campaign_brief import (
    CampaignBrief,
    google_channel_from_creation_plan,
)
from safent_ads.proposals.domain.google_channel_spec import GoogleAdvertisingChannelType
from safent_ads.shared.clock import Clock
from safent_ads.shared.ids import BusinessId, EntityLevel, EntityRef

_MAX_CAUSE_LENGTH = 140


@dataclass(frozen=True, kw_only=True, slots=True)
class ProposeCampaignRequest:
    business_id: BusinessId
    brief: CampaignBrief
    account_ref: EntityRef | None = None
    # data-model.md §4: `person:<user_id>` cuando una llamada MCP con puesto
    # crea la propuesta (bug 2, hotfix 0.2.20 -- antes se perdia siempre en
    # esta frontera), `None` para el motor de reglas u otro llamador.
    proposed_by: str | None = None


class ProposeCampaign:
    def __init__(
        self,
        *,
        offerings: OfferingExistsPort,
        accounts: ActiveAccountLookupPort,
        daily_caps: AccountDailyCapPort,
        campaign_proposals: CampaignProposalPort,
        clock: Clock,
        enabled_google_channels: frozenset[GoogleAdvertisingChannelType] = frozenset(
            {GoogleAdvertisingChannelType.SEARCH}
        ),
    ) -> None:
        self._offerings = offerings
        self._accounts = accounts
        self._daily_caps = daily_caps
        self._campaign_proposals = campaign_proposals
        self._clock = clock
        self._enabled_google_channels = enabled_google_channels

    async def execute(self, request: ProposeCampaignRequest) -> CampaignProposalOutcome:
        brief = request.brief
        brief.validate_creation_plan()
        # T076 (POLISH): defence in depth, before any port I/O -- the draft
        # promotion path (`CampaignDraftStore.promote`) already saved this
        # brief days ago through the MCP-boundary check; the installation's
        # enabled channels can have changed since.
        self._require_enabled_google_channel(brief)
        if not await self._offerings.exists(
            business_id=request.business_id, offering_id=brief.offering_id
        ):
            raise OfferingNotFoundError(brief.offering_id)

        selected = request.account_ref
        if selected is not None and (
            selected.level != EntityLevel.ACCOUNT
            or selected.platform != brief.platform
            or selected.business_id != request.business_id.value
            or selected.connection_id is None
        ):
            raise NoActiveAccountForPlatformError(brief.platform.value)
        account_ref = await self._accounts.find_active_account(
            business_id=request.business_id, platform=brief.platform, account_ref=selected
        )
        if account_ref is None:
            raise NoActiveAccountForPlatformError(brief.platform.value)

        brief.validate_creation_plan(account_ref)
        await self._require_budget_within_cap(account_ref, brief)

        now = self._clock.now()
        return await self._campaign_proposals.accept(
            business_id=request.business_id,
            account_ref=account_ref,
            candidate_key=_candidate_key(brief, account_ref),
            brief=brief,
            expected_contribution_delta=None,
            cause_sentence=_cause_sentence(brief),
            now=now,
            proposed_by=request.proposed_by,
        )

    async def _require_budget_within_cap(
        self, account_ref: EntityRef, brief: CampaignBrief
    ) -> None:
        cap = await self._daily_caps.get_daily_cap(account_ref=account_ref)
        if cap is not None and brief.daily_budget.amount > cap.amount:
            raise DailyBudgetExceedsCapError(
                f"{brief.daily_budget.amount} > {cap.amount} {cap.currency}"
            )

    def _require_enabled_google_channel(self, brief: CampaignBrief) -> None:
        channel = google_channel_from_creation_plan(brief.creation_plan)
        if channel is not None and channel not in self._enabled_google_channels:
            raise ChannelTypeNotEnabledError(channel=channel.value)


def _candidate_key(brief: CampaignBrief, account_ref: EntityRef) -> str:
    if brief.calendar_event_id is not None:
        return f"calendar_event:{brief.calendar_event_id}:{account_ref}"
    return f"agent:{brief.platform.value}:{brief.offering_id}:{account_ref}"


def _cause_sentence(brief: CampaignBrief) -> str:
    return brief.objective[:_MAX_CAUSE_LENGTH]

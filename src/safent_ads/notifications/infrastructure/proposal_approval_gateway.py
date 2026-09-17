"""`ProposalApprovalGateway` implementa `ApprovalGatewayPort`
(contracts/telegram.md: "Telegram debe llamar al MISMO caso de uso que
REST, nunca uno paralelo"): envuelve exactamente las mismas piezas que
`composition/execution_rest.py` usa para el panel --
`SubmitApproval.execute()` para aprobar, `Proposal.reject()`/`.postpone()`
para las otras dos decisiones (mismo patron que el router REST, que
tampoco tiene un caso de uso dedicado para esas dos todavia) y
`UndoExecution.execute()` para deshacer -- para que las dos superficies
produzcan siempre el mismo tipo de `Authorization`.

Vive en `infrastructure/` a proposito: es la UNICA pieza de `notifications`
que importa `proposals`/`execution` (plan.md §4 permite N7 -> N5/N6, las
flechas del grafo van solo hacia arriba); `notifications/application` nunca
lo hace, solo conoce `ApprovalGatewayPort`."""

from __future__ import annotations

from datetime import timedelta

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from safent_ads.composition.container import ExecutionUseCases
from safent_ads.execution.application.undo_execution import (
    ExecutionAlreadyUndoneError,
    UndoExecutionCommand,
    UndoNotAllowedError,
    UndoOutcome,
)
from safent_ads.execution.domain.guardrails import GuardrailScope, ScopeKind, money_pair_from_diff
from safent_ads.notifications.application.dto import (
    ApprovalDetailView,
    ApprovalEvidenceLine,
    SignalKind,
)
from safent_ads.notifications.application.ports import (
    DecisionKind,
    DecisionResult,
    LiveProposalView,
    UndoResult,
    UndoResultKind,
)
from safent_ads.proposals.application.submit_approval import (
    ProposalApprovalDeniedError,
    SubmitApprovalCommand,
)
from safent_ads.proposals.domain.authorization import AuthorizationChannel
from safent_ads.proposals.domain.identifiers import ProposalId
from safent_ads.proposals.domain.money import Money
from safent_ads.proposals.domain.proposal import Proposal, ProposalState
from safent_ads.shared.clock import Clock
from safent_ads.shared.ids import EntityRef

_MAX_GROUP_SIBLINGS = 24
_CURRENCY_SYMBOLS: dict[str, str] = {"EUR": "€"}
_PARAMETER_LABELS: dict[str, str] = {"daily_budget": "Presupuesto"}
_PARAMETER_UNIT_SUFFIX: dict[str, str] = {"daily_budget": "/día"}
_STATE_LABELS: dict[ProposalState, str] = {
    ProposalState.PENDING: "pendiente",
    ProposalState.POSTPONED: "pospuesta",
    ProposalState.APPROVED: "aprobada",
    ProposalState.SCHEDULED: "programada",
    ProposalState.EXECUTING: "ejecutando",
    ProposalState.EXECUTED: "ejecutada",
    ProposalState.FAILED: "fallida",
    ProposalState.EXPIRED: "caducada",
    ProposalState.INVALIDATED: "invalidada",
    ProposalState.REJECTED: "rechazada",
}
_UNDO_OUTCOME_MAP: dict[UndoOutcome, UndoResultKind] = {
    UndoOutcome.CANCELLED_SCHEDULED: UndoResultKind.CANCELLED,
    UndoOutcome.RESTORED: UndoResultKind.RESTORED,
    UndoOutcome.COMPENSATING_PROPOSAL_CREATED: UndoResultKind.COMPENSATING_PROPOSAL_CREATED,
}

_SELECT_ENTITY_NAME = text("SELECT name FROM ad_entities WHERE entity_ref = :entity_ref")
_SELECT_SIBLING_PENDING_IDS = text("""
    SELECT id FROM proposals
     WHERE cause_key = :cause_key AND state = 'pending' AND id <> :exclude_id
     ORDER BY estimated_impact_amount DESC
     LIMIT :limit
""")


class ProposalApprovalGateway:
    def __init__(
        self, *, session: AsyncSession, use_cases: ExecutionUseCases, clock: Clock
    ) -> None:
        self._session = session
        self._use_cases = use_cases
        self._clock = clock

    async def get_live_proposal(self, proposal_id: str) -> LiveProposalView | None:
        proposal = await self._use_cases.proposals.get(ProposalId.parse(proposal_id))
        return None if proposal is None else await self._to_view(proposal)

    async def get_group_members(self, anchor_proposal_id: str) -> tuple[LiveProposalView, ...]:
        anchor = await self._use_cases.proposals.get(ProposalId.parse(anchor_proposal_id))
        if anchor is None:
            return ()
        views = [await self._to_view(anchor)]
        sibling_ids = await self._session.execute(
            _SELECT_SIBLING_PENDING_IDS,
            {
                "cause_key": anchor.cause_key.as_grouping_key(),
                "exclude_id": anchor_proposal_id,
                "limit": _MAX_GROUP_SIBLINGS,
            },
        )
        for row in sibling_ids:
            sibling = await self._use_cases.proposals.get(ProposalId(row.id))
            if sibling is not None:
                views.append(await self._to_view(sibling))
        return tuple(views)

    async def get_detail(self, proposal_id: str) -> ApprovalDetailView | None:
        proposal = await self._use_cases.proposals.get(ProposalId.parse(proposal_id))
        if proposal is None:
            return None
        scope = GuardrailScope(kind=ScopeKind.ENTITY, ref=str(proposal.diff.entity_ref))
        guardrails = await self._use_cases.guardrail_sets.get_effective(scope)
        evidence = tuple(
            ApprovalEvidenceLine(
                metric=item.metric,
                actual=item.actual,
                target=item.target,
                window_label=item.window_preset,
            )
            for item in proposal.evidence
        )
        return ApprovalDetailView(
            evidence=evidence,
            guardrail_floor_label=_format_money(guardrails.floor),
            guardrail_ceiling_label=_format_money(guardrails.ceiling),
            guardrail_monthly_cap_label=_format_money(guardrails.monthly_cap),
        )

    async def approve(self, *, proposal_id: str, diff_hash: str, decided_by: str) -> DecisionResult:
        try:
            await self._use_cases.submit_approval.execute(
                SubmitApprovalCommand(
                    proposal_id=ProposalId.parse(proposal_id),
                    diff_hash=diff_hash,
                    approved_by=decided_by,
                    channel=AuthorizationChannel.TELEGRAM,
                )
            )
        except ProposalApprovalDeniedError as exc:
            return DecisionResult(kind=DecisionKind.DENIED, denial_reason=exc.reason.value)
        return DecisionResult(kind=DecisionKind.APPROVED)

    async def reject(self, *, proposal_id: str, diff_hash: str, decided_by: str) -> DecisionResult:
        proposal = await self._require_pending(proposal_id)
        if isinstance(proposal, DecisionResult):
            return proposal
        if proposal.diff.diff_hash != diff_hash:
            return DecisionResult(kind=DecisionKind.DENIED, denial_reason="DIFF_CHANGED")
        proposal.reject(self._clock.now(), decided_by)
        await self._use_cases.proposals.save(proposal)
        return DecisionResult(kind=DecisionKind.REJECTED)

    async def postpone(self, *, proposal_id: str, decided_by: str, hours: int) -> DecisionResult:
        del decided_by
        proposal = await self._require_pending(proposal_id)
        if isinstance(proposal, DecisionResult):
            return proposal
        now = self._clock.now()
        proposal.postpone(now + timedelta(hours=hours), now)
        await self._use_cases.proposals.save(proposal)
        return DecisionResult(kind=DecisionKind.POSTPONED)

    async def undo(self, *, proposal_id: str, initiated_by: str) -> UndoResult:
        try:
            result = await self._use_cases.undo_execution.execute(
                UndoExecutionCommand(
                    proposal_id=ProposalId.parse(proposal_id), initiated_by=initiated_by
                )
            )
        except ExecutionAlreadyUndoneError:
            return UndoResult(kind=UndoResultKind.ALREADY_UNDONE)
        except UndoNotAllowedError:
            return UndoResult(kind=UndoResultKind.NOT_ALLOWED)
        return UndoResult(kind=_UNDO_OUTCOME_MAP[result.outcome])

    async def _require_pending(self, proposal_id: str) -> Proposal | DecisionResult:
        proposal = await self._use_cases.proposals.get(ProposalId.parse(proposal_id))
        if proposal is None or proposal.state is not ProposalState.PENDING:
            return DecisionResult(kind=DecisionKind.DENIED, denial_reason="PROPOSAL_NOT_PENDING")
        return proposal

    async def _to_view(self, proposal: Proposal) -> LiveProposalView:
        entity_name = await self._entity_name(proposal.diff.entity_ref)
        before, after = money_pair_from_diff(proposal.diff)
        suffix = _PARAMETER_UNIT_SUFFIX.get(proposal.diff.parameter, "")
        window_label = proposal.evidence[0].window_preset if proposal.evidence else "—"
        return LiveProposalView(
            proposal_id=str(proposal.proposal_id),
            diff_hash=proposal.diff.diff_hash,
            state_label=_STATE_LABELS.get(proposal.state, proposal.state.value),
            is_pending=proposal.state is ProposalState.PENDING,
            entity_name=entity_name,
            platform_label=proposal.diff.entity_ref.platform.value.title(),
            kind=_kind_for(before, after),
            parameter_label=_PARAMETER_LABELS.get(proposal.diff.parameter, proposal.diff.parameter),
            before_label=f"{_format_money(before)}{suffix}",
            after_label=f"{_format_money(after)}{suffix}",
            change_note=_percent_change_note(before, after),
            cause_text=proposal.cause.text,
            rule_id=proposal.cause.rule_id or "—",
            window_label=window_label,
            impact_label=f"+{_format_money(proposal.estimated_impact)}/mes",
            expires_at=proposal.expires_at,
            is_spend_increase=after.amount > before.amount,
        )

    async def _entity_name(self, entity_ref: EntityRef) -> str:
        result = await self._session.execute(_SELECT_ENTITY_NAME, {"entity_ref": str(entity_ref)})
        row = result.one_or_none()
        return entity_ref.external_id if row is None else row.name


def _kind_for(before: Money, after: Money) -> SignalKind:
    if after.amount == 0:
        return SignalKind.EXIT
    if after.amount > before.amount:
        return SignalKind.BUY
    return SignalKind.SELL


def _percent_change_note(before: Money, after: Money) -> str | None:
    if before.amount == 0:
        return None
    percent = (after.amount - before.amount) / before.amount * 100
    sign = "+" if percent >= 0 else "−"
    return f"{sign}{abs(percent):.0f} %"


def _format_money(money: Money) -> str:
    symbol = _CURRENCY_SYMBOLS.get(money.currency, money.currency)
    amount = money.amount
    if amount == amount.to_integral_value():
        return f"{amount:.0f} {symbol}"
    return f"{amount:.2f} {symbol}".replace(".", ",")

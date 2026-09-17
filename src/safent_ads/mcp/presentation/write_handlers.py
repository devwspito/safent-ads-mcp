"""Handlers de las escrituras (T081/T072, US2/US3): traducen argumentos
validados a llamadas de `ProposalWritePort` y devuelven el DTO tal cual —
igual disciplina que `handlers.py` (ninguna logica de negocio aqui)."""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from typing import Any

from safent_ads.mcp.application.caller_scope import CallerScope
from safent_ads.mcp.application.proposal_write_port import (
    DefensiveActionResult,
    ProposalWritePort,
    ProposalWriteResult,
    WithdrawProposalResult,
)
from safent_ads.mcp.presentation import args as a

type Handler[ArgsT, ReturnT] = Callable[[ArgsT, CallerScope], Awaitable[ReturnT]]

_PERSON_CALLER_PREFIX = "person:"


def _proposed_by(caller_scope: CallerScope) -> str | None:
    """data-model.md §4: `person:<user_id>` cuando el puesto de la llamada
    es una persona; `None` para cualquier otro llamador (motor de reglas,
    instancias gestionadas)."""
    return (
        caller_scope.caller_id if caller_scope.caller_id.startswith(_PERSON_CALLER_PREFIX) else None
    )


def build_write_handlers(port: ProposalWritePort) -> dict[str, Any]:
    """Devuelve `{tool_name: handler}`; `catalog.py` lo consume igual que
    `build_handlers` (misma erosion de tipo deliberada, ver su docstring)."""
    return {
        "propose_ad_child": _propose_ad_child(port),
        "propose_budget_change": _propose_budget_change(port),
        "propose_pause": _propose_pause(port),
        "propose_targeting_change": _propose_targeting_change(port),
        "propose_creative_publication": _propose_creative_publication(port),
        "propose_resume": _propose_resume(port),
        "propose_bid_target": _propose_bid_target(port),
        "propose_negative_keywords": _propose_negative_keywords(port),
        "propose_creative_rotation": _propose_creative_rotation(port),
        "propose_delete": _propose_delete(port),
        "withdraw_proposal": _withdraw_proposal(port),
        "apply_defensive_action": _apply_defensive_action(port),
    }


def _evidence_tuple(
    items: list[a.EvidenceArgs],
) -> tuple[tuple[str, float, float, str], ...]:
    return tuple(
        (item.metric, item.actual, item.target, item.window_preset.value) for item in items
    )


def _propose_ad_child(
    port: ProposalWritePort,
) -> Handler[a.ProposeAdChildArgs, ProposalWriteResult]:
    async def handler(
        args: a.ProposeAdChildArgs, caller_scope: CallerScope
    ) -> ProposalWriteResult:
        return await port.propose_ad_child(
            business_id=args.business_id,
            entity_ref=args.entity_ref,
            child_plan=args.child_plan.model_dump(mode="json"),
            cause_text=args.cause.text,
            proposed_by=_proposed_by(caller_scope),
        )

    return handler


def _propose_budget_change(
    port: ProposalWritePort,
) -> Handler[a.ProposeBudgetChangeArgs, ProposalWriteResult]:
    async def handler(
        args: a.ProposeBudgetChangeArgs, caller_scope: CallerScope
    ) -> ProposalWriteResult:
        return await port.propose_budget_change(
            business_id=args.business_id,
            entity_ref=args.entity_ref,
            new_daily_budget_amount=args.new_daily_budget_amount,
            new_daily_budget_currency=args.new_daily_budget_currency,
            cause_text=args.cause.text,
            cause_signal_id=args.cause.signal_id,
            cause_rule_id=args.cause.rule_id,
            evidence=_evidence_tuple(args.evidence),
            urgency=args.urgency.value,
            proposed_by=_proposed_by(caller_scope),
        )

    return handler


def _propose_pause(port: ProposalWritePort) -> Handler[a.ProposePauseArgs, ProposalWriteResult]:
    async def handler(args: a.ProposePauseArgs, caller_scope: CallerScope) -> ProposalWriteResult:
        return await port.propose_pause(
            business_id=args.business_id,
            entity_ref=args.entity_ref,
            cause_text=args.cause.text,
            cause_signal_id=args.cause.signal_id,
            cause_rule_id=args.cause.rule_id,
            evidence=_evidence_tuple(args.evidence),
            urgency=args.urgency.value,
            proposed_by=_proposed_by(caller_scope),
        )

    return handler


def _propose_targeting_change(
    port: ProposalWritePort,
) -> Handler[a.ProposeTargetingChangeArgs, ProposalWriteResult]:
    async def handler(
        args: a.ProposeTargetingChangeArgs, caller_scope: CallerScope
    ) -> ProposalWriteResult:
        return await port.propose_targeting_change(
            business_id=args.business_id,
            entity_ref=args.entity_ref,
            targeting_diff=args.targeting_diff,
            cause_text=args.cause.text,
            cause_signal_id=args.cause.signal_id,
            cause_rule_id=args.cause.rule_id,
            evidence=_evidence_tuple(args.evidence),
            urgency=args.urgency.value,
            proposed_by=_proposed_by(caller_scope),
        )

    return handler


def _propose_creative_publication(
    port: ProposalWritePort,
) -> Handler[a.ProposeCreativePublicationArgs, ProposalWriteResult]:
    async def handler(
        args: a.ProposeCreativePublicationArgs, caller_scope: CallerScope
    ) -> ProposalWriteResult:
        return await port.propose_creative_publication(
            business_id=args.business_id,
            ad_set_ref=args.ad_set_ref,
            creative_asset_ids=tuple(args.creative_asset_ids),
            ad_copy=args.ad_copy,
            cause_text=args.cause.text,
            cause_signal_id=args.cause.signal_id,
            cause_rule_id=args.cause.rule_id,
            proposed_by=_proposed_by(caller_scope),
        )

    return handler


def _propose_resume(port: ProposalWritePort) -> Handler[a.ProposeResumeArgs, ProposalWriteResult]:
    async def handler(args: a.ProposeResumeArgs, caller_scope: CallerScope) -> ProposalWriteResult:
        return await port.propose_resume(
            business_id=args.business_id,
            entity_ref=args.entity_ref,
            cause_text=args.cause.text,
            cause_signal_id=args.cause.signal_id,
            cause_rule_id=args.cause.rule_id,
            evidence=_evidence_tuple(args.evidence),
            urgency=args.urgency.value,
            proposed_by=_proposed_by(caller_scope),
        )

    return handler


def _propose_bid_target(
    port: ProposalWritePort,
) -> Handler[a.ProposeBidTargetArgs, ProposalWriteResult]:
    async def handler(
        args: a.ProposeBidTargetArgs, caller_scope: CallerScope
    ) -> ProposalWriteResult:
        return await port.propose_bid_target(
            business_id=args.business_id,
            entity_ref=args.entity_ref,
            bid_target_amount=args.bid_target_amount,
            bid_target_currency=args.bid_target_currency,
            cause_text=args.cause.text,
            cause_signal_id=args.cause.signal_id,
            cause_rule_id=args.cause.rule_id,
            evidence=_evidence_tuple(args.evidence),
            urgency=args.urgency.value,
            proposed_by=_proposed_by(caller_scope),
        )

    return handler


def _propose_negative_keywords(
    port: ProposalWritePort,
) -> Handler[a.ProposeNegativeKeywordsArgs, ProposalWriteResult]:
    async def handler(
        args: a.ProposeNegativeKeywordsArgs, caller_scope: CallerScope
    ) -> ProposalWriteResult:
        return await port.propose_negative_keywords(
            business_id=args.business_id,
            entity_ref=args.entity_ref,
            keywords=tuple(args.keywords),
            cause_text=args.cause.text,
            cause_signal_id=args.cause.signal_id,
            cause_rule_id=args.cause.rule_id,
            evidence=_evidence_tuple(args.evidence),
            urgency=args.urgency.value,
            proposed_by=_proposed_by(caller_scope),
        )

    return handler


def _propose_creative_rotation(
    port: ProposalWritePort,
) -> Handler[a.ProposeCreativeRotationArgs, ProposalWriteResult]:
    async def handler(
        args: a.ProposeCreativeRotationArgs, caller_scope: CallerScope
    ) -> ProposalWriteResult:
        return await port.propose_creative_rotation(
            business_id=args.business_id,
            entity_ref=args.entity_ref,
            cause_text=args.cause.text,
            cause_signal_id=args.cause.signal_id,
            cause_rule_id=args.cause.rule_id,
            evidence=_evidence_tuple(args.evidence),
            urgency=args.urgency.value,
            proposed_by=_proposed_by(caller_scope),
        )

    return handler


def _propose_delete(port: ProposalWritePort) -> Handler[a.ProposeDeleteArgs, ProposalWriteResult]:
    async def handler(args: a.ProposeDeleteArgs, caller_scope: CallerScope) -> ProposalWriteResult:
        return await port.propose_delete(
            business_id=args.business_id,
            entity_ref=args.entity_ref,
            cause_text=args.cause.text,
            cause_signal_id=args.cause.signal_id,
            cause_rule_id=args.cause.rule_id,
            evidence=_evidence_tuple(args.evidence),
            urgency=args.urgency.value,
            proposed_by=_proposed_by(caller_scope),
        )

    return handler


def _withdraw_proposal(
    port: ProposalWritePort,
) -> Handler[a.WithdrawProposalArgs, WithdrawProposalResult]:
    async def handler(
        args: a.WithdrawProposalArgs, _caller_scope: CallerScope
    ) -> WithdrawProposalResult:
        return await port.withdraw_proposal(
            business_id=args.business_id, proposal_id=args.proposal_id, reason=args.reason
        )

    return handler


def _apply_defensive_action(
    port: ProposalWritePort,
) -> Handler[a.ApplyDefensiveActionArgs, DefensiveActionResult]:
    async def handler(
        args: a.ApplyDefensiveActionArgs, _caller_scope: CallerScope
    ) -> DefensiveActionResult:
        return await port.apply_defensive_action(
            business_id=args.business_id,
            entity_ref=args.entity_ref,
            rule_id=args.rule_id,
            action=args.action.value,
            cause_text=args.cause.text,
            cause_signal_id=args.cause.signal_id,
            cause_rule_id=args.cause.rule_id,
            magnitude_pct=args.magnitude_pct,
        )

    return handler

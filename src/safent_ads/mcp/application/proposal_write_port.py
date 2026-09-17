"""`ProposalWritePort` (contracts/mcp-tools.md §Escrituras + `apply_defensive_action`).

Puerto hacia `proposals`/`execution` (plan.md §4: mcp es N7, puede depender
de N5/N6 directamente -- a diferencia de los puertos de LECTURA de este
mismo paquete, que envuelven la forma exacta del dato porque muchos
contextos la comparten, este puerto es deliberadamente fino: solo hay un
llamador de cada caso de uso (`ProposeAction`/`WithdrawProposal`/
`ApplyDefensiveAction`), así que el puerto es casi un alias de su firma.
La implementación real (`composition/mcp_write_adapter.py`) abre su propia
sesión por llamada -- `Container.build_execution_use_cases` vive en
`composition`, que `mcp` no puede importar (raíz de composición,
transversal), así que el puente es esta interfaz."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Protocol


@dataclass(frozen=True, slots=True)
class ProposalWriteResult:
    proposal_id: str
    estado: str
    diff_hash: str
    expires_at: datetime
    classification: str


@dataclass(frozen=True, slots=True)
class WithdrawProposalResult:
    estado: str


@dataclass(frozen=True, slots=True)
class DefensiveActionResult:
    execution_id: str
    outcome: str
    applied_value: object
    undo_deadline: datetime | None


class ProposalWritePort(Protocol):
    """Cada metodo corresponde 1:1 a una escritura de
    `contracts/mcp-tools.md`. Ninguno toca una plataforma (regla 3);
    `apply_defensive_action` es la unica excepcion acotada que llega hasta
    el chokepoint, y solo porque la regla ya es `AUTO` y la firma la acuña
    el servicio, nunca el agente."""

    async def propose_ad_child(
        self,
        *,
        business_id: str,
        entity_ref: str,
        child_plan: dict[str, object],
        cause_text: str,
        proposed_by: str | None = None,
    ) -> ProposalWriteResult: ...

    async def propose_budget_change(
        self,
        *,
        business_id: str,
        entity_ref: str,
        new_daily_budget_amount: str,
        new_daily_budget_currency: str,
        cause_text: str,
        cause_signal_id: str | None,
        cause_rule_id: str | None,
        evidence: tuple[tuple[str, float, float, str], ...],
        urgency: str,
        proposed_by: str | None = None,
    ) -> ProposalWriteResult: ...

    async def propose_pause(
        self,
        *,
        business_id: str,
        entity_ref: str,
        cause_text: str,
        cause_signal_id: str | None,
        cause_rule_id: str | None,
        evidence: tuple[tuple[str, float, float, str], ...],
        urgency: str,
        proposed_by: str | None = None,
    ) -> ProposalWriteResult: ...

    async def propose_targeting_change(
        self,
        *,
        business_id: str,
        entity_ref: str,
        targeting_diff: dict[str, object],
        cause_text: str,
        cause_signal_id: str | None,
        cause_rule_id: str | None,
        evidence: tuple[tuple[str, float, float, str], ...],
        urgency: str,
        proposed_by: str | None = None,
    ) -> ProposalWriteResult: ...

    async def propose_creative_publication(
        self,
        *,
        business_id: str,
        ad_set_ref: str,
        creative_asset_ids: tuple[str, ...],
        ad_copy: dict[str, object],
        cause_text: str,
        cause_signal_id: str | None,
        cause_rule_id: str | None,
        proposed_by: str | None = None,
    ) -> ProposalWriteResult: ...

    # 004 tasks-2.md W2 (historia 16-17): las 5 propuestas de optimizacion
    # que faltaban en el mapa `WriteOperation` <-> herramienta.

    async def propose_resume(
        self,
        *,
        business_id: str,
        entity_ref: str,
        cause_text: str,
        cause_signal_id: str | None,
        cause_rule_id: str | None,
        evidence: tuple[tuple[str, float, float, str], ...],
        urgency: str,
        proposed_by: str | None = None,
    ) -> ProposalWriteResult: ...

    async def propose_bid_target(
        self,
        *,
        business_id: str,
        entity_ref: str,
        bid_target_amount: str,
        bid_target_currency: str,
        cause_text: str,
        cause_signal_id: str | None,
        cause_rule_id: str | None,
        evidence: tuple[tuple[str, float, float, str], ...],
        urgency: str,
        proposed_by: str | None = None,
    ) -> ProposalWriteResult: ...

    async def propose_negative_keywords(
        self,
        *,
        business_id: str,
        entity_ref: str,
        keywords: tuple[str, ...],
        cause_text: str,
        cause_signal_id: str | None,
        cause_rule_id: str | None,
        evidence: tuple[tuple[str, float, float, str], ...],
        urgency: str,
        proposed_by: str | None = None,
    ) -> ProposalWriteResult: ...

    async def propose_creative_rotation(
        self,
        *,
        business_id: str,
        entity_ref: str,
        cause_text: str,
        cause_signal_id: str | None,
        cause_rule_id: str | None,
        evidence: tuple[tuple[str, float, float, str], ...],
        urgency: str,
        proposed_by: str | None = None,
    ) -> ProposalWriteResult: ...

    async def propose_delete(
        self,
        *,
        business_id: str,
        entity_ref: str,
        cause_text: str,
        cause_signal_id: str | None,
        cause_rule_id: str | None,
        evidence: tuple[tuple[str, float, float, str], ...],
        urgency: str,
        proposed_by: str | None = None,
    ) -> ProposalWriteResult: ...

    # 004 tasks-2.md W3 (historia 20): escritura nativa siempre con
    # aprobacion humana -- ver `proposals/domain/classification.py::
    # _ALWAYS_IMPORTANT`.

    async def propose_native_write(
        self,
        *,
        business_id: str,
        entity_ref: str,
        platform: str,
        operation: str,
        payload: dict[str, object],
        why: str,
        proposed_by: str | None = None,
    ) -> ProposalWriteResult: ...

    async def withdraw_proposal(
        self, *, business_id: str, proposal_id: str, reason: str | None
    ) -> WithdrawProposalResult: ...

    async def apply_defensive_action(
        self,
        *,
        business_id: str,
        entity_ref: str,
        rule_id: str,
        action: str,
        cause_text: str,
        cause_signal_id: str | None,
        cause_rule_id: str | None,
        magnitude_pct: float | None,
    ) -> DefensiveActionResult: ...

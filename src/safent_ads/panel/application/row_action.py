"""`derive_row_action` (026, tasks.md T010): deriva `kind`/`mode`/`friction`
de una fila desde su señal, controlabilidad, frescura, freno y la propuesta
viva que la sustenta -- puro, sin I/O. `sql_cockpit_read_model.py` (T007)
resuelve esa propuesta contra el chokepoint de escritura existente
(`proposals`/`execution`) y le pasa la forma reducida `ResolvedProposal`;
esta función nunca abre una sesión ni conoce SQL.

FR-013 ("el tablero MUST NO abrir escritura nueva"): `target` siempre
apunta a una ruta YA existente de `proposals`/`execution`, nunca una nueva.
Si no hay una propuesta viva que sustente la fila, `kind=NONE` con
`reason` -- nunca se anuncia una acción que el panel no puede ejecutar
(acordado con el carril del panel, ver docstring de `cockpit_dto.py`)."""

from __future__ import annotations

from dataclasses import dataclass, replace
from datetime import datetime

from safent_ads.panel.application.cockpit_dto import (
    ActionFriction,
    ActionKind,
    ActionMode,
    ActionTarget,
    AppliedChange,
    BlockedReason,
    RowAction,
    SignalKind,
)

_AUTONOMOUS_SIGNAL_KINDS = frozenset({SignalKind.SELL, SignalKind.EXIT})
_ACTION_KIND_BY_SIGNAL: dict[SignalKind, ActionKind] = {
    SignalKind.BUY: ActionKind.APPROVE_INCREASE,
    SignalKind.SELL: ActionKind.APPLY_DECREASE,
    SignalKind.EXIT: ActionKind.APPLY_EXIT,
}
_NON_ROUTINE_CLASSIFICATIONS = frozenset({"important", "critical"})
_NO_LIVE_PROPOSAL_REASON = "Sin propuesta activa para esta señal todavía."


@dataclass(frozen=True, slots=True, kw_only=True)
class ResolvedProposal:
    """Lo mínimo que `derive_row_action` necesita de la propuesta viva de
    una fila, ya resuelta por `sql_cockpit_read_model.py` contra
    `proposals`/`execution` -- nunca un puerto ni una sesión."""

    proposal_id: str
    diff_hash: str
    classification: str  # "routine" | "important" | "critical"
    is_live: bool  # estado en LIVE_STATES: pendiente de aprobación/aplicación
    is_resolved_unexecuted: bool  # rejected/expired/invalidated/failed
    applied_change: AppliedChange | None = None
    execution_id: str | None = None


def _blocked(
    *, blocked_reason: BlockedReason | None, reason: str | None, kind: ActionKind = ActionKind.NONE
) -> RowAction:
    return RowAction(
        kind=kind,
        mode=ActionMode.BLOCKED,
        friction=ActionFriction.NONE,
        requires_evidence=False,
        proposal_id=None,
        diff_hash=None,
        applied_change=None,
        blocked_reason=blocked_reason,
        reason=reason,
        target=None,
    )


def _approve_target(proposal_id: str) -> ActionTarget:
    return ActionTarget(method="POST", path=f"/api/v1/proposals/{proposal_id}/approve")


def _undo_target(execution_id: str) -> ActionTarget:
    return ActionTarget(method="POST", path=f"/api/v1/executions/{execution_id}/undo")


def _classification_friction(classification: str) -> ActionFriction:
    return (
        ActionFriction.CONFIRM
        if classification in _NON_ROUTINE_CLASSIFICATIONS
        else ActionFriction.NONE
    )


def _autonomously_applied(
    action_kind: ActionKind, proposal: ResolvedProposal, *, now: datetime
) -> RowAction:
    applied = proposal.applied_change
    still_undoable = applied is not None and now < applied.undo_deadline
    target = (
        _undo_target(proposal.execution_id)
        if still_undoable and proposal.execution_id is not None
        else None
    )
    return RowAction(
        kind=action_kind,
        mode=ActionMode.AUTONOMOUS_APPLIED,
        friction=ActionFriction.NONE,
        requires_evidence=False,
        proposal_id=proposal.proposal_id,
        diff_hash=proposal.diff_hash,
        applied_change=applied,
        blocked_reason=None,
        reason=None,
        target=target,
    )


def _pending_approval(action_kind: ActionKind, proposal: ResolvedProposal) -> RowAction:
    is_increase = action_kind is ActionKind.APPROVE_INCREASE
    friction = ActionFriction.CONFIRM if is_increase else _classification_friction(
        proposal.classification
    )
    return RowAction(
        kind=action_kind,
        mode=ActionMode.INLINE_APPROVAL if is_increase else ActionMode.PROPOSAL,
        friction=friction,
        requires_evidence=is_increase,
        proposal_id=proposal.proposal_id,
        diff_hash=proposal.diff_hash,
        applied_change=None,
        blocked_reason=None,
        reason=None,
        target=_approve_target(proposal.proposal_id),
    )


def derive_row_action(
    *,
    signal_kind: SignalKind | None,
    is_controllable: bool,
    is_stale: bool,
    brake_engaged: bool,
    proposal: ResolvedProposal | None,
    now: datetime,
) -> RowAction:
    if not is_controllable:
        return _blocked(
            blocked_reason=BlockedReason.NOT_CONTROLLABLE,
            reason="Entidad no controlable: presupuesto compartido o sin palanca.",
        )
    if signal_kind is None or signal_kind is SignalKind.HOLD:
        return _blocked(blocked_reason=None, reason="Sin señal accionable.")

    action = _resolve_from_proposal(
        action_kind=_ACTION_KIND_BY_SIGNAL[signal_kind],
        signal_kind=signal_kind,
        proposal=proposal,
        brake_engaged=brake_engaged,
        now=now,
    )
    return _stale_or(action, is_stale=is_stale)


def _resolve_from_proposal(
    *,
    action_kind: ActionKind,
    signal_kind: SignalKind,
    proposal: ResolvedProposal | None,
    brake_engaged: bool,
    now: datetime,
) -> RowAction:
    if proposal is not None and proposal.applied_change is not None:
        return _autonomously_applied(action_kind, proposal, now=now)
    if proposal is None:
        return _blocked(blocked_reason=None, reason=_NO_LIVE_PROPOSAL_REASON)
    if proposal.is_resolved_unexecuted:
        return _blocked(
            blocked_reason=None, reason="La propuesta ya se resolvió en otra superficie."
        )
    if not proposal.is_live:
        return _blocked(blocked_reason=None, reason=_NO_LIVE_PROPOSAL_REASON)
    if brake_engaged and signal_kind in _AUTONOMOUS_SIGNAL_KINDS:
        return replace(
            _pending_approval(action_kind, proposal),
            reason="El freno detiene la aplicación autónoma; requiere aprobación manual.",
        )
    return _pending_approval(action_kind, proposal)


def _stale_or(action: RowAction, *, is_stale: bool) -> RowAction:
    """NFR-1/cockpit-read-model.md §4: dato obsoleto -> `mode=blocked` con
    `blocked_reason=stale_data`, sin escritura -- se aplica DESPUES de
    resolver `kind`/friccion para que el motivo declarado sea el correcto
    incluso cuando la propuesta seguía viva."""
    if not is_stale:
        return action
    return replace(
        action,
        mode=ActionMode.BLOCKED,
        friction=ActionFriction.NONE,
        requires_evidence=False,
        blocked_reason=BlockedReason.STALE_DATA,
        reason="Dato obsoleto: sin escritura hasta refrescar.",
        target=None,
    )

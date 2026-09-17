"""`derive_row_action` (026, tasks.md T010): matriz señal x guardarraíl x
freno x frescura, pura -- sin sesión, sin puerto."""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from safent_ads.panel.application.cockpit_dto import (
    ActionFriction,
    ActionKind,
    ActionMode,
    AppliedChange,
    BlockedReason,
    RowAction,
    SignalKind,
)
from safent_ads.panel.application.row_action import ResolvedProposal, derive_row_action
from safent_ads.shared.read_models.serialization import to_json_dict

_NOW = datetime(2026, 9, 10, 12, 0, tzinfo=UTC)


def test_shared_panel_wire_contract() -> None:
    fixture = Path(__file__).resolve().parents[3] / "contracts" / "row-action.json"
    expected = json.loads(fixture.read_text())
    actions = [
        _derive(signal_kind=SignalKind.BUY, proposal=_live_proposal()),
        _derive(signal_kind=SignalKind.BUY, is_controllable=False, proposal=_live_proposal()),
    ]
    assert [to_json_dict(action) for action in actions] == expected


def _live_proposal(*, classification: str = "routine") -> ResolvedProposal:
    return ResolvedProposal(
        proposal_id="proposal-1",
        diff_hash="hash-1",
        classification=classification,
        is_live=True,
        is_resolved_unexecuted=False,
    )


def _applied_proposal(
    *, undo_deadline: datetime, execution_id: str = "execution-1"
) -> ResolvedProposal:
    return ResolvedProposal(
        proposal_id="proposal-1",
        diff_hash="hash-1",
        classification="routine",
        is_live=False,
        is_resolved_unexecuted=False,
        applied_change=AppliedChange(
            parameter="daily_budget_minor",
            before="10000",
            after="8000",
            applied_at=_NOW - timedelta(minutes=5),
            undo_deadline=undo_deadline,
        ),
        execution_id=execution_id,
    )


def _resolved_unexecuted_proposal() -> ResolvedProposal:
    return ResolvedProposal(
        proposal_id="proposal-1",
        diff_hash="hash-1",
        classification="routine",
        is_live=False,
        is_resolved_unexecuted=True,
    )


def _derive(
    *,
    signal_kind: SignalKind | None = SignalKind.SELL,
    is_controllable: bool = True,
    is_stale: bool = False,
    brake_engaged: bool = False,
    proposal: ResolvedProposal | None = None,
    now: datetime = _NOW,
) -> RowAction:
    return derive_row_action(
        signal_kind=signal_kind,
        is_controllable=is_controllable,
        is_stale=is_stale,
        brake_engaged=brake_engaged,
        proposal=proposal,
        now=now,
    )


def test_not_controllable_blocks_regardless_of_signal() -> None:
    action = _derive(signal_kind=SignalKind.BUY, is_controllable=False, proposal=_live_proposal())

    assert action.kind is ActionKind.NONE
    assert action.mode is ActionMode.BLOCKED
    assert action.blocked_reason is BlockedReason.NOT_CONTROLLABLE
    assert action.target is None


@pytest.mark.parametrize("signal_kind", [None, SignalKind.HOLD])
def test_no_actionable_signal_is_none(signal_kind: SignalKind | None) -> None:
    action = _derive(signal_kind=signal_kind, proposal=_live_proposal())

    assert action.kind is ActionKind.NONE
    assert action.mode is ActionMode.BLOCKED
    assert action.target is None


def test_no_live_proposal_yet_is_none_with_reason_never_a_dead_action() -> None:
    action = _derive(signal_kind=SignalKind.SELL, proposal=None)

    assert action.kind is ActionKind.NONE
    assert action.proposal_id is None
    assert action.target is None
    assert action.reason


def test_buy_signal_with_a_live_proposal_requires_inline_approval_and_evidence() -> None:
    action = _derive(signal_kind=SignalKind.BUY, proposal=_live_proposal())

    assert action.kind is ActionKind.APPROVE_INCREASE
    assert action.mode is ActionMode.INLINE_APPROVAL
    assert action.friction is ActionFriction.CONFIRM
    assert action.requires_evidence is True
    assert action.proposal_id == "proposal-1"
    assert action.diff_hash == "hash-1"
    assert action.target is not None
    assert action.target.method == "POST"
    assert action.target.path == "/api/v1/proposals/proposal-1/approve"


@pytest.mark.parametrize("signal_kind", [SignalKind.SELL, SignalKind.EXIT])
def test_sell_or_exit_applied_within_guardrail_is_autonomous_with_no_friction(
    signal_kind: SignalKind,
) -> None:
    proposal = _applied_proposal(undo_deadline=_NOW + timedelta(seconds=30))

    action = _derive(signal_kind=signal_kind, proposal=proposal)

    assert action.mode is ActionMode.AUTONOMOUS_APPLIED
    assert action.friction is ActionFriction.NONE
    assert action.applied_change is not None
    assert action.target is not None
    assert action.target.path == "/api/v1/executions/execution-1/undo"


def test_autonomous_applied_outside_the_undo_window_has_no_target() -> None:
    proposal = _applied_proposal(undo_deadline=_NOW - timedelta(seconds=1))

    action = _derive(signal_kind=SignalKind.SELL, proposal=proposal)

    assert action.mode is ActionMode.AUTONOMOUS_APPLIED
    assert action.target is None


def test_sell_blocked_by_guardrail_degrades_to_proposal_mode() -> None:
    action = _derive(
        signal_kind=SignalKind.SELL, proposal=_live_proposal(classification="important")
    )

    assert action.mode is ActionMode.PROPOSAL
    assert action.friction is ActionFriction.CONFIRM
    assert action.kind is ActionKind.APPLY_DECREASE


def test_routine_classification_proposal_mode_needs_no_confirmation() -> None:
    action = _derive(signal_kind=SignalKind.EXIT, proposal=_live_proposal(classification="routine"))

    assert action.friction is ActionFriction.NONE


def test_brake_engaged_forces_autonomous_signals_into_proposal_mode() -> None:
    action = _derive(
        signal_kind=SignalKind.SELL, brake_engaged=True, proposal=_live_proposal()
    )

    assert action.mode is ActionMode.PROPOSAL
    assert "freno" in (action.reason or "")


def test_brake_engaged_does_not_touch_an_already_applied_change() -> None:
    """El freno detiene la aplicacion autonoma FUTURA -- no deshace lo que
    ya se aplico dentro de guardarrail antes de activarse (FR-14: nunca se
    bloquea la accion segura)."""
    proposal = _applied_proposal(undo_deadline=_NOW + timedelta(seconds=30))

    action = _derive(signal_kind=SignalKind.SELL, brake_engaged=True, proposal=proposal)

    assert action.mode is ActionMode.AUTONOMOUS_APPLIED


def test_resolved_unexecuted_proposal_never_shows_a_second_action() -> None:
    """FR-012: una propuesta ya resuelta en otra superficie (p. ej.
    rechazada en Telegram) no vuelve a ofrecer accion en el cockpit."""
    action = _derive(signal_kind=SignalKind.SELL, proposal=_resolved_unexecuted_proposal())

    assert action.kind is ActionKind.NONE
    assert action.mode is ActionMode.BLOCKED
    assert action.target is None


def test_stale_data_blocks_regardless_of_an_otherwise_actionable_signal() -> None:
    action = _derive(signal_kind=SignalKind.BUY, is_stale=True, proposal=_live_proposal())

    assert action.mode is ActionMode.BLOCKED
    assert action.blocked_reason is BlockedReason.STALE_DATA
    assert action.target is None
    # `kind` se conserva -- el motivo del bloqueo es la frescura, no la señal.
    assert action.kind is ActionKind.APPROVE_INCREASE


def test_stale_overrides_autonomous_applied_target_too() -> None:
    proposal = _applied_proposal(undo_deadline=_NOW + timedelta(seconds=30))

    action = _derive(signal_kind=SignalKind.SELL, is_stale=True, proposal=proposal)

    assert action.mode is ActionMode.BLOCKED
    assert action.blocked_reason is BlockedReason.STALE_DATA
    assert action.target is None

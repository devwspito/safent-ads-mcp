"""`UndoGracePolicy.grace_for` (FR-15; design.md
§0.7: "Eliminar... aplica de inmediato: sin ventana de gracia ni
Deshacer")."""

from __future__ import annotations

from datetime import timedelta

from safent_ads.execution.domain.undo_policy import UndoGracePolicy
from safent_ads.proposals.domain.proposal import ProposedDiff
from safent_ads.shared.ids import EntityLevel, EntityRef, PlatformCode

_ENTITY_REF = EntityRef(PlatformCode.META, EntityLevel.CAMPAIGN, "c-1")
_POLICY = UndoGracePolicy()


def _status_diff(*, before: str, after: str) -> ProposedDiff:
    return ProposedDiff.build(
        entity_ref=_ENTITY_REF, parameter="status", before=before, after=after
    )


def test_pause_grace_applies_to_a_status_transition() -> None:
    grace = _POLICY.grace_for(_status_diff(before="ACTIVE", after="PAUSED"))

    assert grace == _POLICY.pause_grace


def test_resume_grace_applies_to_a_status_transition() -> None:
    grace = _POLICY.grace_for(_status_diff(before="PAUSED", after="ACTIVE"))

    assert grace == _POLICY.pause_grace


def test_delete_has_no_undo_window() -> None:
    grace = _POLICY.grace_for(_status_diff(before="ACTIVE", after="DELETED"))

    assert grace is None


def test_budget_decrease_keeps_its_own_grace() -> None:
    diff = ProposedDiff.build(
        entity_ref=_ENTITY_REF, parameter="daily_budget", before="70", after="50"
    )

    assert _POLICY.grace_for(diff) == _POLICY.budget_decrease_grace


def test_creation_keeps_zero_grace() -> None:
    diff = ProposedDiff.build(
        entity_ref=_ENTITY_REF, parameter="new_campaign:1", before=None, after={}
    )

    assert _POLICY.grace_for(diff) == timedelta(0)

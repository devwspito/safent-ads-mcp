"""`PendingDecision` (data-model.md §DecisionLogEntry): rechaza payloads con
claves de PII o secretos antes de que lleguen a la base de datos."""

from __future__ import annotations

import pytest

from safent_ads.audit.domain.entry import (
    ActorKind,
    DecisionKind,
    DecisionLogPayloadError,
    PendingDecision,
)
from safent_ads.shared.ids import BusinessId


def _business_id() -> BusinessId:
    return BusinessId.new()


def test_pending_decision_accepts_clean_payload() -> None:
    decision = PendingDecision(
        business_id=_business_id(),
        kind=DecisionKind.SIGNAL,
        actor_kind=ActorKind.RULE_ENGINE,
        payload={"cause_key": "M05", "money_at_stake": 42.5},
    )

    assert decision.payload["cause_key"] == "M05"


@pytest.mark.parametrize("forbidden_key", ["password", "Token", "totp_secret", "email"])
def test_pending_decision_rejects_forbidden_top_level_key(forbidden_key: str) -> None:
    with pytest.raises(DecisionLogPayloadError):
        PendingDecision(
            business_id=_business_id(),
            kind=DecisionKind.LOGIN,
            actor_kind=ActorKind.OWNER,
            payload={forbidden_key: "secret-value"},
        )


def test_pending_decision_rejects_forbidden_key_nested_in_dict() -> None:
    with pytest.raises(DecisionLogPayloadError):
        PendingDecision(
            business_id=_business_id(),
            kind=DecisionKind.PROPOSAL,
            actor_kind=ActorKind.AGENT,
            payload={"context": {"nested": {"password": "x"}}},
        )


def test_pending_decision_rejects_forbidden_key_nested_in_list_of_dicts() -> None:
    with pytest.raises(DecisionLogPayloadError):
        PendingDecision(
            business_id=_business_id(),
            kind=DecisionKind.EXECUTION,
            actor_kind=ActorKind.SYSTEM,
            payload={"items": [{"ok": True}, {"secret": "x"}]},
        )


def test_pending_decision_allows_business_key_that_contains_substring_key() -> None:
    """`cause_key` contiene la subcadena "key" pero no es un secreto: la
    comparacion es por clave exacta, no por subcadena."""
    decision = PendingDecision(
        business_id=_business_id(),
        kind=DecisionKind.PROPOSAL,
        actor_kind=ActorKind.RULE_ENGINE,
        payload={"cause_key": "M05", "api_tier": "STANDARD"},
    )

    assert decision.payload["cause_key"] == "M05"

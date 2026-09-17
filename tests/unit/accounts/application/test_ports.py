"""`ports.py` reproduce exactamente los DTOs de
`contracts/platform-port.md` y valida sus propios invariantes minimos."""

from __future__ import annotations

from datetime import UTC, date, datetime

import pytest

from safent_ads.accounts.application.ports import (
    SignedAuthorization,
    WriteIntent,
    WriteOperation,
    WriteOutcome,
)
from safent_ads.accounts.domain.date_window import DateWindow, InvalidDateWindowError
from safent_ads.shared.ids import EntityLevel, EntityRef, PlatformCode


def test_write_intent_carries_the_five_hashed_fields() -> None:
    intent = WriteIntent(
        entity_ref=EntityRef(PlatformCode.GOOGLE, EntityLevel.CAMPAIGN, "1"),
        operation=WriteOperation.LOWER_BUDGET,
        parametro="daily_budget_minor_units",
        valor_actual=5000,
        valor_propuesto=4000,
        diff_hash="a" * 64,
        expected_state_hash="b" * 64,
    )

    assert intent.operation is WriteOperation.LOWER_BUDGET
    assert intent.valor_propuesto == 4000


def test_signed_authorization_kind_is_restricted_literal() -> None:
    authorization = SignedAuthorization(
        authorization_id="auth-1",
        proposal_id="proposal-1",
        kind="human_approval",
        diff_hash="a" * 64,
        guardrail_verdict_hash="c" * 64,
        issued_by="owner-1",
        expires_at=datetime(2026, 9, 9, tzinfo=UTC),
        signature="ed25519-sig",
    )

    assert authorization.kind == "human_approval"


def test_write_outcome_denied_has_no_applied_value() -> None:
    outcome = WriteOutcome(
        outcome="DENIED",
        applied_value=None,
        state_hash_after=None,
        error_code="NO_AUTHORIZATION",
        platform_request_id=None,
    )

    assert outcome.outcome == "DENIED"
    assert outcome.applied_value is None


def test_date_window_rejects_start_after_end() -> None:
    with pytest.raises(InvalidDateWindowError):
        DateWindow(start=date(2026, 9, 9), end=date(2026, 9, 1))

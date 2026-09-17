"""`_derive_outcome` (mcp/infrastructure/sql_signal_read_port.py): mismos 5
estados que `panel.infrastructure.sql_read_model._signal_outcome`, sobre la
columna `signals.outcome_at_14d`. Puro, sin DB."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from safent_ads.mcp.infrastructure.sql_signal_read_port import _derive_outcome
from safent_ads.shared.read_models.dto import SignalOutcomeStatus

_NOW = datetime(2026, 3, 15, 12, 0, tzinfo=UTC)


def test_hold_signal_is_never_applicable() -> None:
    outcome = _derive_outcome("HOLD", _NOW - timedelta(days=20), None, _NOW)

    assert outcome.status == SignalOutcomeStatus.NOT_APPLICABLE
    assert outcome.days_remaining is None


def test_recent_signal_is_in_progress_with_days_remaining() -> None:
    outcome = _derive_outcome("SELL", _NOW - timedelta(days=5), None, _NOW)

    assert outcome.status == SignalOutcomeStatus.IN_PROGRESS
    assert outcome.days_remaining == 9


def test_closed_window_without_resolver_is_pending_not_invented() -> None:
    outcome = _derive_outcome("SELL", _NOW - timedelta(days=20), None, _NOW)

    assert outcome.status == SignalOutcomeStatus.PENDING


def test_populated_outcome_column_is_respected_as_confirmed() -> None:
    outcome = _derive_outcome(
        "SELL", _NOW - timedelta(days=20), {"status": "confirmed"}, _NOW
    )

    assert outcome.status == SignalOutcomeStatus.CONFIRMED
    assert outcome.evaluated_at == _NOW


def test_populated_outcome_column_is_respected_as_not_confirmed() -> None:
    outcome = _derive_outcome(
        "BUY", _NOW - timedelta(days=20), {"status": "not_confirmed"}, _NOW
    )

    assert outcome.status == SignalOutcomeStatus.NOT_CONFIRMED

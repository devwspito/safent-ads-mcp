"""`DailyOperationBudget` (Google Explorer 2.880/dia) y `WriteBudgetWindow`
(Meta Limited ~20/5 min): rechazan **antes** de la llamada
(contracts/platform-port.md, threat-model.md C-22)."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from safent_ads.broker.platforms.rate_limits import DailyOperationBudget, WriteBudgetWindow
from safent_ads.shared.clock import FixedClock

_DAY_ONE = datetime(2026, 9, 9, 10, 0, tzinfo=UTC)
_DAY_TWO = datetime(2026, 9, 10, 0, 0, 1, tzinfo=UTC)


def test_daily_ops_budget_throttles() -> None:
    clock = FixedClock(_DAY_ONE)
    budget = DailyOperationBudget(max_operations_per_day=3, clock=clock)

    assert budget.try_consume() is True
    assert budget.try_consume() is True
    assert budget.try_consume() is True
    assert budget.try_consume() is False
    assert budget.is_exhausted is True


def test_daily_ops_budget_resets_on_new_calendar_day() -> None:
    clock = FixedClock(_DAY_ONE)
    budget = DailyOperationBudget(max_operations_per_day=1, clock=clock)

    assert budget.try_consume() is True
    assert budget.try_consume() is False

    clock.advance_to(_DAY_TWO)

    assert budget.try_consume() is True


def test_daily_ops_budget_rejects_multi_op_batch_that_would_overflow() -> None:
    clock = FixedClock(_DAY_ONE)
    budget = DailyOperationBudget(max_operations_per_day=5, clock=clock)

    assert budget.try_consume(operations=4) is True
    assert budget.try_consume(operations=2) is False
    assert budget.try_consume(operations=1) is True


def test_write_budget_per_window() -> None:
    clock = FixedClock(_DAY_ONE)
    window = WriteBudgetWindow(max_calls=20, window=timedelta(minutes=5), clock=clock)

    for _ in range(20):
        assert window.try_consume() is True

    assert window.try_consume() is False


def test_write_budget_window_recovers_after_expiry() -> None:
    clock = FixedClock(_DAY_ONE)
    window = WriteBudgetWindow(max_calls=1, window=timedelta(minutes=5), clock=clock)

    assert window.try_consume() is True
    assert window.try_consume() is False

    clock.advance_to(_DAY_ONE + timedelta(minutes=5, seconds=1))

    assert window.try_consume() is True

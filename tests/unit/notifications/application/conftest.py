from __future__ import annotations

from decimal import Decimal

import pytest

from safent_ads.notifications.application.dto import Money, SignalKind, TickerSignal
from safent_ads.notifications.domain.value_objects import ActiveHoursWindow
from safent_ads.shared.ids import BusinessId


@pytest.fixture
def business_id() -> BusinessId:
    return BusinessId.new()


@pytest.fixture
def madrid_active_hours() -> ActiveHoursWindow:
    return ActiveHoursWindow.parse("08:00-21:00", tz_name="Europe/Madrid")


def make_signal(
    *,
    name: str = "Búsqueda Marca",
    platform: str = "Meta",
    kind: SignalKind = SignalKind.SELL,
    strength: int = 78,
    cause: str = "CPL 41 € vs 28 €",
    window: str = "7D",
    amount: str = "100",
) -> TickerSignal:
    return TickerSignal(
        entity_name=name,
        platform_label=platform,
        kind=kind,
        strength=strength,
        cause_text=cause,
        window_label=window,
        money_at_stake=Money(Decimal(amount)),
    )

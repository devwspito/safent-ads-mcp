"""`ReportBusinessStatus` (`/estado`): emparejamiento vivo obligatorio antes
de leer nada, mismo criterio que `ResolveCallback` -- pero sin decision de
por medio: un chat allow-listed sin emparejar no genera entrada en
`decision_log` (a diferencia de una pulsacion de boton no emparejada)."""

from __future__ import annotations

from decimal import Decimal

from safent_ads.notifications.application.dto import Money
from safent_ads.notifications.application.ports import BusinessStatusView, BusinessSummaryView
from safent_ads.notifications.application.report_business_status import ReportBusinessStatus
from safent_ads.notifications.testing.fakes import FakeBusinessStatusPort, FakeTelegramPairingGuard

_CHAT_ID = 111222333


def _status_view(*, engaged: bool = False) -> BusinessStatusView:
    return BusinessStatusView(
        spend_today=Money(Decimal("42")),
        daily_cap=Money(Decimal("100")),
        pacing_index_pct=95.0,
        pacing_projection_pct=101.0,
        open_signals=2,
        pending_proposals=1,
        brake_engaged=engaged,
        brake_mode="all" if engaged else None,
        freshness_lag_minutes=12,
        freshness_is_stale=False,
        is_partial=False,
    )


async def test_authorized_and_paired_returns_one_card_per_business() -> None:
    business = BusinessSummaryView(
        business_id="11111111-1111-1111-1111-111111111111", name="Negocio Uno"
    )
    port = FakeBusinessStatusPort(
        businesses=(business,), statuses={business.business_id: _status_view()}
    )
    use_case = ReportBusinessStatus(
        status=port, pairing_guard=FakeTelegramPairingGuard(paired=True)
    )

    cards = await use_case.execute(chat_id=_CHAT_ID)

    assert len(cards) == 1
    assert "Negocio Uno" in cards[0]
    assert "42" in cards[0]


async def test_allow_listed_but_unpaired_replies_sin_emparejar_without_a_decision() -> None:
    guard = FakeTelegramPairingGuard(paired=False)
    use_case = ReportBusinessStatus(status=FakeBusinessStatusPort(), pairing_guard=guard)

    cards = await use_case.execute(chat_id=_CHAT_ID)

    assert cards == ("Sin emparejar",)
    assert guard.denied_chat_ids == []  # no decision_log entry for a plain unpaired read


async def test_no_active_businesses_replies_with_a_friendly_message() -> None:
    use_case = ReportBusinessStatus(
        status=FakeBusinessStatusPort(), pairing_guard=FakeTelegramPairingGuard(paired=True)
    )

    cards = await use_case.execute(chat_id=_CHAT_ID)

    assert cards == ("No hay negocios activos.",)


async def test_brake_engaged_business_shows_it_in_the_card() -> None:
    business = BusinessSummaryView(
        business_id="22222222-2222-2222-2222-222222222222", name="Negocio Dos"
    )
    port = FakeBusinessStatusPort(
        businesses=(business,), statuses={business.business_id: _status_view(engaged=True)}
    )
    use_case = ReportBusinessStatus(
        status=port, pairing_guard=FakeTelegramPairingGuard(paired=True)
    )

    cards = await use_case.execute(chat_id=_CHAT_ID)

    assert "Freno activo" in cards[0]

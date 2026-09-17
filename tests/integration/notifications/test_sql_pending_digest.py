"""`SqlPendingDigest.pop_due` contra Postgres real: `digest_hour`
(0023_owner_settings) reemplaza "enviar en la primera hora activa" (ver
docstring de la migracion). Regresion: antes de esta rama, `digest_hour`
no tenia consumidor -- el digest se liberaba con la primera llamada dentro
de `active_hours`, sin importar la hora exacta configurada."""

from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal
from zoneinfo import ZoneInfo

import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from safent_ads.notifications.application.dto import Money, SignalKind, TickerSignal
from safent_ads.notifications.domain.value_objects import ActiveHoursWindow
from safent_ads.notifications.infrastructure.sql_repositories import SqlPendingDigest
from safent_ads.shared.ids import BusinessId
from tests.conftest import BusinessFactory

pytestmark = pytest.mark.integration

_MADRID = ZoneInfo("Europe/Madrid")
_ALWAYS_ACTIVE = ActiveHoursWindow.parse("00:00-23:59", tz_name="UTC")


def _signal() -> TickerSignal:
    return TickerSignal(
        entity_name="Campana de prueba",
        platform_label="google",
        kind=SignalKind.SELL,
        strength=70,
        cause_text="CPL por encima del objetivo",
        window_label="7D",
        money_at_stake=Money(amount=Decimal("250")),
    )


async def test_no_digest_before_the_configured_hour(
    db_session: AsyncSession, business_factory: BusinessFactory
) -> None:
    business_id = BusinessId(
        await business_factory.create(timezone="Europe/Madrid", digest_hour=9)
    )
    repository = SqlPendingDigest(
        db_session, active_hours=_ALWAYS_ACTIVE, digest_hour_default=8
    )
    await repository.enqueue(
        business_id, [_signal()], queued_at=datetime(2026, 9, 10, 2, 0, tzinfo=UTC)
    )

    due = await repository.pop_due(
        business_id, at=datetime(2026, 9, 10, 8, 0, tzinfo=_MADRID)
    )

    assert due == []


async def test_digest_fires_exactly_at_the_configured_hour(
    db_session: AsyncSession, business_factory: BusinessFactory
) -> None:
    business_id = BusinessId(
        await business_factory.create(timezone="Europe/Madrid", digest_hour=9)
    )
    repository = SqlPendingDigest(
        db_session, active_hours=_ALWAYS_ACTIVE, digest_hour_default=8
    )
    await repository.enqueue(
        business_id, [_signal()], queued_at=datetime(2026, 9, 10, 2, 0, tzinfo=UTC)
    )

    due = await repository.pop_due(
        business_id, at=datetime(2026, 9, 10, 9, 0, tzinfo=_MADRID)
    )

    assert len(due) == 1
    assert due[0].entity_name == "Campana de prueba"


async def test_no_second_digest_the_same_day_after_the_hour_passed(
    db_session: AsyncSession, business_factory: BusinessFactory
) -> None:
    business_id = BusinessId(
        await business_factory.create(timezone="Europe/Madrid", digest_hour=9)
    )
    repository = SqlPendingDigest(
        db_session, active_hours=_ALWAYS_ACTIVE, digest_hour_default=8
    )
    await repository.enqueue(
        business_id, [_signal()], queued_at=datetime(2026, 9, 10, 2, 0, tzinfo=UTC)
    )
    await repository.pop_due(business_id, at=datetime(2026, 9, 10, 9, 0, tzinfo=_MADRID))

    due = await repository.pop_due(
        business_id, at=datetime(2026, 9, 10, 10, 0, tzinfo=_MADRID)
    )

    assert due == []


async def test_falls_back_to_the_env_default_hour_when_business_never_set_one(
    db_session: AsyncSession, business_factory: BusinessFactory
) -> None:
    business_id = BusinessId(
        await business_factory.create(timezone="Europe/Madrid", digest_hour=None)
    )
    repository = SqlPendingDigest(
        db_session, active_hours=_ALWAYS_ACTIVE, digest_hour_default=8
    )
    await repository.enqueue(
        business_id, [_signal()], queued_at=datetime(2026, 9, 10, 2, 0, tzinfo=UTC)
    )

    at_default_hour = await repository.pop_due(
        business_id, at=datetime(2026, 9, 10, 8, 0, tzinfo=_ALWAYS_ACTIVE.tz)
    )

    assert len(at_default_hour) == 1

"""`DigestSchedule.is_due` (this branch: `digest_hour` reemplaza "primera
hora activa"). Fixed clock, sin DB -- la integracion contra Postgres real
vive en `tests/integration/notifications/test_sql_pending_digest.py`."""

from __future__ import annotations

from datetime import datetime
from zoneinfo import ZoneInfo

from safent_ads.notifications.domain.value_objects import DigestSchedule

_MADRID = ZoneInfo("Europe/Madrid")
_UTC = ZoneInfo("UTC")


def test_is_due_at_the_exact_configured_hour() -> None:
    schedule = DigestSchedule(hour=9, tz=_MADRID)

    assert schedule.is_due(datetime(2026, 9, 10, 9, 0, tzinfo=_MADRID)) is True
    assert schedule.is_due(datetime(2026, 9, 10, 9, 45, tzinfo=_MADRID)) is True


def test_not_due_before_or_after_the_configured_hour() -> None:
    schedule = DigestSchedule(hour=9, tz=_MADRID)

    assert schedule.is_due(datetime(2026, 9, 10, 8, 59, tzinfo=_MADRID)) is False
    assert schedule.is_due(datetime(2026, 9, 10, 10, 0, tzinfo=_MADRID)) is False


def test_converts_the_moment_to_the_schedule_timezone_before_comparing() -> None:
    schedule = DigestSchedule(hour=9, tz=_MADRID)
    # 07:00 UTC == 09:00 Europe/Madrid en septiembre (CEST, +2).
    moment_utc = datetime(2026, 9, 10, 7, 0, tzinfo=_UTC)

    assert schedule.is_due(moment_utc) is True

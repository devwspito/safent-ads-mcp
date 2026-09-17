"""`Session` (tasks.md T011): caducidad absoluta+por inactividad sobre
`created_at`/`expires_at` (0001_bootstrap.py, sin columnas nuevas)."""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta

import pytest

from safent_ads.iam.domain.errors import SessionExpiredError, SessionRevokedError
from safent_ads.iam.domain.session import TOUCH_PERSISTENCE_THRESHOLD, Session

_CREATED_AT = datetime(2026, 1, 1, 0, 0, tzinfo=UTC)
_IDLE_TTL = timedelta(minutes=30)
_ABSOLUTE_TTL = timedelta(hours=12)


def _session(expires_at: datetime, revoked_at: datetime | None = None) -> Session:
    return Session(
        session_id=uuid.uuid4(),
        owner_id=uuid.uuid4(),
        token_hash="a" * 64,
        created_at=_CREATED_AT,
        expires_at=expires_at,
        revoked_at=revoked_at,
    )


def test_session_active_within_idle_window() -> None:
    session = _session(expires_at=_CREATED_AT + _IDLE_TTL)
    now = _CREATED_AT + timedelta(minutes=10)

    session.require_active(now, _ABSOLUTE_TTL)


def test_session_expires_after_idle_window() -> None:
    session = _session(expires_at=_CREATED_AT + _IDLE_TTL)
    now = _CREATED_AT + timedelta(minutes=31)

    with pytest.raises(SessionExpiredError):
        session.require_active(now, _ABSOLUTE_TTL)


def test_session_expires_at_absolute_ttl_even_if_touched() -> None:
    session = _session(expires_at=_CREATED_AT + _ABSOLUTE_TTL + timedelta(hours=1))
    now = _CREATED_AT + _ABSOLUTE_TTL + timedelta(minutes=1)

    with pytest.raises(SessionExpiredError):
        session.require_active(now, _ABSOLUTE_TTL)


def test_revoked_session_is_never_active() -> None:
    session = _session(expires_at=_CREATED_AT + _IDLE_TTL, revoked_at=_CREATED_AT)

    with pytest.raises(SessionRevokedError):
        session.require_active(_CREATED_AT, _ABSOLUTE_TTL)


def test_touch_slides_expires_at_forward_within_idle_ttl() -> None:
    session = _session(expires_at=_CREATED_AT + _IDLE_TTL)
    now = _CREATED_AT + timedelta(minutes=20)

    session.touch(now, _IDLE_TTL, _ABSOLUTE_TTL)

    assert session.expires_at == now + _IDLE_TTL


def test_touch_never_slides_past_the_absolute_ttl() -> None:
    session = _session(expires_at=_CREATED_AT + _IDLE_TTL)
    now = _CREATED_AT + _ABSOLUTE_TTL - timedelta(minutes=5)

    session.touch(now, _IDLE_TTL, _ABSOLUTE_TTL)

    assert session.expires_at == _CREATED_AT + _ABSOLUTE_TTL


def test_touch_reports_no_persistence_needed_within_the_threshold() -> None:
    """Perf 16-sep item 2: el llamador (`current_owner`) usa esta senal
    para no escribir en cada peticion cuando el panel sondea cada
    10-60 s."""
    session = _session(expires_at=_CREATED_AT + _IDLE_TTL)
    baseline = _CREATED_AT + timedelta(minutes=10)
    session.touch(baseline, _IDLE_TTL, _ABSOLUTE_TTL)
    barely_later = baseline + TOUCH_PERSISTENCE_THRESHOLD

    should_persist = session.touch(barely_later, _IDLE_TTL, _ABSOLUTE_TTL)

    assert should_persist is False
    # `expires_at` en memoria siempre se desliza, se persista o no -- solo
    # cambia si el llamador escribe la fila.
    assert session.expires_at == barely_later + _IDLE_TTL


def test_touch_reports_persistence_needed_beyond_the_threshold() -> None:
    session = _session(expires_at=_CREATED_AT + _IDLE_TTL)
    baseline = _CREATED_AT + timedelta(minutes=10)
    session.touch(baseline, _IDLE_TTL, _ABSOLUTE_TTL)
    later = baseline + TOUCH_PERSISTENCE_THRESHOLD + timedelta(seconds=1)

    should_persist = session.touch(later, _IDLE_TTL, _ABSOLUTE_TTL)

    assert should_persist is True


def test_touch_persistence_signal_never_extends_the_absolute_ttl() -> None:
    session = _session(expires_at=_CREATED_AT + _IDLE_TTL)
    now = _CREATED_AT + _ABSOLUTE_TTL - timedelta(minutes=5)
    session.touch(now, _IDLE_TTL, _ABSOLUTE_TTL)
    past_the_cap = now + TOUCH_PERSISTENCE_THRESHOLD + timedelta(seconds=1)

    should_persist = session.touch(past_the_cap, _IDLE_TTL, _ABSOLUTE_TTL)

    assert should_persist is False
    assert session.expires_at == _CREATED_AT + _ABSOLUTE_TTL


def test_revoke_sets_revoked_at() -> None:
    session = _session(expires_at=_CREATED_AT + _IDLE_TTL)

    session.revoke(_CREATED_AT + timedelta(minutes=5))

    assert session.is_revoked is True

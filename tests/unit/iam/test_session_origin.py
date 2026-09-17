"""`Session` con origen y frescura federada (002b tasks.md T019/T020,
data-model.md §Session): 002b **no alarga ninguna sesion**. La frescura es
una marca aparte que nunca mueve `created_at` ni `expires_at`, y una sesion
revocada o caducada jamas la concede."""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta

import pytest

from safent_ads.iam.domain.errors import FederatedSessionWithoutIdentificationError
from safent_ads.iam.domain.session import Session, SessionOrigin

_CREATED_AT = datetime(2026, 9, 16, 12, 0, tzinfo=UTC)
_IDLE_TTL = timedelta(minutes=30)
_ABSOLUTE_TTL = timedelta(hours=12)
_WINDOW = timedelta(minutes=5)


def _session(
    *,
    origin: SessionOrigin = SessionOrigin.PASSWORD,
    last_federated_auth_at: datetime | None = None,
    expires_at: datetime | None = None,
    revoked_at: datetime | None = None,
) -> Session:
    return Session(
        session_id=uuid.uuid4(),
        owner_id=uuid.uuid4(),
        token_hash="a" * 64,
        created_at=_CREATED_AT,
        expires_at=expires_at or _CREATED_AT + _IDLE_TTL,
        revoked_at=revoked_at,
        origin=origin,
        last_federated_auth_at=last_federated_auth_at,
    )


def test_a_session_defaults_to_the_least_capable_origin() -> None:
    session = _session()

    assert session.origin is SessionOrigin.PASSWORD
    assert session.last_federated_auth_at is None


def test_a_federated_session_cannot_exist_without_its_identification_mark() -> None:
    with pytest.raises(FederatedSessionWithoutIdentificationError):
        _session(origin=SessionOrigin.FEDERATED, last_federated_auth_at=None)


def test_identification_is_fresh_inside_the_window() -> None:
    session = _session(origin=SessionOrigin.FEDERATED, last_federated_auth_at=_CREATED_AT)
    now = _CREATED_AT + _WINDOW - timedelta(seconds=1)

    assert session.has_fresh_federated_identification(now, _WINDOW, _ABSOLUTE_TTL) is True


def test_identification_is_stale_outside_the_window() -> None:
    session = _session(origin=SessionOrigin.FEDERATED, last_federated_auth_at=_CREATED_AT)
    now = _CREATED_AT + _WINDOW + timedelta(seconds=1)

    assert session.has_fresh_federated_identification(now, _WINDOW, _ABSOLUTE_TTL) is False


def test_identification_at_the_exact_edge_of_the_window_is_still_fresh() -> None:
    session = _session(origin=SessionOrigin.FEDERATED, last_federated_auth_at=_CREATED_AT)

    assert (
        session.has_fresh_federated_identification(_CREATED_AT + _WINDOW, _WINDOW, _ABSOLUTE_TTL)
        is True
    )


def test_a_session_without_the_mark_is_never_fresh() -> None:
    session = _session()

    assert session.has_fresh_federated_identification(_CREATED_AT, _WINDOW, _ABSOLUTE_TTL) is False


def test_a_revoked_session_is_never_fresh() -> None:
    session = _session(
        origin=SessionOrigin.FEDERATED,
        last_federated_auth_at=_CREATED_AT,
        revoked_at=_CREATED_AT,
    )

    assert session.has_fresh_federated_identification(_CREATED_AT, _WINDOW, _ABSOLUTE_TTL) is False


def test_an_idle_expired_session_is_never_fresh() -> None:
    session = _session(
        origin=SessionOrigin.FEDERATED, last_federated_auth_at=_CREATED_AT + _IDLE_TTL
    )
    now = _CREATED_AT + _IDLE_TTL + timedelta(seconds=1)

    assert session.has_fresh_federated_identification(now, _WINDOW, _ABSOLUTE_TTL) is False


def test_a_session_past_its_absolute_ttl_is_never_fresh() -> None:
    session = _session(
        origin=SessionOrigin.FEDERATED,
        last_federated_auth_at=_CREATED_AT + _ABSOLUTE_TTL,
        expires_at=_CREATED_AT + _ABSOLUTE_TTL + timedelta(hours=1),
    )
    now = _CREATED_AT + _ABSOLUTE_TTL + timedelta(seconds=1)

    assert session.has_fresh_federated_identification(now, _WINDOW, _ABSOLUTE_TTL) is False


def test_recording_identification_marks_freshness() -> None:
    session = _session()
    now = _CREATED_AT + timedelta(minutes=10)

    session.record_fresh_identification(now)

    assert session.last_federated_auth_at == now
    assert session.has_fresh_federated_identification(now, _WINDOW, _ABSOLUTE_TTL) is True


def test_recording_identification_never_moves_created_at_nor_expires_at() -> None:
    session = _session()
    expires_at_before = session.expires_at

    session.record_fresh_identification(_CREATED_AT + timedelta(minutes=10))

    assert session.created_at == _CREATED_AT
    assert session.expires_at == expires_at_before


def test_recording_identification_never_pushes_past_the_absolute_ttl() -> None:
    session = _session(expires_at=_CREATED_AT + _ABSOLUTE_TTL)
    now = _CREATED_AT + _ABSOLUTE_TTL - timedelta(seconds=30)

    session.record_fresh_identification(now)

    assert session.expires_at <= _CREATED_AT + _ABSOLUTE_TTL


def test_recording_identification_never_rewrites_the_origin() -> None:
    session = _session(origin=SessionOrigin.PASSWORD)

    session.record_fresh_identification(_CREATED_AT + timedelta(minutes=1))

    assert session.origin is SessionOrigin.PASSWORD

"""`OAuthConnectSession`: un solo uso, caducidad, sin `code`/`code_verifier`
(data-model.md `ReconnectSession`, contracts/rest-api.md §Conexiones)."""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta

import pytest

from safent_ads.accounts.domain.errors import InvalidStateTransitionError
from safent_ads.accounts.domain.oauth_connect_session import (
    OAuthConnectSession,
    OAuthSessionStatus,
)
from safent_ads.shared.ids import BusinessId, PlatformCode

_NOW = datetime(2026, 9, 9, tzinfo=UTC)


def _session(**overrides: object) -> OAuthConnectSession:
    defaults: dict[str, object] = {
        "session_id": uuid.uuid4(),
        "business_id": BusinessId.new(),
        "owner_id": uuid.uuid4(),
        "provider": PlatformCode.GOOGLE,
        "state_hash": "a" * 64,
        "expires_at": _NOW + timedelta(minutes=10),
    }
    defaults.update(overrides)
    return OAuthConnectSession(**defaults)  # type: ignore[arg-type]


def test_starts_waiting() -> None:
    assert _session().status == OAuthSessionStatus.WAITING


def test_mark_ok_once() -> None:
    session = _session()

    session.mark_ok(at=_NOW)

    assert session.status == OAuthSessionStatus.OK
    assert session.completed_at == _NOW


def test_mark_ok_twice_raises_single_use() -> None:
    session = _session()
    session.mark_ok(at=_NOW)

    with pytest.raises(InvalidStateTransitionError):
        session.mark_ok(at=_NOW)


def test_mark_error_records_code() -> None:
    session = _session()

    session.mark_error(error_code="provider_denied", at=_NOW)

    assert session.status == OAuthSessionStatus.ERROR
    assert session.error_code == "provider_denied"


def test_expired_session_cannot_be_resolved() -> None:
    session = _session(expires_at=_NOW - timedelta(seconds=1))

    with pytest.raises(InvalidStateTransitionError):
        session.mark_ok(at=_NOW)


def test_is_expired() -> None:
    session = _session(expires_at=_NOW + timedelta(minutes=1))
    assert session.is_expired(_NOW) is False
    assert session.is_expired(_NOW + timedelta(minutes=2)) is True


def test_exact_expiry_boundary_cannot_be_completed() -> None:
    session = _session(expires_at=_NOW)
    assert session.is_expired(_NOW)
    with pytest.raises(InvalidStateTransitionError):
        session.mark_ok(at=_NOW)

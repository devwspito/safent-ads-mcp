"""`PlatformCredential.needs_reconnect()` y transiciones de estado
(data-model.md `CredentialRef`, threat-model.md C-21)."""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta

import pytest

from safent_ads.accounts.domain.errors import InvalidStateTransitionError
from safent_ads.accounts.domain.events import CredentialInvalidated
from safent_ads.accounts.domain.platform_credential import (
    CredentialHealth,
    CredentialStatus,
    PlatformCredential,
    classify_credential_health,
    error_code_for_health,
)
from safent_ads.accounts.domain.refs import CredentialRefId
from safent_ads.shared.ids import BusinessId, PlatformCode

_NOW = datetime(2026, 9, 9, tzinfo=UTC)


def _credential(**overrides: object) -> PlatformCredential:
    defaults: dict[str, object] = {
        "credential_ref_id": CredentialRefId(uuid.uuid4()),
        "business_id": BusinessId.new(),
        "platform": PlatformCode.GOOGLE,
        "alias": "alias-1",
        "scopes": frozenset({"https://www.googleapis.com/auth/adwords"}),
    }
    defaults.update(overrides)
    return PlatformCredential(**defaults)  # type: ignore[arg-type]


def test_starts_connected_and_does_not_need_reconnect() -> None:
    credential = _credential()
    assert credential.status == CredentialStatus.CONNECTED
    assert credential.needs_reconnect() is False


def test_mark_invalid_needs_reconnect_and_emits_event() -> None:
    credential = _credential()

    credential.mark_invalid(at=_NOW, reason="refresh_denied")

    assert credential.status == CredentialStatus.INVALID
    assert credential.needs_reconnect() is True
    events = credential.pull_events()
    assert len(events) == 1
    assert isinstance(events[0], CredentialInvalidated)
    assert events[0].reason == "refresh_denied"


def test_mark_validated_clears_invalid_status() -> None:
    credential = _credential()
    credential.mark_invalid(at=_NOW, reason="refresh_denied")

    credential.mark_validated(at=_NOW, expires_at=None)

    assert credential.status == CredentialStatus.CONNECTED
    assert credential.needs_reconnect() is False
    assert credential.last_validated_at == _NOW


def test_revoke_is_terminal() -> None:
    credential = _credential()

    credential.revoke(at=_NOW)

    assert credential.status == CredentialStatus.REVOKED
    assert credential.needs_reconnect() is True
    with pytest.raises(InvalidStateTransitionError):
        credential.revoke(at=_NOW)


def test_mark_validated_after_revoke_raises() -> None:
    credential = _credential()
    credential.revoke(at=_NOW)

    with pytest.raises(InvalidStateTransitionError):
        credential.mark_validated(at=_NOW, expires_at=None)


def test_mark_invalid_after_revoke_is_a_noop() -> None:
    credential = _credential()
    credential.revoke(at=_NOW)
    credential.pull_events()

    credential.mark_invalid(at=_NOW, reason="whatever")

    assert credential.status == CredentialStatus.REVOKED
    assert credential.pull_events() == []


class TestClassifyCredentialHealth:
    def test_revoked_status_is_always_revoked_health(self) -> None:
        assert (
            classify_credential_health(CredentialStatus.REVOKED, None, now=_NOW)
            == CredentialHealth.REVOKED
        )

    def test_invalid_status_maps_to_expired_health(self) -> None:
        assert (
            classify_credential_health(CredentialStatus.INVALID, None, now=_NOW)
            == CredentialHealth.EXPIRED
        )

    def test_connected_without_expiry_is_ok(self) -> None:
        assert (
            classify_credential_health(CredentialStatus.CONNECTED, None, now=_NOW)
            == CredentialHealth.OK
        )

    def test_connected_far_from_expiry_is_ok(self) -> None:
        expires_at = _NOW + timedelta(days=30)
        assert (
            classify_credential_health(CredentialStatus.CONNECTED, expires_at, now=_NOW)
            == CredentialHealth.OK
        )

    def test_connected_within_a_week_of_expiry_is_expiring_soon(self) -> None:
        expires_at = _NOW + timedelta(days=3)
        assert (
            classify_credential_health(CredentialStatus.CONNECTED, expires_at, now=_NOW)
            == CredentialHealth.EXPIRING_SOON
        )

    def test_connected_past_expiry_is_expired(self) -> None:
        expires_at = _NOW - timedelta(hours=1)
        assert (
            classify_credential_health(CredentialStatus.CONNECTED, expires_at, now=_NOW)
            == CredentialHealth.EXPIRED
        )


class TestErrorCodeForHealth:
    def test_ok_has_no_error_code(self) -> None:
        assert error_code_for_health(CredentialHealth.OK) is None

    @pytest.mark.parametrize(
        ("health", "expected"),
        [
            (CredentialHealth.EXPIRING_SOON, "TOKEN_EXPIRING_SOON"),
            (CredentialHealth.EXPIRED, "TOKEN_EXPIRED"),
            (CredentialHealth.REVOKED, "CREDENTIAL_REVOKED"),
        ],
    )
    def test_unhealthy_states_carry_a_stable_code(
        self, health: CredentialHealth, expected: str
    ) -> None:
        assert error_code_for_health(health) == expected


class TestRecordHealthCheck:
    def test_always_moves_checked_at_and_error_code(self) -> None:
        credential = _credential()

        credential.record_health_check(
            at=_NOW, status=CredentialStatus.CONNECTED, expires_at=None, error_code=None
        )

        assert credential.checked_at == _NOW
        assert credential.last_error_code is None

    def test_connected_result_validates_and_updates_expiry(self) -> None:
        credential = _credential()
        expires_at = _NOW + timedelta(days=30)

        credential.record_health_check(
            at=_NOW, status=CredentialStatus.CONNECTED, expires_at=expires_at, error_code=None
        )

        assert credential.status == CredentialStatus.CONNECTED
        assert credential.expires_at == expires_at
        assert credential.last_validated_at == _NOW

    def test_expired_result_marks_invalid_with_the_error_code_as_reason(self) -> None:
        credential = _credential()
        expires_at = _NOW - timedelta(hours=1)

        credential.record_health_check(
            at=_NOW,
            status=CredentialStatus.EXPIRED,
            expires_at=expires_at,
            error_code="TOKEN_EXPIRED",
        )

        assert credential.status == CredentialStatus.INVALID
        assert credential.last_error_code == "TOKEN_EXPIRED"
        assert credential.expires_at == expires_at

    def test_revoked_result_revokes(self) -> None:
        credential = _credential()

        credential.record_health_check(
            at=_NOW,
            status=CredentialStatus.REVOKED,
            expires_at=None,
            error_code="CREDENTIAL_REVOKED",
        )

        assert credential.status == CredentialStatus.REVOKED
        assert credential.revoked_at == _NOW

    def test_is_a_noop_on_status_once_already_revoked(self) -> None:
        credential = _credential()
        credential.revoke(at=_NOW)

        later = _NOW + timedelta(hours=1)
        credential.record_health_check(
            at=later, status=CredentialStatus.CONNECTED, expires_at=None, error_code=None
        )

        assert credential.status == CredentialStatus.REVOKED
        assert credential.checked_at == later

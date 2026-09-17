"""`Owner` (data-model.md §Owner/Session): reglas de enrolamiento TOTP,
puras, sin criptografia."""

from __future__ import annotations

import uuid
from datetime import UTC, datetime

import pytest

from safent_ads.iam.domain.email import Email, InvalidEmailError
from safent_ads.iam.domain.errors import TotpAlreadyEnrolledError, TotpNotEnrolledError
from safent_ads.iam.domain.owner import Owner


def _owner(*, totp_secret: bytes | None = None, confirmed: datetime | None = None) -> Owner:
    return Owner(
        owner_id=uuid.uuid4(),
        email=Email("owner@safent.example"),
        password_hash="argon2id$...",
        totp_secret_encrypted=totp_secret,
        totp_confirmed_at=confirmed,
        created_at=datetime(2026, 1, 1, tzinfo=UTC),
    )


def test_email_normalizes_case() -> None:
    assert str(Email("Owner@Safent.Example")) == "owner@safent.example"


def test_email_rejects_malformed_address() -> None:
    with pytest.raises(InvalidEmailError):
        Email("not-an-email")


def test_fresh_owner_is_not_totp_enrolled() -> None:
    owner = _owner()

    assert owner.is_totp_enrolled is False
    with pytest.raises(TotpNotEnrolledError):
        owner.require_totp_enrolled()


def test_begin_totp_enrollment_stores_the_encrypted_secret() -> None:
    owner = _owner()

    owner.begin_totp_enrollment(b"encrypted-blob")

    assert owner.totp_secret_encrypted == b"encrypted-blob"
    assert owner.is_totp_enrolled is False


def test_begin_totp_enrollment_twice_when_already_confirmed_raises() -> None:
    owner = _owner(totp_secret=b"blob", confirmed=datetime(2026, 1, 2, tzinfo=UTC))

    with pytest.raises(TotpAlreadyEnrolledError):
        owner.begin_totp_enrollment(b"new-blob")


def test_confirm_totp_enrollment_without_pending_secret_raises() -> None:
    owner = _owner()

    with pytest.raises(TotpNotEnrolledError):
        owner.confirm_totp_enrollment(datetime(2026, 1, 1, tzinfo=UTC))


def test_confirm_totp_enrollment_marks_owner_enrolled() -> None:
    owner = _owner(totp_secret=b"blob")

    owner.confirm_totp_enrollment(datetime(2026, 1, 2, tzinfo=UTC))

    assert owner.is_totp_enrolled is True
    owner.require_totp_enrolled()

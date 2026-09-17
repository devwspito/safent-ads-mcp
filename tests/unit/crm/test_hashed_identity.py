"""`HashedIdentity`: sha256 salada por negocio, nunca guarda el crudo
(threat-model.md C-31)."""

from __future__ import annotations

import pytest

from safent_ads.crm.domain.errors import (
    BlankDigestError,
    BlankRawIdentifierError,
    BlankSaltError,
)
from safent_ads.crm.domain.hashed_identity import HashedIdentity
from safent_ads.shared.ids import BusinessId

_RAW_EMAIL = "lead@example.com"


def _compute(
    business_id: BusinessId, *, raw_identifier: str = _RAW_EMAIL, salt: str
) -> HashedIdentity:
    return HashedIdentity.compute(business_id=business_id, raw_identifier=raw_identifier, salt=salt)


def test_compute_is_deterministic_for_same_input() -> None:
    business_id = BusinessId.new()

    first = _compute(business_id, salt="s1")
    second = _compute(business_id, salt="s1")

    assert first == second


def test_compute_differs_across_salts() -> None:
    business_id = BusinessId.new()

    a = _compute(business_id, salt="s1")
    b = _compute(business_id, salt="s2")

    assert a.digest != b.digest


def test_compute_normalizes_case_and_whitespace() -> None:
    business_id = BusinessId.new()

    a = _compute(business_id, raw_identifier=" Lead@Example.com ", salt="s1")
    b = _compute(business_id, salt="s1")

    assert a.digest == b.digest


def test_digest_never_contains_the_raw_identifier() -> None:
    identity = _compute(BusinessId.new(), salt="s1")

    assert _RAW_EMAIL not in identity.digest
    assert len(identity.digest) == 64  # sha256 hex digest


def test_rejects_blank_raw_identifier() -> None:
    with pytest.raises(BlankRawIdentifierError):
        _compute(BusinessId.new(), raw_identifier="  ", salt="s1")


def test_rejects_blank_salt() -> None:
    with pytest.raises(BlankSaltError):
        _compute(BusinessId.new(), salt="")


def test_from_digest_reconstructs_without_hashing() -> None:
    business_id = BusinessId.new()

    identity = HashedIdentity.from_digest(business_id=business_id, digest="abc123")

    assert identity.digest == "abc123"
    assert identity.business_id == business_id


def test_from_digest_rejects_blank() -> None:
    with pytest.raises(BlankDigestError):
        HashedIdentity.from_digest(business_id=BusinessId.new(), digest="")

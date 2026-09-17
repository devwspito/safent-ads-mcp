"""`IdentityMapping` (spec 027, data-model.md §IdentityMapping): solo-
anexable, fundir escribe `merged_into` sin borrar la fila absorbida."""

from __future__ import annotations

import hashlib
from datetime import UTC, datetime

import pytest

from safent_ads.crm.domain.errors import InvalidIdentityDigestError
from safent_ads.crm.domain.identity_mapping import IdentityMapping, require_hashed_digest
from safent_ads.shared.ids import BusinessId

_OBSERVED_AT = datetime(2026, 1, 1, tzinfo=UTC)
_DIGEST = hashlib.sha256(b"salt:a@example.com").hexdigest()
_OTHER_DIGEST = hashlib.sha256(b"salt:b@example.com").hexdigest()


def _mapping(*, salt_version: int = 1) -> IdentityMapping:
    return IdentityMapping(
        business_id=BusinessId.new(),
        identity_digest=_DIGEST,
        salt_version=salt_version,
        observed_at=_OBSERVED_AT,
    )


def test_valid_digest_is_accepted() -> None:
    mapping = _mapping()

    assert mapping.merged_into is None


@pytest.mark.parametrize("bad", ["cliente@example.com", "abc123", "x" * 63, "x" * 65, ""])
def test_non_hex64_digest_is_rejected(bad: str) -> None:
    with pytest.raises(InvalidIdentityDigestError):
        require_hashed_digest(bad)


def test_merge_into_sets_merged_into_without_losing_the_row() -> None:
    mapping = _mapping()

    merged = mapping.merge_into(surviving_digest=_OTHER_DIGEST, observed_at=_OBSERVED_AT)

    assert merged.identity_digest == _DIGEST
    assert merged.merged_into == _OTHER_DIGEST


def test_salt_version_must_be_positive() -> None:
    with pytest.raises(ValueError, match="salt_version"):
        _mapping(salt_version=0)

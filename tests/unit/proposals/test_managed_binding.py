"""Managed authority is signed data, never an implicit owner permission."""

import hashlib
import json
from dataclasses import FrozenInstanceError, replace
from uuid import UUID

import pytest

from safent_ads.accounts.domain.refs import AccountRef
from safent_ads.iam.application.managed_ads_authority import ManagedAdsBinding as PublicBinding
from safent_ads.proposals.domain.proposal import ProposedDiff
from safent_ads.shared.ids import EntityLevel, EntityRef, PlatformCode
from safent_ads.shared.managed_ads import ManagedAdsBinding


def binding():
    return ManagedAdsBinding(
        UUID(int=1),
        1,
        UUID(int=2),
        UUID(int=3),
        UUID(int=4),
        UUID(int=5),
        AccountRef(PlatformCode.GOOGLE, "1234567890", UUID(int=6), UUID(int=7)),
        1,
    )


def entity():
    b = binding()
    return EntityRef(
        PlatformCode.GOOGLE,
        EntityLevel.CAMPAIGN,
        "123",
        b.account.business_id,
        b.account.connection_id,
    )


def test_unmanaged_hash_remains_exact_legacy_bytes():
    payload = {
        "entity_ref": str(entity()),
        "parameter": "status",
        "before": "ACTIVE",
        "after": "PAUSED",
    }
    expected = hashlib.sha256(
        json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()
    assert ProposedDiff.build(entity(), "status", "ACTIVE", "PAUSED").diff_hash == expected


def test_public_reexport_preserves_existing_authority_account_length_contract():
    assert PublicBinding is ManagedAdsBinding
    b = binding()
    longest = replace(b, account=replace(b.account, external_account_id="1" * 128))
    assert PublicBinding.from_claims(longest.as_claims()) == longest
    with pytest.raises(ValueError, match="invalid_managed_binding"):
        replace(b, account=replace(b.account, external_account_id="1" * 129))


def test_binding_roundtrip_immutable_and_hash_changes_for_every_identity_field():
    b = binding()
    assert ManagedAdsBinding.from_claims(b.as_claims()) == b
    with pytest.raises(FrozenInstanceError):
        b.revision = 2
    diff = ProposedDiff.build(entity(), "status", "ACTIVE", "PAUSED", managed_binding=b)
    assert diff.with_new_value("ACTIVE").managed_binding == b
    for name in ("grant_id", "org_id", "user_id", "employee_id", "instance_id"):
        assert (
            ProposedDiff.build(
                entity(),
                "status",
                "ACTIVE",
                "PAUSED",
                managed_binding=replace(b, **{name: UUID(int=100)}),
            ).diff_hash
            != diff.diff_hash
        )
    for name in ("revision", "resource_revision"):
        assert (
            ProposedDiff.build(
                entity(), "status", "ACTIVE", "PAUSED", managed_binding=replace(b, **{name: 2})
            ).diff_hash
            != diff.diff_hash
        )


@pytest.mark.parametrize("value", [None, [], "x", True, 0, {}])
def test_binding_parser_rejects_invalid_json(value):
    with pytest.raises(ValueError):
        ManagedAdsBinding.from_claims(value)


@pytest.mark.parametrize(
    "field,value",
    [
        ("revision", True),
        ("revision", 0),
        ("role", "owner"),
        ("capabilities", ["execute"]),
        ("external_account_id", "act_123"),
        ("business_id", None),
        ("unknown", "x"),
    ],
)
def test_binding_parser_closed_and_strict(field, value):
    claims = binding().as_claims()
    claims[field] = value
    with pytest.raises(ValueError):
        ManagedAdsBinding.from_claims(claims)


def test_binding_cannot_be_attached_to_other_business_connection_or_platform():
    for ref in (
        replace(entity(), business_id=UUID(int=55)),
        replace(entity(), connection_id=UUID(int=55)),
        replace(entity(), platform=PlatformCode.META),
    ):
        with pytest.raises(ValueError):
            ProposedDiff.build(ref, "status", "ACTIVE", "PAUSED", managed_binding=binding())

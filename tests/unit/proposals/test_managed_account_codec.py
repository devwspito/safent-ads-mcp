"""EE claims are numeric; provider references are Google digits or Meta act_ID."""

from dataclasses import replace

import pytest

from safent_ads.shared.ids import EntityLevel, EntityRef, PlatformCode
from safent_ads.shared.managed_ads import ManagedAdsBinding, enterprise_account_ref
from tests.unit.proposals.test_managed_binding import binding


@pytest.mark.parametrize(
    "platform,prefix", [(PlatformCode.GOOGLE, ""), (PlatformCode.META, "act_")]
)
def test_provider_mapping_is_explicit_and_signed_claims_unchanged(platform, prefix):
    original = binding()
    bound = replace(original, account=replace(original.account, platform=platform))
    claims = bound.as_claims()
    native = bound.provider_account
    assert native == replace(bound.account, external_account_id=prefix + "1234567890")
    assert enterprise_account_ref(native) == bound.account
    assert ManagedAdsBinding.from_claims(claims).provider_account == native
    bound.validate_entity(
        EntityRef(
            platform,
            EntityLevel.ACCOUNT,
            native.external_account_id,
            native.business_id,
            native.connection_id,
        )
    )
    assert bound.as_claims() == claims


@pytest.mark.parametrize(
    "raw",
    [
        "123",
        "act_act_123",
        "act_",
        "act_１２３",
        "act_123/1",
        "act_123 ",
        " act_123",
        "act_1" + "0" * 128,
    ],
)
def test_meta_provider_reference_never_uses_alternate_lookup(raw):
    account = replace(binding().account, platform=PlatformCode.META, external_account_id=raw)
    with pytest.raises(ValueError, match="managed_account_format_invalid"):
        enterprise_account_ref(account)


@pytest.mark.parametrize("raw", ["act_123", "123-456", "１２３", "", "123/1"])
def test_google_never_accepts_meta_or_display_formats(raw):
    with pytest.raises(ValueError, match="managed_account_format_invalid"):
        enterprise_account_ref(replace(binding().account, external_account_id=raw))


def test_meta_account_entity_must_use_provider_reference_not_numeric_claim():
    bound = replace(binding(), account=replace(binding().account, platform=PlatformCode.META))
    with pytest.raises(ValueError, match="managed_binding_scope_mismatch"):
        bound.validate_entity(
            EntityRef(
                PlatformCode.META,
                EntityLevel.ACCOUNT,
                "1234567890",
                bound.account.business_id,
                bound.account.connection_id,
            )
        )
    assert bound.as_claims()["external_account_id"] == "1234567890"


def test_codec_preserves_exact_business_connection_and_leading_digits():
    bound = replace(
        binding(),
        account=replace(binding().account, platform=PlatformCode.META, external_account_id="00123"),
    )
    native = bound.provider_account
    assert native.external_account_id == "act_00123"
    assert enterprise_account_ref(native) == bound.account
    assert native.business_id == bound.account.business_id
    assert native.connection_id == bound.account.connection_id

"""`AccountRef.parse`/`__str__` round trip (`platform_account_id` de la
superficie REST de conexiones)."""

from __future__ import annotations

import pytest

from safent_ads.accounts.domain.refs import AccountRef, AccountRefFormatError
from safent_ads.shared.ids import PlatformCode


def test_str_and_parse_round_trip() -> None:
    ref = AccountRef(PlatformCode.GOOGLE, "1234567890")

    assert str(ref) == "google:1234567890"
    assert AccountRef.parse(str(ref)) == ref


def test_parse_meta_account_id_with_prefix() -> None:
    ref = AccountRef.parse("meta:act_123")

    assert ref.platform == PlatformCode.META
    assert ref.external_account_id == "act_123"


@pytest.mark.parametrize("raw", ["", "google", "google:", ":123", "tiktok:123"])
def test_parse_rejects_malformed_input(raw: str) -> None:
    with pytest.raises(AccountRefFormatError):
        AccountRef.parse(raw)

"""Incidente 2026-09-15: sin esta entrada, `PlatformCapabilityNotImplementedError`
caia en el `except Exception` del dispatcher y salia como FAILED/adapter_error."""

from __future__ import annotations

from safent_ads.broker.platforms.errors import PlatformCapabilityNotImplementedError
from safent_ads.broker.platforms.meta_ad_library import MetaAdLibraryIdentityRequiredError
from safent_ads.broker.presentation.dispatcher import _KNOWN_ERROR_CODES


def test_capability_not_implemented_is_a_known_denial_code() -> None:
    assert _KNOWN_ERROR_CODES[PlatformCapabilityNotImplementedError] == (
        "PLATFORM_CAPABILITY_NOT_IMPLEMENTED"
    )


def test_meta_ad_library_identity_required_is_a_known_denial_code() -> None:
    """fix/ad-library-identity-reason: without this entry,
    `MetaAdLibraryIdentityRequiredError` falls into the generic `except
    Exception` and leaves ads-api unable to tell it apart from any other
    `MetaAdLibraryError` -- same incident shape as the test above."""
    assert _KNOWN_ERROR_CODES[MetaAdLibraryIdentityRequiredError] == (
        "META_AD_LIBRARY_IDENTITY_REQUIRED"
    )

"""`AdCopy`: `headline <= 40` es invariante de constructor (encargo del
carril), y el CTA pertenece a un conjunto cerrado por plataforma."""

from __future__ import annotations

import pytest

from safent_ads.creative.domain.copy import (
    AdCopy,
    AdCopyError,
    CtaNotAllowedForPlatformError,
    cta_label_es,
    is_cta_allowed,
)
from safent_ads.creative.domain.enums import CallToAction
from safent_ads.shared.ids import PlatformCode
from tests.unit.creative.domain.factories import make_ad_copy


def test_headline_at_40_chars_is_valid() -> None:
    copy = make_ad_copy(headline="x" * 40)

    assert len(copy.headline) == 40


def test_headline_over_40_chars_raises() -> None:
    with pytest.raises(AdCopyError):
        make_ad_copy(headline="x" * 41)


def test_empty_headline_raises() -> None:
    with pytest.raises(AdCopyError):
        make_ad_copy(headline="   ")


def test_empty_primary_text_raises() -> None:
    with pytest.raises(AdCopyError):
        make_ad_copy(primary_text="")


def test_call_now_allowed_on_google_not_meta() -> None:
    assert is_cta_allowed(CallToAction.CALL_NOW, PlatformCode.GOOGLE)
    assert not is_cta_allowed(CallToAction.CALL_NOW, PlatformCode.META)


def test_require_cta_allowed_for_raises_when_not_allowed() -> None:
    copy = make_ad_copy(cta=CallToAction.CALL_NOW)

    with pytest.raises(CtaNotAllowedForPlatformError):
        copy.require_cta_allowed_for(PlatformCode.META)


def test_require_cta_allowed_for_passes_when_allowed() -> None:
    copy = make_ad_copy(cta=CallToAction.CALL_NOW)

    copy.require_cta_allowed_for(PlatformCode.GOOGLE)


def test_every_cta_has_a_spanish_label() -> None:
    for cta in CallToAction:
        assert cta_label_es(cta)


def test_default_language_is_es_es() -> None:
    copy = AdCopy(headline="Titular", primary_text="Texto principal.", cta=CallToAction.LEARN_MORE)

    assert copy.language.value == "es-ES"

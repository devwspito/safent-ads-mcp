"""`evaluate_policy`: normas de plataforma antes de proponer (spec.md FR-32,
threat-model.md C-30)."""

from __future__ import annotations

from safent_ads.creative.domain.enums import CallToAction, Placement, PolicyVerdictResult
from safent_ads.creative.domain.policy import PolicyCheckContext, evaluate_policy
from safent_ads.shared.ids import PlatformCode
from tests.unit.creative.domain.factories import make_ad_copy


def _context(**overrides: object) -> PolicyCheckContext:
    defaults: dict[str, object] = {
        "platform": PlatformCode.META,
        "placement": Placement.FEED,
        "ad_copy": make_ad_copy(),
    }
    defaults.update(overrides)
    return PolicyCheckContext(**defaults)  # type: ignore[arg-type]


def test_clean_copy_passes() -> None:
    verdict = evaluate_policy(_context())

    assert verdict.verdict == PolicyVerdictResult.PASS_
    assert verdict.is_publishable


def test_garantizado_fails() -> None:
    copy = make_ad_copy(primary_text="Aprobado garantizado con nuestro método.")

    verdict = evaluate_policy(_context(ad_copy=copy))

    assert verdict.verdict == PolicyVerdictResult.FAIL
    assert any(f.code == "FORBIDDEN_CLAIM" for f in verdict.findings)


def test_gratis_without_flag_fails() -> None:
    copy = make_ad_copy(primary_text="Prepárate gratis con nosotros.")

    verdict = evaluate_policy(_context(ad_copy=copy))

    assert verdict.verdict == PolicyVerdictResult.FAIL
    assert any(f.code == "UNVERIFIED_FREE_CLAIM" for f in verdict.findings)


def test_gratis_with_flag_passes() -> None:
    copy = make_ad_copy(primary_text="Prepárate gratis con nosotros.")

    verdict = evaluate_policy(_context(ad_copy=copy, is_genuinely_free=True))

    assert verdict.verdict == PolicyVerdictResult.PASS_


def test_personal_attribute_inference_fails() -> None:
    copy = make_ad_copy(primary_text="Sabemos que si eres parado necesitas esto.")

    verdict = evaluate_policy(_context(ad_copy=copy))

    assert any(f.code == "PERSONAL_ATTRIBUTE_INFERENCE" for f in verdict.findings)


def test_model_style_tic_fails() -> None:
    copy = make_ad_copy(
        primary_text="En el mundo actual, prepárate con nosotros para el lanzamiento."
    )

    verdict = evaluate_policy(_context(ad_copy=copy))

    assert any(f.code == "MODEL_STYLE_TIC" for f in verdict.findings)


def test_excessive_punctuation_is_a_style_tic() -> None:
    copy = make_ad_copy(primary_text="Apúntate ya!!! No te lo pierdas!!!")

    verdict = evaluate_policy(_context(ad_copy=copy))

    assert any(f.code == "MODEL_STYLE_TIC" for f in verdict.findings)


def test_caps_abuse_is_a_warning_not_a_failure() -> None:
    copy = make_ad_copy(headline="APUNTATE YA A TU PLAZA")

    verdict = evaluate_policy(_context(ad_copy=copy))

    assert verdict.verdict == PolicyVerdictResult.WARN
    assert any(f.code == "CAPS_ABUSE" for f in verdict.findings)


def test_meta_text_coverage_over_20_percent_is_advisory_warning() -> None:
    verdict = evaluate_policy(_context(text_coverage_pct=35.0))

    assert verdict.verdict == PolicyVerdictResult.WARN
    assert any(f.code == "TEXT_COVERAGE_ADVISORY" for f in verdict.findings)


def test_meta_text_coverage_under_20_percent_has_no_finding() -> None:
    verdict = evaluate_policy(_context(text_coverage_pct=5.0))

    assert not any(f.code == "TEXT_COVERAGE_ADVISORY" for f in verdict.findings)


def test_text_coverage_advisory_does_not_apply_outside_meta_feed() -> None:
    verdict = evaluate_policy(
        _context(platform=PlatformCode.GOOGLE, placement=Placement.SEARCH, text_coverage_pct=90.0)
    )

    assert not any(f.code == "TEXT_COVERAGE_ADVISORY" for f in verdict.findings)


def test_destination_not_allowlisted_fails() -> None:
    verdict = evaluate_policy(
        _context(
            destination_url="https://phishing.example/landing",
            allowed_destination_domains=frozenset({"ejemplo.es"}),
        )
    )

    assert verdict.verdict == PolicyVerdictResult.FAIL
    assert any(f.code == "DESTINATION_NOT_ALLOWLISTED" for f in verdict.findings)


def test_destination_allowlisted_passes() -> None:
    verdict = evaluate_policy(
        _context(
            destination_url="https://ejemplo.es/landing",
            allowed_destination_domains=frozenset({"ejemplo.es"}),
        )
    )

    assert not any(f.code == "DESTINATION_NOT_ALLOWLISTED" for f in verdict.findings)


def test_model_rendered_exact_copy_fails() -> None:
    verdict = evaluate_policy(_context(exact_copy_rendered_by_model=True))

    assert verdict.verdict == PolicyVerdictResult.FAIL
    assert any(f.code == "MODEL_RENDERED_EXACT_COPY" for f in verdict.findings)


def test_primary_text_over_platform_limit_warns() -> None:
    copy = make_ad_copy(primary_text="x" * 130, cta=CallToAction.LEARN_MORE)

    verdict = evaluate_policy(_context(ad_copy=copy))

    assert any(f.code == "PRIMARY_TEXT_TOO_LONG" for f in verdict.findings)

"""threat-model.md R-2/ME-5: los nombres de
recurso de acciones de conversion nunca sobreviven en texto de
diagnostico."""

from __future__ import annotations

from safent_ads.shared.diagnostic_redaction import REDACTED, redact_diagnostic_text


def test_conversion_action_resource_name_is_redacted() -> None:
    text = "conversion goal customers/1112223333/conversionActions/456 unusable"

    redacted = redact_diagnostic_text(text)

    assert "conversionActions/456" not in redacted
    assert REDACTED in redacted


def test_text_without_conversion_action_is_unchanged() -> None:
    text = "campaign_creation_confirmation_mismatch"

    assert redact_diagnostic_text(text) == text

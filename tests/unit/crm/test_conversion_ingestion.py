"""`build_conversion_signal`/`resolve_conversion_attribution` (T220):
nucleo compartido de `POST /conversions/import` y `POST /conversions/
webhook` -- valida forma, hashea la identidad, nunca guarda el dato
crudo (threat-model.md C-31)."""

from __future__ import annotations

from datetime import UTC, datetime

import pytest

from safent_ads.crm.application.conversion_ingestion import (
    ConversionRow,
    ConversionRowError,
    build_conversion_signal,
    resolve_conversion_attribution,
)
from safent_ads.crm.domain.conversion_kind import ConversionKind
from safent_ads.crm.domain.lead_attribution import AttributionRung
from safent_ads.shared.ids import BusinessId

_BUSINESS_ID = BusinessId.new()
_SALT = "test-salt"
_NOW = datetime(2026, 9, 10, tzinfo=UTC)


def _row(**overrides: object) -> ConversionRow:
    fields: dict[str, object] = {
        "kind": ConversionKind.BUSINESS_CONVERSION,
        "occurred_at": datetime(2026, 9, 1, tzinfo=UTC),
        "amount_minor": 110_000,
        "currency": "EUR",
        "offering_id": None,
        "gclid": None,
        "fbclid": None,
        "external_ref": None,
        "email": "lead@example.com",
        "phone": None,
    }
    fields.update(overrides)
    return ConversionRow(**fields)  # type: ignore[arg-type]


def test_builds_a_signal_hashing_the_email() -> None:
    signal = build_conversion_signal(_BUSINESS_ID, _row(), salt=_SALT, observed_at=_NOW)

    assert signal.hashed_identity is not None
    assert signal.hashed_identity.digest != "lead@example.com"


def test_prefers_email_then_phone_then_external_ref() -> None:
    by_email = build_conversion_signal(
        _BUSINESS_ID,
        _row(email="a@x.com", phone="+34600000000", external_ref="ext-1"),
        salt=_SALT,
        observed_at=_NOW,
    )
    by_email_again = build_conversion_signal(
        _BUSINESS_ID, _row(email="a@x.com"), salt=_SALT, observed_at=_NOW
    )

    assert by_email.hashed_identity == by_email_again.hashed_identity


def test_rejects_row_without_any_identity() -> None:
    with pytest.raises(ConversionRowError, match="email, phone o external_ref"):
        build_conversion_signal(
            _BUSINESS_ID,
            _row(email=None, phone=None, external_ref=None),
            salt=_SALT,
            observed_at=_NOW,
        )


def test_rejects_unsupported_currency() -> None:
    with pytest.raises(ConversionRowError, match="currency"):
        build_conversion_signal(_BUSINESS_ID, _row(currency="USD"), salt=_SALT, observed_at=_NOW)


def test_accepts_blank_currency() -> None:
    signal = build_conversion_signal(
        _BUSINESS_ID, _row(currency=""), salt=_SALT, observed_at=_NOW
    )
    assert signal.value_minor == 110_000


def test_rejects_malformed_offering_id() -> None:
    with pytest.raises(ConversionRowError, match="offering_id"):
        build_conversion_signal(
            _BUSINESS_ID, _row(offering_id="not-a-uuid"), salt=_SALT, observed_at=_NOW
        )


def test_accepts_well_formed_offering_id() -> None:
    signal = build_conversion_signal(
        _BUSINESS_ID,
        _row(offering_id="4f6f3a2e-6f3a-4a2e-9f3a-2e6f3a4a2e9f"),
        salt=_SALT,
        observed_at=_NOW,
    )
    assert signal.hashed_identity is not None


def test_resolve_without_entity_maps_falls_back_to_aggregate() -> None:
    signal = build_conversion_signal(_BUSINESS_ID, _row(), salt=_SALT, observed_at=_NOW)

    attribution = resolve_conversion_attribution(signal)

    assert attribution.attribution_rung is AttributionRung.AGGREGATE
    assert attribution.entity_ref is None

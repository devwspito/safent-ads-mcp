"""`parse_conversion_signal`: lista blanca de campos, ningun dato personal
crudo llega al modelo (threat-model.md C-31, `test_no_pii_in_model_context`).
`ConversionFieldMapping` hace el mapeo declarativo: un CRM con otro
vocabulario de campos JSON pasa su propia instancia, nunca toca el modulo."""

from __future__ import annotations

import dataclasses

import pytest

from safent_ads.crm.infrastructure.conversion_mapper import (
    ConversionFieldMapping,
    parse_conversion_signal,
)
from safent_ads.crm.infrastructure.errors import CrmRequestError
from safent_ads.shared.ids import BusinessId

_BUSINESS_ID = BusinessId.new()

_VALID_PAYLOAD = {
    "conversion_kind": "lead",
    "value_minor": 1_500,
    "occurred_at": "2026-09-08T10:00:00+00:00",
    "observed_at": "2026-09-09T09:00:00+00:00",
    "gclid": "g1",
    "fbclid": None,
    "utm_campaign": "campana-septiembre",
    "hashed_identity_digest": "abc123",
}


def test_maps_a_valid_whitelisted_payload() -> None:
    signal = parse_conversion_signal(_BUSINESS_ID, dict(_VALID_PAYLOAD))

    assert signal.business_id == _BUSINESS_ID
    assert signal.gclid == "g1"
    assert signal.hashed_identity is not None
    assert signal.hashed_identity.digest == "abc123"


def test_no_pii_in_model_context() -> None:
    payload_with_pii = dict(_VALID_PAYLOAD) | {
        "email": "lead@example.com",
        "phone": "+34600000000",
        "full_name": "Nombre Apellido",
    }

    with pytest.raises(CrmRequestError):
        parse_conversion_signal(_BUSINESS_ID, payload_with_pii)


def test_resulting_signal_has_no_field_capable_of_holding_raw_pii() -> None:
    signal = parse_conversion_signal(_BUSINESS_ID, dict(_VALID_PAYLOAD))

    field_names = {field.name for field in dataclasses.fields(signal)}
    assert field_names == {
        "business_id",
        "conversion_kind",
        "value_minor",
        "occurred_at",
        "observed_at",
        "gclid",
        "fbclid",
        "utm_campaign",
        "hashed_identity",
        "calendar_event_id",
    }


def test_rejects_malformed_payload() -> None:
    malformed = dict(_VALID_PAYLOAD)
    del malformed["conversion_kind"]

    with pytest.raises(CrmRequestError):
        parse_conversion_signal(_BUSINESS_ID, malformed)


def test_custom_field_mapping_reads_a_differently_named_payload() -> None:
    mapping = ConversionFieldMapping(
        conversion_kind="kind",
        value_minor="amount_cents",
        occurred_at="happened_at",
        observed_at="seen_at",
        gclid="gclid",
        fbclid="fbclid",
        utm_campaign="campaign",
        hashed_identity_digest="identity_hash",
    )
    payload = {
        "kind": "lead",
        "amount_cents": 1_500,
        "happened_at": "2026-09-08T10:00:00+00:00",
        "seen_at": "2026-09-09T09:00:00+00:00",
        "gclid": "g1",
        "fbclid": None,
        "campaign": None,
        "identity_hash": None,
    }

    signal = parse_conversion_signal(_BUSINESS_ID, payload, mapping)

    assert signal.value_minor == 1_500
    assert signal.gclid == "g1"


def test_custom_field_mapping_still_rejects_unexpected_fields() -> None:
    mapping = ConversionFieldMapping(value_minor="amount_cents")
    payload = dict(_VALID_PAYLOAD) | {"amount_cents": 1_500}

    with pytest.raises(CrmRequestError):
        parse_conversion_signal(_BUSINESS_ID, payload, mapping)

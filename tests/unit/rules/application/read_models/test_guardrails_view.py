"""`parse_scope_ref` (`GET /guardrails?scope_ref`): distingue un
`business_id` (UUID) de un `account_ref` (`<platform>:<external_id>`) sin
ambiguedad -- funcion pura, sin sesion."""

from __future__ import annotations

import uuid

import pytest

from safent_ads.rules.application.read_models.guardrails_view import (
    InvalidScopeRefError,
    parse_scope_ref,
)


def test_a_uuid_parses_as_a_business_scope() -> None:
    business_id = uuid.uuid4()

    kind, ref = parse_scope_ref(str(business_id))

    assert kind == "business"
    assert ref == str(business_id)


def test_an_account_ref_parses_as_an_account_scope() -> None:
    kind, ref = parse_scope_ref("google:100-000-0002")

    assert kind == "account"
    assert ref == "google:100-000-0002"


def test_a_meta_account_ref_parses_as_an_account_scope() -> None:
    kind, ref = parse_scope_ref("meta:act_100000000000001")

    assert kind == "account"
    assert ref == "meta:act_100000000000001"


@pytest.mark.parametrize(
    "scope_ref",
    ["not-a-uuid-or-ref", "unknown_platform:123", "google:", ":123", ""],
)
def test_neither_a_uuid_nor_a_known_platform_ref_is_rejected(scope_ref: str) -> None:
    with pytest.raises(InvalidScopeRefError):
        parse_scope_ref(scope_ref)

"""`InstanceIdentity` (data-model.md §InstanceIdentity, plan.md §3): como
se llama ESTE servidor para quien lo autoriza, distinto de `brand_name`
(el negocio del que se anuncia)."""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from safent_ads.composition.settings import InstanceIdentity
from tests.unit.composition.factories import build_api_settings


def test_default_instance_name_is_generic_never_a_fixed_client_name() -> None:
    settings = build_api_settings()

    assert settings.instance_name == "Ads MCP"


def test_instance_name_strips_surrounding_whitespace() -> None:
    settings = build_api_settings(instance_name="  Acme Ads MCP  ")

    assert settings.instance_name == "Acme Ads MCP"


@pytest.mark.parametrize("value", ["", "   ", "x" * 65])
def test_instance_name_rejects_empty_or_too_long_values(value: str) -> None:
    with pytest.raises(ValidationError, match="ADS_INSTANCE_NAME"):
        build_api_settings(instance_name=value)


def test_instance_name_accepts_the_maximum_length() -> None:
    settings = build_api_settings(instance_name="x" * 64)

    assert settings.instance_name == "x" * 64


def test_panel_host_is_derived_from_public_base_url_never_a_separate_value() -> None:
    settings = build_api_settings(public_base_url="https://ads.example.com")

    identity = InstanceIdentity.from_settings(settings)

    assert identity.panel_host == "ads.example.com"


def test_from_settings_carries_the_configured_instance_name() -> None:
    settings = build_api_settings(instance_name="Acme Ads MCP")

    identity = InstanceIdentity.from_settings(settings)

    assert identity.name == "Acme Ads MCP"

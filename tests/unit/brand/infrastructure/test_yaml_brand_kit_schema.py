"""`parse_brand_kit_yaml`: puro (mismo patron que
`broker.infrastructure.caps_config.parse_caps_config`), sin tocar el
sistema de ficheros."""

from __future__ import annotations

import pytest

from safent_ads.brand.domain.claims_policy import DEFAULT_FORBIDDEN_CLAIMS
from safent_ads.brand.infrastructure.errors import BrandKitYamlError
from safent_ads.brand.infrastructure.yaml_brand_kit_schema import parse_brand_kit_yaml
from safent_ads.shared.clock import FixedClock
from safent_ads.shared.ids import BusinessId
from tests.unit.brand.factories import FIXED_UPDATED_AT

_CLOCK = FixedClock(FIXED_UPDATED_AT)

_MINIMAL_YAML = """
typography:
  primary_family: "Fake Sans"
  licence_note: "Google Fonts, SIL OFL 1.1"
tone_of_voice:
  description: "Cercano y claro."
palette:
  - role: primary
    hex: "#101010"
    contrast_ratio_on_white: 18.0
assets:
  - asset_id: "logo-principal"
    kind: logo_vector
    storage_uri: "s3://brand/example/logo.svg"
    usage_rule: "no deformar"
"""


def test_parses_minimal_valid_document() -> None:
    business_id = BusinessId.new()

    kit = parse_brand_kit_yaml(_MINIMAL_YAML, business_id=business_id, clock=_CLOCK)

    assert kit.business_id == business_id
    assert kit.typography.primary_family == "Fake Sans"
    assert kit.palette.swatches[0].hex == "#101010"
    assert DEFAULT_FORBIDDEN_CLAIMS <= {c for c in kit.forbidden_claims}


def test_business_id_comes_from_the_caller_not_the_yaml() -> None:
    """El YAML no tiene campo `business_id` a proposito
    (`BrandKitPayload` lo rechaza por `extra=forbid`): un fichero mal
    editado no puede escribir el kit de otro negocio."""
    business_id = BusinessId.new()
    yaml_with_business_id = _MINIMAL_YAML + '\nbusiness_id: "should-be-ignored"\n'

    with pytest.raises(BrandKitYamlError):
        parse_brand_kit_yaml(yaml_with_business_id, business_id=business_id, clock=_CLOCK)


def test_rejects_invalid_yaml() -> None:
    with pytest.raises(BrandKitYamlError):
        parse_brand_kit_yaml("not: [valid, yaml", business_id=BusinessId.new(), clock=_CLOCK)


def test_rejects_non_mapping_root() -> None:
    with pytest.raises(BrandKitYamlError):
        parse_brand_kit_yaml("- just\n- a\n- list\n", business_id=BusinessId.new(), clock=_CLOCK)


def test_rejects_missing_required_field() -> None:
    incomplete_yaml = "typography:\n  primary_family: x\n"

    with pytest.raises(BrandKitYamlError):
        parse_brand_kit_yaml(incomplete_yaml, business_id=BusinessId.new(), clock=_CLOCK)


def test_domain_invariant_violation_is_wrapped_as_yaml_error() -> None:
    """`primary_family` en blanco viola `Typography.__post_init__`: el
    esquema pydantic no lo detecta (no hay `min_length`), asi que el
    invariante de dominio es la ultima linea de defensa."""
    blank_family_yaml = _MINIMAL_YAML.replace('primary_family: "Fake Sans"', 'primary_family: " "')

    with pytest.raises(BrandKitYamlError):
        parse_brand_kit_yaml(blank_family_yaml, business_id=BusinessId.new(), clock=_CLOCK)


def test_owner_forbidden_claims_are_merged_with_the_baseline() -> None:
    yaml_with_extra_claim = _MINIMAL_YAML + '\nforbidden_claims:\n  - "nota media mas alta"\n'

    kit = parse_brand_kit_yaml(yaml_with_extra_claim, business_id=BusinessId.new(), clock=_CLOCK)

    assert "nota media mas alta" in kit.forbidden_claims
    assert DEFAULT_FORBIDDEN_CLAIMS <= {c for c in kit.forbidden_claims}

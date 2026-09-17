"""`load_brand_kit_yaml`: unico punto que toca el sistema de ficheros
(mismo patron que `broker.infrastructure.caps_config.load_caps_config`).
Tambien fija la plantilla de ejemplo que rellena el propietario
(`config/brand/example.yaml`) contra el esquema, para que un cambio de
esquema no la deje rota en silencio."""

from __future__ import annotations

from pathlib import Path

import pytest

from safent_ads.brand.infrastructure.errors import BrandKitYamlError
from safent_ads.brand.infrastructure.yaml_brand_kit_loader import load_brand_kit_yaml
from safent_ads.shared.clock import FixedClock
from safent_ads.shared.ids import BusinessId
from tests.unit.brand.factories import FIXED_UPDATED_AT

_CLOCK = FixedClock(FIXED_UPDATED_AT)
_REPO_ROOT = Path(__file__).resolve().parents[4]


def test_raises_when_file_is_missing(tmp_path: Path) -> None:
    missing_path = tmp_path / "does-not-exist.yaml"

    with pytest.raises(BrandKitYamlError):
        load_brand_kit_yaml(missing_path, business_id=BusinessId.new(), clock=_CLOCK)


def test_loads_a_valid_file(tmp_path: Path) -> None:
    yaml_path = tmp_path / "kit.yaml"
    yaml_path.write_text(
        "typography:\n"
        '  primary_family: "Fake Sans"\n'
        '  licence_note: "SIL OFL"\n'
        "tone_of_voice:\n"
        '  description: "Cercano."\n',
        encoding="utf-8",
    )

    kit = load_brand_kit_yaml(yaml_path, business_id=BusinessId.new(), clock=_CLOCK)

    assert kit.typography.primary_family == "Fake Sans"


def test_example_brand_kit_still_matches_the_schema() -> None:
    """`config/brand/example.yaml` conserva placeholders (`REEMPLAZAR`) a
    proposito -- validos contra el esquema, aunque `is_complete()` los
    marque como incompletos."""
    example_path = _REPO_ROOT / "config" / "brand" / "example.yaml"

    kit = load_brand_kit_yaml(example_path, business_id=BusinessId.new(), clock=_CLOCK)

    assert kit.is_complete() is False

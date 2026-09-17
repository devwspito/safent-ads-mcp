"""Unico punto que toca el sistema de ficheros para `brand`
(mismo patron que `broker.infrastructure.caps_config.load_caps_config`)."""

from __future__ import annotations

from pathlib import Path

from safent_ads.brand.domain.brand_kit import BrandKit
from safent_ads.brand.infrastructure.errors import BrandKitYamlError
from safent_ads.brand.infrastructure.yaml_brand_kit_schema import parse_brand_kit_yaml
from safent_ads.shared.clock import Clock
from safent_ads.shared.ids import BusinessId


def load_brand_kit_yaml(path: Path, *, business_id: BusinessId, clock: Clock) -> BrandKit:
    if not path.is_file():
        raise BrandKitYamlError(f"fichero de kit de marca no encontrado: {path}")
    raw_yaml = path.read_text(encoding="utf-8")
    return parse_brand_kit_yaml(raw_yaml, business_id=business_id, clock=clock)

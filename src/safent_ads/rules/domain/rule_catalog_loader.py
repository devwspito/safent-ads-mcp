"""Carga el catalogo empaquetado en `rules/catalog/rules.yaml` (tasks.md
T038). `load_default_catalog` es el unico punto que toca el sistema de
ficheros; `parse_catalog` (rule_catalog_schema.py) sigue siendo pura."""

from __future__ import annotations

from pathlib import Path

from safent_ads.rules.domain.rule import Rule
from safent_ads.rules.domain.rule_catalog_schema import parse_catalog

DEFAULT_CATALOG_PATH = Path(__file__).resolve().parent.parent / "catalog" / "rules.yaml"


def load_default_catalog(path: Path = DEFAULT_CATALOG_PATH) -> tuple[Rule, ...]:
    return parse_catalog(path.read_text(encoding="utf-8"))

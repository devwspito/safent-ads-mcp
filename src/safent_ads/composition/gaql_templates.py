"""Carga las plantillas GAQL versionadas (package data `broker/platforms/gaql/*.gaql`, T162,
profitability-engine.md §2: "las ventanas son ficheros GAQL de gaarf
versionados: la consulta deja de ser codigo"). Cada fichero se valida al
cargarlo con el MISMO validador de `run_gaql`
(`broker.platforms.gaql_validator.validate_gaql`), no en cada llamada --
un fichero mal formado falla el arranque, no una peticion en produccion.

Lineas que empiezan por `--` son comentario y se descartan antes de
validar (documentan que consulta actual sustituyen, GAQL en si no tiene
sintaxis de comentario propia).

**Estado tras el cableado (T162 follow-up)**: `composition/broker.py`
carga las cinco plantillas de `GoogleAdsAdapter` con `load_gaql_template`
y las inyecta en su constructor (`GoogleAdsQueryTemplates`); el adaptador
ya no construye esas consultas desde constantes Python en tiempo de
ejecucion -- las constantes/`_build_select` siguen vivas solo como
version de referencia con la que `tests/unit/composition/
test_gaql_templates.py` prueba, caracter a caracter, que cada fichero
`.gaql` reproduce lo mismo. `search_terms` (P2, `mcp.infrastructure.
broker_search_term_read_port`) no tiene equivalente inline previo -- se
valida igual, sin comparacion byte a byte contra nada."""

from __future__ import annotations

import importlib.resources
import re
from pathlib import Path
from typing import Final

from safent_ads.broker.platforms.errors import GaqlValidationError
from safent_ads.broker.platforms.gaql_validator import validate_gaql
from safent_ads.shared.errors import InfrastructureError

__all__ = [
    "GaqlTemplateError",
    "GAQL_TEMPLATE_NAMES",
    "load_gaql_template",
]

# Package data (`safent_ads/broker/platforms/gaql/*.gaql`), resuelto con
# importlib.resources: la aritmetica `Path(__file__).parents[N]` apuntaba a
# `<raiz>/config/gaql` en el checkout pero a `.venv/lib/python3.12/config`
# dentro de la imagen (paquete instalado) y ads-api moria al arrancar (T214).
_GAQL_PACKAGE: Final = "safent_ads.broker.platforms"

_WHITESPACE_RUN: Final = re.compile(r"\s+")

GAQL_TEMPLATE_NAMES: Final = (
    "campaign_inventory",
    "ad_group_inventory",
    "ad_inventory",
    "campaign_metrics",
    "campaign_budget_lookup",
    # `list_search_terms` (tool-surface.md P2, `mcp.infrastructure.
    # broker_search_term_read_port`): sin equivalente inline previo, no
    # entra en `tests/unit/composition/test_gaql_templates.py` byte a byte.
    "search_terms",
)


class GaqlTemplateError(InfrastructureError):
    """Plantilla ausente o que no supera `validate_gaql` -- fallo de
    arranque, nunca un `SELECT` a medias servido en produccion."""


def load_gaql_template(name: str, *, gaql_dir: Path | None = None) -> str:
    """Lee `<gaql_dir>/<name>.gaql`, descarta lineas de comentario (`--`) y
    valida el resultado con `validate_gaql` antes de devolverlo. Sin
    `WHERE`/filtros de tiempo de ejecucion incluidos a proposito quien
    llama los concatena (mismo comportamiento que las constantes que
    sustituye)."""
    filename = f"{name}.gaql"
    try:
        if gaql_dir is not None:
            raw = (gaql_dir / filename).read_text(encoding="utf-8")
        else:
            raw = (
                importlib.resources.files(_GAQL_PACKAGE)
                .joinpath("gaql", filename)
                .read_text(encoding="utf-8")
            )
    except (OSError, TypeError) as exc:
        raise GaqlTemplateError(f"plantilla GAQL no encontrada: {filename}") from exc
    query = _normalize(raw)
    try:
        validate_gaql(query)
    except GaqlValidationError as exc:
        raise GaqlTemplateError(f"plantilla GAQL invalida ({name}): {exc}") from exc
    return query


def _normalize(raw: str) -> str:
    """Descarta comentarios (`--`) y colapsa el salto de linea/sangria de
    formato humano a un unico espacio entre tokens: GAQL, como SQL, es
    insensible a los espacios entre palabras -- el fichero se formatea para
    legibilidad, la cadena que viaja a la API es de una sola linea, igual
    que las constantes que sustituye."""
    lines = [line for line in raw.splitlines() if not line.lstrip().startswith("--")]
    return _WHITESPACE_RUN.sub(" ", " ".join(lines)).strip()

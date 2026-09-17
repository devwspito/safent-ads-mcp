"""INV-15 (tasks.md T002, plan.md):
ningun literal de canal ni de puja de Google sobrevive fuera de
`proposals/domain/google_channel_spec.py` -- esa tabla (T010) es la unica
autoridad de dominio. Analiza el AST de cada fichero en vez de hacer un
grep de texto: la comparacion es por igualdad exacta de un
`ast.Constant` de tipo `str`, nunca por subcadena, asi que un nombre de
campo GAQL como `"campaign.advertising_channel_type"` no cuenta.

Deuda explicita: `_PENDIENTES` nace (T002, hoy) con exactamente los ocho
ficheros que este mismo analisis encuentra antes de que exista la tabla.
Cada tarea que migra uno de ellos a `google_channel_spec.py` borra su
linea; T070 vacia el conjunto entero y esta prueba queda pura."""

from __future__ import annotations

import ast
from pathlib import Path
from typing import Final

_SRC_ROOT = Path(__file__).resolve().parents[2] / "src" / "safent_ads"

_CHANNEL_AND_BIDDING_LITERALS: Final[frozenset[str]] = frozenset(
    {"SEARCH", "MANUAL_CPC", "DISPLAY", "DEMAND_GEN", "PERFORMANCE_MAX"}
)

_EXEMPT_FILE: Final[str] = "proposals/domain/google_channel_spec.py"

# Congelado el 2026-09-15 (T002) con el grep de hoy de
# `research.md §hoy`: exactamente los ficheros que este analisis
# encuentra antes de que exista `google_channel_spec.py`. Cada tarea que
# migra un fichero a la tabla borra su linea; ninguna tarea anade una
# linea nueva -- ese es el sentido de "no crece" mas abajo.
_PENDIENTES: Final[frozenset[str]] = frozenset()

# Techo congelado el dia de T002: el tamano de `_PENDIENTES` de hoy. Cada
# migracion borra su linea de `_PENDIENTES`, nunca anade una -- este techo
# no se toca ni siquiera cuando el conjunto encoge, para que
# `test_la_lista_de_pendientes_no_crece` seguir siendo una comprobacion
# real y no una tautologia contra si misma.
_PENDIENTES_CEILING: Final[int] = 8


def _string_constants(path: Path) -> set[str]:
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    return {
        node.value
        for node in ast.walk(tree)
        if isinstance(node, ast.Constant) and isinstance(node.value, str)
    }


def _files_with_channel_literals() -> dict[str, set[str]]:
    offenders: dict[str, set[str]] = {}
    for path in sorted(_SRC_ROOT.rglob("*.py")):
        rel = str(path.relative_to(_SRC_ROOT))
        if rel == _EXEMPT_FILE:
            continue
        found = _string_constants(path) & _CHANNEL_AND_BIDDING_LITERALS
        if found:
            offenders[rel] = found
    return offenders


def test_ningun_literal_de_canal_fuera_de_la_tabla() -> None:
    offenders = _files_with_channel_literals()

    fuera_de_pendientes = set(offenders) - _PENDIENTES
    assert fuera_de_pendientes == set(), (
        "literal de canal/puja fuera de google_channel_spec.py y de "
        f"_PENDIENTES: {sorted(fuera_de_pendientes)}"
    )


def test_la_lista_de_pendientes_no_crece() -> None:
    assert len(_PENDIENTES) <= _PENDIENTES_CEILING, (
        "_PENDIENTES solo puede migrar (encoger), nunca ganar una entrada "
        f"nueva: {len(_PENDIENTES)} > techo congelado {_PENDIENTES_CEILING}"
    )


def test_la_lista_de_pendientes_no_tiene_entradas_muertas() -> None:
    offenders = _files_with_channel_literals()

    muertas = {rel for rel in _PENDIENTES if rel not in offenders}
    assert muertas == set(), (
        "entrada de _PENDIENTES sin ningun literal ya -- borrar su linea "
        f"(migracion olvidada de borrar la entrada): {sorted(muertas)}"
    )

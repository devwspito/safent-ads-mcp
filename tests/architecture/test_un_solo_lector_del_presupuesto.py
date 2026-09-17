"""T016 (BL-5, T-7): `check_hard_caps`
solo confia en `money_minor_units` (`broker/domain/write_authorization.py`),
que delega en `creation_budget`/`_google`
(`proposals/domain/campaign_creation.py`) para leer el importe de un plan
de creacion -- el UNICO camino que alimenta el tope duro independiente del
broker (T014). Analiza el AST en vez de grep de texto: una funcion "parsea
`creation_plan.daily_budget`" si subindexa o `.get(...)` la clave
`"daily_budget"` sobre un valor que a su vez vino de subindexar/`.get(...)`
la clave `"creation_plan"` -- exactamente el patron que competiria con
`creation_budget` sin pasar por el.

`live_google_ads_client.py`/`live_meta_graph_client.py` (construyen la
mutacion real) y `value_codec.py`/`panel_read.py`/`platform_completeness.py`
(spend_ledger, panel, huella del paquete) ya delegan en `creation_budget`
en vez de leer `daily_budget` a mano -- no aparecen en `_EXCEPCIONES`
porque el analisis no los encuentra.

`_EXCEPCIONES` es deuda declarada, no una licencia: `packages/infrastructure/
chokepoint_step_executor.py::_money_pair` estima el impacto de un paso de
paquete ANTES de que llegue al broker (003, fuera de esta feature y de
este carril -- `packages/**` es de otro carril). No decide el tope duro
(`check_hard_caps` no lo llama), pero duplica el parseo del importe fuera
de `creation_budget`: si su forma diverge de la de `_google`, el impacto
mostrado y el importe realmente autorizado podrian dejar de coincidir.
Congelado el 2026-09-15 (T016); no crece -- migrarlo a `creation_budget`
es trabajo del carril de `packages/**`."""

from __future__ import annotations

import ast
from pathlib import Path
from typing import Final

_SRC_ROOT = Path(__file__).resolve().parents[2] / "src" / "safent_ads"

_AUTHORITY_FILE: Final[str] = "proposals/domain/campaign_creation.py"

_EXCEPCIONES: Final[frozenset[str]] = frozenset()


def _key_read(node: ast.AST) -> tuple[ast.AST, object] | None:
    """Si `node` es `obj[key]` o `obj.get(key)` con `key` una constante,
    devuelve `(obj, key)`; si no, `None`."""
    if isinstance(node, ast.Subscript) and isinstance(node.slice, ast.Constant):
        return node.value, node.slice.value
    if (
        isinstance(node, ast.Call)
        and isinstance(node.func, ast.Attribute)
        and node.func.attr == "get"
        and node.args
        and isinstance(node.args[0], ast.Constant)
    ):
        return node.func.value, node.args[0].value
    return None


def _reads_creation_plan(node: ast.AST) -> bool:
    return any(
        (read := _key_read(sub)) is not None and read[1] == "creation_plan"
        for sub in ast.walk(node)
    )


def _names_assigned_from_creation_plan(tree: ast.AST) -> set[str]:
    names: set[str] = set()
    for node in ast.walk(tree):
        if (
            isinstance(node, ast.Assign)
            and len(node.targets) == 1
            and isinstance(node.targets[0], ast.Name)
            and _reads_creation_plan(node.value)
        ):
            names.add(node.targets[0].id)
    return names


def _parses_daily_budget_from_creation_plan(path: Path) -> bool:
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    plan_names = _names_assigned_from_creation_plan(tree)
    for node in ast.walk(tree):
        read = _key_read(node)
        if read is None or read[1] != "daily_budget":
            continue
        source = read[0]
        if isinstance(source, ast.Name) and source.id in plan_names:
            return True
        if _reads_creation_plan(source):
            return True
    return False


def _offenders() -> set[str]:
    offenders: set[str] = set()
    for path in sorted(_SRC_ROOT.rglob("*.py")):
        rel = str(path.relative_to(_SRC_ROOT))
        if rel == _AUTHORITY_FILE:
            continue
        if _parses_daily_budget_from_creation_plan(path):
            offenders.add(rel)
    return offenders


def test_ninguna_otra_funcion_parsea_creation_plan_daily_budget() -> None:
    fuera_de_excepciones = _offenders() - _EXCEPCIONES
    assert fuera_de_excepciones == set(), (
        "lector de creation_plan.daily_budget fuera de creation_budget y de "
        f"_EXCEPCIONES: {sorted(fuera_de_excepciones)}"
    )


def test_las_excepciones_congeladas_no_tienen_entradas_muertas() -> None:
    offenders = _offenders()
    muertas = {rel for rel in _EXCEPCIONES if rel not in offenders}
    assert muertas == set(), (
        "entrada de _EXCEPCIONES sin ningun lector ya -- borrar su linea "
        f"(migracion olvidada de borrar la entrada): {sorted(muertas)}"
    )

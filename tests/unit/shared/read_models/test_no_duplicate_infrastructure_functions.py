"""Guarda contra que la duplicacion resuelta en `rules.application
.read_models.caps_and_pacing` vuelva a aparecer en silencio:
`panel/infrastructure/sql_read_model.py` y
`mcp/infrastructure/sql_portfolio_read_port.py` no deben volver a declarar,
cada uno por su lado, una funcion/metodo con el mismo cuerpo o una consulta
SQL literal con el mismo texto -- esa era exactamente la forma en que
`_caps_and_pacing`, `_SELECT_ONE_ACCOUNT_REF_FOR_BUSINESS`,
`_days_in_month`, `_money` y `_to_major` divergieron antes de moverse a
`rules.application.read_models.caps_and_pacing` (N4: el mas alto del que
dependen, panel y mcp se sientan por encima en plan.md §4).
Analiza el AST de cada fichero en vez de importar los paquetes: una funcion
nueva que replique un cuerpo ya existente en el contexto hermano no se
detectaria en tiempo de import."""

from __future__ import annotations

import ast
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parents[4]
_PANEL_READ_MODEL = (
    _REPO_ROOT / "src" / "safent_ads" / "panel" / "infrastructure" / "sql_read_model.py"
)
_MCP_READ_PORT = (
    _REPO_ROOT / "src" / "safent_ads" / "mcp" / "infrastructure" / "sql_portfolio_read_port.py"
)

# Cuerpos mas cortos que esto (en nodos AST) son demasiado genericos para
# ser una duplicacion real (p.ej. un getter de una linea) -- umbral fijado
# al tamano del cuerpo mas pequeno de los que motivaron esta guarda
# (`_to_major`, dos sentencias).
_MIN_BODY_AST_NODES = 4


def _function_bodies(path: Path) -> dict[str, str]:
    """Nombre -> huella AST del cuerpo (sin nombre de funcion/metodo, para
    detectar duplicados aunque se renombren) de cada funcion o metodo
    declarado en `path`, filtrando cuerpos triviales."""
    tree = ast.parse(path.read_text())
    bodies: dict[str, str] = {}
    for node in ast.walk(tree):
        if not isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef):
            continue
        if node.name.startswith("__") and node.name.endswith("__"):
            continue  # __init__ y compania: boilerplate de asignacion, no proyeccion de lectura
        body_module = ast.Module(body=node.body, type_ignores=[])
        dump = ast.dump(body_module)
        if sum(1 for _ in ast.walk(body_module)) < _MIN_BODY_AST_NODES:
            continue
        bodies[node.name] = dump
    return bodies


def _sql_literals(path: Path) -> dict[str, str]:
    """Nombre de constante -> texto SQL de cada `text(...)` asignado a un
    nombre de nivel de modulo (las `_SELECT_*`/`_COUNT_*` de este proyecto)."""
    tree = ast.parse(path.read_text())
    literals: dict[str, str] = {}
    for node in tree.body:
        if not isinstance(node, ast.Assign) or not isinstance(node.value, ast.Call):
            continue
        call = node.value
        if not (isinstance(call.func, ast.Name) and call.func.id == "text"):
            continue
        if len(call.args) != 1 or not isinstance(call.args[0], ast.Constant):
            continue
        target = node.targets[0]
        if isinstance(target, ast.Name) and isinstance(call.args[0].value, str):
            literals[target.id] = call.args[0].value.strip()
    return literals


def test_panel_and_mcp_do_not_redeclare_function_bodies() -> None:
    panel_bodies = _function_bodies(_PANEL_READ_MODEL)
    mcp_bodies = _function_bodies(_MCP_READ_PORT)

    duplicates = {
        (panel_name, mcp_name): dump
        for panel_name, dump in panel_bodies.items()
        for mcp_name, mcp_dump in mcp_bodies.items()
        if dump == mcp_dump
    }

    assert duplicates == {}, (
        f"panel y mcp redeclaran funciones/metodos con el mismo cuerpo "
        f"(deberian vivir en rules.application.read_models, mismo criterio que "
        f"caps_and_pacing): {sorted(duplicates)}"
    )


def test_panel_and_mcp_do_not_redeclare_sql_literals() -> None:
    panel_sql = _sql_literals(_PANEL_READ_MODEL)
    mcp_sql = _sql_literals(_MCP_READ_PORT)

    duplicates = {
        (panel_name, mcp_name): sql
        for panel_name, sql in panel_sql.items()
        for mcp_name, mcp_sql_text in mcp_sql.items()
        if sql == mcp_sql_text
    }

    assert duplicates == {}, (
        f"panel y mcp redeclaran la misma consulta SQL literal (deberia vivir en "
        f"rules.application.read_models, mismo criterio que "
        f"_SELECT_ONE_ACCOUNT_REF_FOR_BUSINESS): {sorted(duplicates)}"
    )

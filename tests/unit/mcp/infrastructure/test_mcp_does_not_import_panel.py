"""Guarda arquitectonica de plan.md §4 ("mcp no importa panel, ni al reves;
ambos leen, nunca importan"). Analiza el AST de cada fichero fuente de
`safent_ads.mcp` en vez de importar el paquete: una comprobacion en tiempo
de import no detectaria un `import` puesto dentro de una funcion y nunca
ejecutado en el proceso de test."""

from __future__ import annotations

import ast
from pathlib import Path

_MCP_SRC_ROOT = Path(__file__).resolve().parents[4] / "src" / "safent_ads" / "mcp"
_FORBIDDEN_PREFIX = "safent_ads.panel"


def _imported_module_names(source: str) -> set[str]:
    tree = ast.parse(source)
    names: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            names.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            names.add(node.module)
    return names


def test_no_mcp_source_file_imports_panel() -> None:
    offenders: dict[str, set[str]] = {}
    for path in _MCP_SRC_ROOT.rglob("*.py"):
        imported = _imported_module_names(path.read_text())
        forbidden = {name for name in imported if name.startswith(_FORBIDDEN_PREFIX)}
        if forbidden:
            offenders[str(path.relative_to(_MCP_SRC_ROOT))] = forbidden
    assert offenders == {}, f"mcp importa panel (plan.md §4): {offenders}"

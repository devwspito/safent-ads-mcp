"""Guarda contra que la duplicacion de simplification-audit.md #9 vuelva a
aparecer en silencio: `panel/application/dto.py` y `mcp/application/dto.py`
no deben volver a declarar localmente dos DTOs con el mismo conjunto de
campos (mismo nombre + misma anotacion, en el mismo orden) o dos StrEnum
con los mismos miembros -- esa era exactamente la forma en que `Money`,
`Caps`, `CapsSource`, `SpendBreakdown`, `SignalOutcome`,
`SignalOutcomeStatus`, `Freshness`/`FreshnessInfo` y `Pacing`/
`PacingOverview` divergieron antes de moverse a `shared.read_models.dto`.
Analiza el AST de cada fichero en vez de importar los paquetes: una clase
nueva que replique un DTO de `shared` sin usarlo no se detectaria en tiempo
de import."""

from __future__ import annotations

import ast
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parents[4]
_PANEL_DTO = _REPO_ROOT / "src" / "safent_ads" / "panel" / "application" / "dto.py"
_MCP_DTO = _REPO_ROOT / "src" / "safent_ads" / "mcp" / "application" / "dto.py"

_DATACLASS_DECORATOR_NAMES = {"dataclass"}
_FieldSet = tuple[tuple[str, str], ...]


def _is_dataclass(node: ast.ClassDef) -> bool:
    for decorator in node.decorator_list:
        target = decorator.func if isinstance(decorator, ast.Call) else decorator
        name = target.id if isinstance(target, ast.Name) else None
        if name in _DATACLASS_DECORATOR_NAMES:
            return True
    return False


def _is_str_enum(node: ast.ClassDef) -> bool:
    return any(ast.unparse(base) in {"StrEnum", "str, Enum"} for base in node.bases)


def _dataclass_field_set(node: ast.ClassDef) -> _FieldSet:
    fields = []
    for stmt in node.body:
        if isinstance(stmt, ast.AnnAssign) and isinstance(stmt.target, ast.Name):
            fields.append((stmt.target.id, ast.unparse(stmt.annotation)))
    return tuple(fields)


def _enum_member_set(node: ast.ClassDef) -> _FieldSet:
    members = []
    for stmt in node.body:
        if isinstance(stmt, ast.Assign):
            for target in stmt.targets:
                if isinstance(target, ast.Name):
                    members.append((target.id, ast.unparse(stmt.value)))
    return tuple(members)


def _locally_declared_dtos(path: Path) -> tuple[dict[str, _FieldSet], dict[str, _FieldSet]]:
    """Devuelve (dataclasses, str_enums) declarados directamente en `path`
    (no los importados de `shared.read_models.dto` u otro modulo)."""
    tree = ast.parse(path.read_text())
    dataclasses_by_name: dict[str, _FieldSet] = {}
    enums_by_name: dict[str, _FieldSet] = {}
    for node in tree.body:
        if not isinstance(node, ast.ClassDef):
            continue
        if _is_dataclass(node):
            fields = _dataclass_field_set(node)
            if fields:
                dataclasses_by_name[node.name] = fields
        elif _is_str_enum(node):
            members = _enum_member_set(node)
            if members:
                enums_by_name[node.name] = members
    return dataclasses_by_name, enums_by_name


def test_panel_and_mcp_do_not_redeclare_dtos_with_the_same_field_set() -> None:
    panel_dataclasses, panel_enums = _locally_declared_dtos(_PANEL_DTO)
    mcp_dataclasses, mcp_enums = _locally_declared_dtos(_MCP_DTO)

    duplicate_dataclasses = {
        (panel_name, mcp_name): fields
        for panel_name, fields in panel_dataclasses.items()
        for mcp_name, mcp_fields in mcp_dataclasses.items()
        if fields == mcp_fields
    }
    duplicate_enums = {
        (panel_name, mcp_name): members
        for panel_name, members in panel_enums.items()
        for mcp_name, mcp_members in mcp_enums.items()
        if members == mcp_members
    }

    assert duplicate_dataclasses == {}, (
        f"panel y mcp redeclaran dataclasses con el mismo conjunto de campos "
        f"(deberian vivir en shared.read_models.dto, simplification-audit.md #9): "
        f"{duplicate_dataclasses}"
    )
    assert duplicate_enums == {}, (
        f"panel y mcp redeclaran StrEnum con los mismos miembros "
        f"(deberian vivir en shared.read_models.dto, simplification-audit.md #9): "
        f"{duplicate_enums}"
    )

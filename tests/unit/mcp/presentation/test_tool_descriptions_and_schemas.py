"""004 tasks-2.md I1: dos guardas de calidad sobre el registro COMPLETO
(mismo constructor real que `test_catalog_registries_by_permission.py`).

1. Toda herramienta tiene una descripcion de al menos 40 caracteres: un
   agente que solo ve el nombre y una frase de cinco palabras no sabe
   cuando usarla ni que argumentos espera -- ningun `ToolDefinition` nuevo
   puede colarse con una descripcion telegráfica.
2. El esquema de argumentos de toda herramienta se puede RESOLVER por
   completo: inlinear cada `$ref` de `$defs` contra su definicion, de
   forma recursiva, sin dejar ningun `$ref` suelto. Esto no prohibe
   sub-modelos anidados (pydantic los declara via `$defs`, y varias
   herramientas antiguas como `propose_ad_child` los usan) -- prohibe que
   alguno sea CIRCULAR (una definicion que se referencia a si misma,
   directa o transitivamente), que es precisamente lo que un cliente MCP
   que aplana el esquema no puede representar."""

from __future__ import annotations

import copy
import json
from typing import Any

from safent_ads.mcp.presentation.registry import ToolRegistry
from tests.unit.mcp.presentation.test_catalog_registries_by_permission import _full_registry

_MIN_DESCRIPTION_LENGTH = 40
_LOCAL_DEFS_PREFIX = "#/$defs/"


def _full_registry_fixture() -> ToolRegistry:
    return _full_registry()


def test_every_tool_description_is_at_least_forty_characters() -> None:
    registry = _full_registry_fixture()
    too_short = {
        definition.name: len(definition.description)
        for definition in registry
        if len(definition.description) < _MIN_DESCRIPTION_LENGTH
    }
    assert not too_short, f"descripciones demasiado cortas (< 40 chars): {too_short}"


def test_every_tool_args_schema_resolves_without_a_dangling_ref() -> None:
    registry = _full_registry_fixture()
    unresolved: dict[str, str] = {}
    for definition in registry:
        schema = definition.args_model.model_json_schema()
        defs = schema.get("$defs", {})
        try:
            resolved = _inline_refs(schema, defs, frozenset())
        except _CircularRefError as exc:
            unresolved[definition.name] = str(exc)
            continue
        if "$ref" in json.dumps(resolved):
            unresolved[definition.name] = "leftover $ref after full inlining"
    assert not unresolved, f"esquemas con $ref sin resolver: {unresolved}"


class _CircularRefError(Exception):
    """Una definicion de `$defs` se referencia a si misma, directa o
    transitivamente -- no se puede aplanar en un esquema finito."""


def _inline_refs(node: Any, defs: dict[str, Any], seen: frozenset[str]) -> Any:  # noqa: ANN401
    if isinstance(node, dict):
        ref = node.get("$ref")
        if isinstance(ref, str) and ref.startswith(_LOCAL_DEFS_PREFIX):
            name = ref.removeprefix(_LOCAL_DEFS_PREFIX)
            if name in seen:
                raise _CircularRefError(f"referencia circular en $defs/{name}")
            resolved = _inline_refs(copy.deepcopy(defs[name]), defs, seen | {name})
            overrides = {key: value for key, value in node.items() if key != "$ref"}
            return {**resolved, **overrides}
        return {
            key: _inline_refs(value, defs, seen) for key, value in node.items() if key != "$defs"
        }
    if isinstance(node, list):
        return [_inline_refs(item, defs, seen) for item in node]
    return node

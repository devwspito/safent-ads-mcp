"""`passthrough_tools.py` (R5, historia 19): registro en el `ToolRegistry`
real, nombre verbo-primero, y que las excepciones de la politica de dominio
se traducen a `VALIDATION_ERROR` en el borde, una sola vez."""

from __future__ import annotations

import pytest

from safent_ads.mcp.application.caller_scope import CallerScope, Permission
from safent_ads.mcp.application.errors import EntityNotFoundError, ToolValidationError
from safent_ads.mcp.application.graph_passthrough_port import GraphPassthroughResult
from safent_ads.mcp.domain.meta_graph_path import GraphEdgeDeniedError, GraphFieldDeniedError
from safent_ads.mcp.presentation.passthrough_tools import (
    GetMetaGraphArgs,
    PassthroughToolServices,
    build_passthrough_tool_definitions,
)
from safent_ads.mcp.presentation.registry import ToolClass, ToolRegistry

_BUSINESS_ID = "9d9b8b1a-6b8e-4f0a-9d1e-8f2c6b7a5e10"
_ACCOUNT_REF = "meta:act_111"


def _caller_scope() -> CallerScope:
    return CallerScope(
        caller_id="test",
        allowed_business_ids=frozenset({_BUSINESS_ID}),
        permission=Permission.VIEW,
        person_label="Agente de prueba",
    )


class _FakeGraphPassthroughPort:
    def __init__(
        self,
        *,
        result: GraphPassthroughResult | None = None,
        error: Exception | None = None,
    ) -> None:
        self._result = result or GraphPassthroughResult(rows=(), truncated=False)
        self._error = error
        self.calls: list[tuple[str, str, str, str, tuple[str, ...]]] = []

    async def get_meta_graph(
        self,
        business_id,
        account_ref,
        *,
        node,
        edge,
        fields,
        params,  # noqa: ARG002 - firma del puerto, no usado en el doble
    ) -> GraphPassthroughResult:
        self.calls.append((business_id, account_ref, node, edge, fields))
        if self._error is not None:
            raise self._error
        return self._result


def _services(port: _FakeGraphPassthroughPort | None = None) -> PassthroughToolServices:
    return PassthroughToolServices(graph=port or _FakeGraphPassthroughPort())


def test_build_passthrough_tool_definitions_registers_get_meta_graph() -> None:
    definitions = build_passthrough_tool_definitions(_services())

    assert [d.name for d in definitions] == ["get_meta_graph"]
    assert definitions[0].tool_class is ToolClass.READ


def test_definition_passes_the_registry_naming_guard() -> None:
    ToolRegistry(build_passthrough_tool_definitions(_services()))


async def test_get_meta_graph_delegates_to_the_port() -> None:
    port = _FakeGraphPassthroughPort(
        result=GraphPassthroughResult(rows=({"id": "1", "name": "Campana"},), truncated=False)
    )
    definitions = {d.name: d for d in build_passthrough_tool_definitions(_services(port))}
    args = GetMetaGraphArgs(
        business_id=_BUSINESS_ID,
        account_ref=_ACCOUNT_REF,
        node="act_111",
        edge="campaigns",
        fields=["id", "name"],
    )

    result = await definitions["get_meta_graph"].handler(args, _caller_scope())

    assert result.rows == ({"id": "1", "name": "Campana"},)
    assert port.calls == [
        (_BUSINESS_ID, _ACCOUNT_REF, "act_111", "campaigns", ("id", "name"))
    ]


async def test_edge_policy_denial_becomes_validation_error() -> None:
    port = _FakeGraphPassthroughPort(error=GraphEdgeDeniedError("arista no permitida"))
    definitions = {d.name: d for d in build_passthrough_tool_definitions(_services(port))}
    args = GetMetaGraphArgs(
        business_id=_BUSINESS_ID, account_ref=_ACCOUNT_REF, node="act_111", fields=["id"]
    )

    with pytest.raises(ToolValidationError):
        await definitions["get_meta_graph"].handler(args, _caller_scope())


async def test_field_policy_denial_becomes_validation_error() -> None:
    port = _FakeGraphPassthroughPort(error=GraphFieldDeniedError("campo no permitido"))
    definitions = {d.name: d for d in build_passthrough_tool_definitions(_services(port))}
    args = GetMetaGraphArgs(
        business_id=_BUSINESS_ID, account_ref=_ACCOUNT_REF, node="act_111", fields=["access_token"]
    )

    with pytest.raises(ToolValidationError):
        await definitions["get_meta_graph"].handler(args, _caller_scope())


async def test_entity_not_found_from_the_port_is_not_swallowed() -> None:
    port = _FakeGraphPassthroughPort(error=EntityNotFoundError("act_999 aun no disponible"))
    definitions = {d.name: d for d in build_passthrough_tool_definitions(_services(port))}
    args = GetMetaGraphArgs(
        business_id=_BUSINESS_ID, account_ref=_ACCOUNT_REF, node="999", fields=["id"]
    )

    with pytest.raises(EntityNotFoundError):
        await definitions["get_meta_graph"].handler(args, _caller_scope())


def test_node_must_be_numeric_or_act_prefixed() -> None:
    with pytest.raises(ValueError, match="node"):
        GetMetaGraphArgs(
            business_id=_BUSINESS_ID, account_ref=_ACCOUNT_REF, node="not-a-node", fields=["id"]
        )


def test_fields_cannot_be_empty() -> None:
    """M-1: `fields=[]` dejaba la proyeccion en manos de Meta -- al menos
    un campo explicito."""
    with pytest.raises(ValueError, match="fields"):
        GetMetaGraphArgs(business_id=_BUSINESS_ID, account_ref=_ACCOUNT_REF, node="act_111")


def test_field_names_must_match_the_graph_field_pattern() -> None:
    with pytest.raises(ValueError, match="fields"):
        GetMetaGraphArgs(
            business_id=_BUSINESS_ID,
            account_ref=_ACCOUNT_REF,
            node="act_111",
            fields=["Access-Token"],
        )


def test_fields_over_30_items_are_rejected() -> None:
    with pytest.raises(ValueError, match="fields"):
        GetMetaGraphArgs(
            business_id=_BUSINESS_ID,
            account_ref=_ACCOUNT_REF,
            node="act_111",
            fields=[f"field_{i}" for i in range(31)],
        )


def test_edge_defaults_to_the_node_itself() -> None:
    args = GetMetaGraphArgs(
        business_id=_BUSINESS_ID, account_ref=_ACCOUNT_REF, node="act_111", fields=["id"]
    )

    assert args.edge == ""


def test_bj3_edge_must_match_the_closed_alphabet() -> None:
    """Bj-3: sin `pattern`, `edge` aceptaba cualquier cadena (mayusculas,
    digitos, control chars) antes de llegar a la lista blanca de dominio."""
    with pytest.raises(ValueError, match="edge"):
        GetMetaGraphArgs(
            business_id=_BUSINESS_ID,
            account_ref=_ACCOUNT_REF,
            node="act_111",
            edge="Campaigns",
            fields=["id"],
        )


def test_bj3_edge_over_forty_chars_is_rejected() -> None:
    with pytest.raises(ValueError, match="edge"):
        GetMetaGraphArgs(
            business_id=_BUSINESS_ID,
            account_ref=_ACCOUNT_REF,
            node="act_111",
            edge="a" * 41,
            fields=["id"],
        )

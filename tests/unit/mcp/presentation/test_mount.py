"""`mount.py::_gate_call_tool`: bug 1 (denegar y auditar un nombre
conocido-pero-no-montado) ya cubierto aqui; ahora tambien bug 3 (hotfix
0.2.20) -- sin transporte HTTP ni Postgres, `MCPServer.call_tool` es el
mismo punto que ve un `tools/call` real
(`tests/e2e/journeys/test_journey_permissions.py`/
`test_journey_errors_are_precise.py` lo cubren tambien contra el
transporte real), asi que ejercerlo directamente basta para fijar el
contrato sin pagar el coste de un contenedor por corrida."""

from __future__ import annotations

from typing import Annotated, Any

import pytest
from mcp.server.mcpserver import MCPServer
from mcp.server.mcpserver.exceptions import ToolError
from pydantic import BaseModel, Field

from safent_ads.mcp.application.caller_scope import CallerScope, Permission
from safent_ads.mcp.application.errors import PlatformAppNotConfiguredError
from safent_ads.mcp.presentation.args import BusinessId, ToolArgs
from safent_ads.mcp.presentation.catalog import registries_by_permission
from safent_ads.mcp.presentation.dispatcher import ToolDispatcher
from safent_ads.mcp.presentation.mount import CALLER_SCOPE_STATE_ATTR, mount_tools
from safent_ads.mcp.presentation.registry import ToolClass, ToolDefinition, ToolRegistry

_BUSINESS_ID = "11111111-1111-1111-1111-111111111111"


class _EchoArgs(ToolArgs):
    business_id: BusinessId
    amount: Annotated[str, Field(pattern=r"^\d+(\.\d{1,2})?$")]


async def _echo_handler(args: _EchoArgs, _caller: CallerScope) -> dict[str, Any]:
    return {"amount": args.amount}


def _definition(name: str, tool_class: ToolClass = ToolClass.PROPOSAL) -> ToolDefinition[Any]:
    return ToolDefinition(
        name=name,
        description="d",
        args_model=_EchoArgs,
        tool_class=tool_class,
        handler=_echo_handler,
        business_id_of=lambda args: args.business_id,
    )


class _AlwaysAllowQuota:
    async def check_and_consume(self, **_kwargs: str) -> bool:
        return True


class _RecordingAudit:
    def __init__(self) -> None:
        self.calls: list[dict[str, object]] = []

    async def record_tool_call(self, **kwargs: object) -> None:
        self.calls.append(kwargs)


class _FakeState:
    pass


class _FakeRequest:
    def __init__(self, caller_scope: CallerScope) -> None:
        self.state = _FakeState()
        setattr(self.state, CALLER_SCOPE_STATE_ATTR, caller_scope)


class _FakeRequestContext:
    def __init__(self, request: _FakeRequest) -> None:
        self.request = request


class _FakeContext:
    def __init__(self, caller_scope: CallerScope) -> None:
        self.request_context = _FakeRequestContext(_FakeRequest(caller_scope))


def _ctx(permission: Permission = Permission.VIEW) -> _FakeContext:
    scope = CallerScope("person:ana", frozenset({_BUSINESS_ID}), permission, "Ana")
    return _FakeContext(scope)


def _mounted_server(audit: _RecordingAudit, *, known_tool_names: frozenset[str]) -> MCPServer:
    registry = ToolRegistry([_definition("propose_thing")])
    dispatcher = ToolDispatcher(registry=registry, quota=_AlwaysAllowQuota(), audit=audit)
    server = MCPServer(name="test")
    mount_tools(
        server, registry=registry, dispatcher=dispatcher, known_tool_names=known_tool_names
    )
    return server


async def test_a_known_but_unmounted_name_is_denied_and_audited() -> None:
    """Bug 1: `propose_other_thing` esta en el catalogo completo
    (`known_tool_names`) pero no en el registro montado en ESTE servidor
    -- debe denegarse via `ToolDispatcher.deny`, no un "unknown tool"."""
    audit = _RecordingAudit()
    known_names = frozenset({"propose_thing", "propose_other_thing"})
    server = _mounted_server(audit, known_tool_names=known_names)

    result = await server.call_tool(
        "propose_other_thing", {"args": {"business_id": _BUSINESS_ID, "amount": "1"}}, _ctx()
    )

    assert result.is_error is False
    assert result.structured_content == {
        "error": {
            "code": "PERMISSION_DENIED",
            "message": "tu permiso (view) no alcanza a propose_other_thing.",
        }
    }
    assert len(audit.calls) == 1
    assert audit.calls[0]["outcome"] == "denied"
    assert audit.calls[0]["error_code"] == "PERMISSION_DENIED"
    assert audit.calls[0]["tool_name"] == "propose_other_thing"
    assert audit.calls[0]["caller_id"] == "person:ana"


async def test_a_truly_unknown_name_is_unaffected() -> None:
    """INV-2: un nombre que no esta en NINGUN registro sigue sin auditarse
    ni denegarse via el dispatcher -- el "unknown tool" generico del SDK,
    sin cambios (nunca se inventa una fila para un nombre que nadie declaro)."""
    audit = _RecordingAudit()
    server = _mounted_server(audit, known_tool_names=frozenset({"propose_thing"}))

    with pytest.raises(ToolError, match="Unknown tool"):
        await server.call_tool("totally_bogus", {"args": {}}, _ctx())

    assert audit.calls == []


async def test_invalid_arguments_reach_the_clean_envelope_not_raw_pydantic() -> None:
    """Bug 3: un regex mismatch en `amount` se valida en el SDK ANTES de que
    el cuerpo del wrapper se ejecute -- debe traducirse al mismo sobre
    `{"error": {...}}`, nunca el volcado crudo de pydantic ni el valor
    ofensivo que mando el llamante."""
    audit = _RecordingAudit()
    server = _mounted_server(audit, known_tool_names=frozenset({"propose_thing"}))

    result = await server.call_tool(
        "propose_thing",
        {"args": {"business_id": _BUSINESS_ID, "amount": "not-a-number"}},
        _ctx(),
    )

    assert result.is_error is False
    error = result.structured_content["error"]
    assert error["code"] == "INVALID_ARGUMENTS"
    assert error["fields"] == [
        {"path": "amount", "message": "String should match pattern '^\\d+(\\.\\d{1,2})?$'"}
    ]
    assert "pydantic.dev" not in str(result.content)
    assert "not-a-number" not in str(result.content)
    assert audit.calls == []  # never reached the dispatcher: no fila que auditar aqui


class _InternalOnlyModel(BaseModel):
    internal_field_name: int


async def _crashing_handler(_args: _EchoArgs, _caller: CallerScope) -> dict[str, Any]:
    """Simula un bug interno: un `ValidationError` que no tiene nada que
    ver con lo que mando el llamante (aqui, conversion de un modelo
    interno) escapa del handler."""
    _InternalOnlyModel.model_validate({"internal_field_name": "not-an-int"})
    return {}


async def test_an_internal_validation_error_crash_is_never_treated_as_invalid_arguments() -> None:
    """M-5: `UnexpectedToolError` (crash, no fallo de esquema de entrada)
    con un `ValidationError` interno como causa nunca debe traducirse al
    sobre de `INVALID_ARGUMENTS` -- eso expondria rutas de un modelo
    interno (`internal_field_name`) que el llamante nunca vio ni mando.
    Incidente de produccion (companion 0.2.21): tampoco debe escapar como
    `UnexpectedToolError` -- la red de seguridad generica de `mount.py` lo
    convierte en el mismo sobre `TOOL_FAILED` que cualquier otro crash."""
    audit = _RecordingAudit()
    registry = ToolRegistry(
        [
            ToolDefinition(
                name="propose_thing",
                description="d",
                args_model=_EchoArgs,
                tool_class=ToolClass.PROPOSAL,
                handler=_crashing_handler,
                business_id_of=lambda args: args.business_id,
            )
        ]
    )
    dispatcher = ToolDispatcher(registry=registry, quota=_AlwaysAllowQuota(), audit=audit)
    server = MCPServer(name="test")
    mount_tools(server, registry=registry, dispatcher=dispatcher)

    result = await server.call_tool(
        "propose_thing", {"args": {"business_id": _BUSINESS_ID, "amount": "42"}}, _ctx()
    )

    assert result.is_error is False
    assert result.structured_content == {
        "error": {
            "code": "TOOL_FAILED",
            "message": "La herramienta fallo en el servidor; el equipo de Safent tiene el detalle.",
        }
    }
    assert "internal_field_name" not in str(result.content)
    assert "pydantic.dev" not in str(result.content)
    # `ToolDispatcher.dispatch` ya audito el fallo antes de que la excepcion
    # llegara a `mount.py`: la red de seguridad no duplica la fila.
    assert len(audit.calls) == 1
    assert audit.calls[0]["outcome"] == "error"


async def test_a_mounted_name_with_valid_arguments_still_dispatches_normally() -> None:
    audit = _RecordingAudit()
    server = _mounted_server(audit, known_tool_names=frozenset({"propose_thing"}))

    result = await server.call_tool(
        "propose_thing", {"args": {"business_id": _BUSINESS_ID, "amount": "42"}}, _ctx()
    )

    assert result.is_error is False
    assert result.structured_content == {"result": {"amount": "42"}}
    assert len(audit.calls) == 1
    assert audit.calls[0]["outcome"] == "ok"


async def test_tools_list_marks_every_non_read_class_for_proponer() -> None:
    """contracts/mcp.md §3.4: `tools/list` carries `_meta[
    "anthropic/requiresUserInteraction"] = true` on every `PROPOSAL`,
    `CATALOG_WRITE` and `CREATIVE_WRITE` tool -- `READ` carries none.
    `proponer` never sees `CONNECTION_WRITE` at all (`registries_by_
    permission`, contracts/mcp.md §3), so this covers the other three
    write classes plus `READ` in the same transport-level pass (real
    `MCPServer.list_tools`, the `tools/list` handler)."""
    full_registry = ToolRegistry(
        [
            _definition("list_thing", ToolClass.READ),
            _definition("propose_thing", ToolClass.PROPOSAL),
            _definition("create_offering", ToolClass.CATALOG_WRITE),
            _definition("upload_creative_asset", ToolClass.CREATIVE_WRITE),
            _definition("connect_platform_account", ToolClass.CONNECTION_WRITE),
        ]
    )
    propose_registry = registries_by_permission(full_registry)[Permission.PROPOSE]
    dispatcher = ToolDispatcher(
        registry=propose_registry, quota=_AlwaysAllowQuota(), audit=_RecordingAudit()
    )
    server = MCPServer(name="test")
    mount_tools(server, registry=propose_registry, dispatcher=dispatcher)

    tools_by_name = {tool.name: tool for tool in await server.list_tools()}

    assert set(tools_by_name) == {
        "list_thing",
        "propose_thing",
        "create_offering",
        "upload_creative_asset",
    }
    assert tools_by_name["list_thing"].meta is None
    for name in ("propose_thing", "create_offering", "upload_creative_asset"):
        assert tools_by_name[name].meta == {"anthropic/requiresUserInteraction": True}


async def _platform_app_not_configured_handler(
    _args: _EchoArgs, _caller: CallerScope
) -> dict[str, Any]:
    """Simula lo que `BrokerReferenceDataPort` (incidente de produccion,
    companion 0.2.21) lanza ahora cuando el bróker deniega con
    `broker_op_denied error_code=PLATFORM_APP_NOT_CONFIGURED`."""
    raise PlatformAppNotConfiguredError(
        "Cuenta conectada pero sin app de plataforma ni Composio configurados en "
        "este servidor; pide al dueño configurarlo en Ajustes → Tus cuentas"
    )


async def test_a_broker_denial_reaches_the_clean_envelope_for_list_meta_pages() -> None:
    """Bug reportado en produccion: `list_meta_pages` devolvia el
    `UnexpectedToolError` opaco del SDK en vez del sobre limpio del
    contrato cuando el bróker denegaba con `PLATFORM_APP_NOT_CONFIGURED`."""
    audit = _RecordingAudit()
    registry = ToolRegistry(
        [
            ToolDefinition(
                name="list_meta_pages",
                description="d",
                args_model=_EchoArgs,
                tool_class=ToolClass.READ,
                handler=_platform_app_not_configured_handler,
                business_id_of=lambda args: args.business_id,
            )
        ]
    )
    dispatcher = ToolDispatcher(registry=registry, quota=_AlwaysAllowQuota(), audit=audit)
    server = MCPServer(name="test")
    mount_tools(server, registry=registry, dispatcher=dispatcher)

    result = await server.call_tool(
        "list_meta_pages", {"args": {"business_id": _BUSINESS_ID, "amount": "1"}}, _ctx()
    )

    assert result.is_error is False
    assert result.structured_content == {
        "error": {
            "code": "PLATFORM_APP_NOT_CONFIGURED",
            "message": (
                "Cuenta conectada pero sin app de plataforma ni Composio configurados en "
                "este servidor; pide al dueño configurarlo en Ajustes → Tus cuentas"
            ),
        }
    }
    assert audit.calls[0]["outcome"] == "error"
    assert audit.calls[0]["error_code"] == "PLATFORM_APP_NOT_CONFIGURED"

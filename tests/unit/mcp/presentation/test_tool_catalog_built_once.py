"""Perf (16-sep, item 4): measured `initialize`+`tools/list` at ~1.0 s
through the MCP. Traced the call chain (`composition/app.py::create_app`
-> `_build_mcp_surface` -> `_build_mcp_registry_and_dispatcher` ->
`mount.py::mount_tools` -> SDK's `ToolManager.add_tool` ->
`Tool.from_function`, which runs `arg_model.model_json_schema()` -- the
expensive part, pydantic introspection over every field) once per tool:
`create_app` calls this exactly once per PROCESS, building three
`MCPServer` (one per permission tier, static catalog per
`registries_by_permission`), never per session or per request --
`stateless_http=True` only means no server-side MCP *session* state
between calls, not that the tool catalog gets rebuilt. `MCPServer.
list_tools()` (SDK) only re-wraps the already-computed `Tool.parameters`
schema into a fresh `MCPTool` envelope on every call (a cheap pydantic
re-validation of an already-built dict); it never re-derives the schema.

No code change was needed for this item; this test pins the invariant so
a future change (e.g. mounting tools per request, or per MCP session)
would fail loudly instead of silently costing ~1s per call again."""

from __future__ import annotations

from typing import Any

import pytest
from mcp.server.mcpserver import MCPServer
from mcp.server.mcpserver.tools.base import Tool

from safent_ads.mcp.application.caller_scope import CallerScope
from safent_ads.mcp.presentation.args import BusinessId, ToolArgs
from safent_ads.mcp.presentation.dispatcher import ToolDispatcher
from safent_ads.mcp.presentation.mount import mount_tools
from safent_ads.mcp.presentation.registry import ToolClass, ToolDefinition, ToolRegistry


class _EchoArgs(ToolArgs):
    business_id: BusinessId


async def _echo_handler(args: _EchoArgs, _caller: CallerScope) -> dict[str, Any]:
    return {"business_id": str(args.business_id)}


class _NoOpAudit:
    async def record_tool_call(self, **_kwargs: object) -> None:
        return None


class _AlwaysAllowQuota:
    async def check_and_consume(self, **_kwargs: str) -> bool:
        return True


def _registry() -> ToolRegistry:
    definition = ToolDefinition(
        name="list_something",
        description="d",
        args_model=_EchoArgs,
        tool_class=ToolClass.READ,
        handler=_echo_handler,
        business_id_of=lambda args: args.business_id,
    )
    return ToolRegistry([definition])


def _mounted_server(registry: ToolRegistry) -> MCPServer:
    dispatcher = ToolDispatcher(registry=registry, quota=_AlwaysAllowQuota(), audit=_NoOpAudit())
    return mount_tools(MCPServer(name="test"), registry=registry, dispatcher=dispatcher)


async def test_list_tools_never_reruns_schema_derivation(monkeypatch: pytest.MonkeyPatch) -> None:
    """`Tool.from_function` is where the SDK runs `model_json_schema()` --
    the expensive step. It must run once per tool at mount time, and never
    again just because a client called `tools/list` more than once."""
    calls: list[str] = []
    original_from_function = Tool.from_function.__func__

    def counting_from_function(cls: type[Tool], fn: Any, *args: Any, **kwargs: Any) -> Tool:
        calls.append(getattr(fn, "__name__", repr(fn)))
        return original_from_function(cls, fn, *args, **kwargs)

    monkeypatch.setattr(Tool, "from_function", classmethod(counting_from_function))

    server = _mounted_server(_registry())
    assert calls == ["list_something"]

    await server.list_tools()
    await server.list_tools()
    await server.list_tools()

    assert calls == ["list_something"]


async def test_two_tools_list_calls_return_the_same_schema_content() -> None:
    """Content stays byte-for-byte identical across calls -- the wrapping
    `MCPTool` envelope is rebuilt per call (cheap), but it always reflects
    the one schema computed at mount time, never a fresh derivation."""
    server = _mounted_server(_registry())

    first = await server.list_tools()
    second = await server.list_tools()

    assert first[0].input_schema == second[0].input_schema


async def test_mounting_the_same_registry_twice_derives_schemas_independently_per_server(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Sanity check on the counting hook itself: two separate `MCPServer`
    mounts (one per permission tier in production) each pay the
    derivation once -- the cost is per (server, tool), not amortized
    across servers, which is why `create_app` must build all three once
    and keep them, never rebuild any of them later."""
    calls: list[str] = []
    original_from_function = Tool.from_function.__func__

    def counting_from_function(cls: type[Tool], fn: Any, *args: Any, **kwargs: Any) -> Tool:
        calls.append(getattr(fn, "__name__", repr(fn)))
        return original_from_function(cls, fn, *args, **kwargs)

    monkeypatch.setattr(Tool, "from_function", classmethod(counting_from_function))
    registry = _registry()

    _mounted_server(registry)
    _mounted_server(registry)

    assert calls == ["list_something", "list_something"]

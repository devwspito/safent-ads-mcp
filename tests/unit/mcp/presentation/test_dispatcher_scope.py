"""`ToolDispatcher` (spec 002 tasks.md T012): `ToolClass.PROPOSAL` exige
`ads:propose` en `CallerScope.granted_scopes`; `None` (bearer estatico) es
"sin restriccion", no "sin alcance"."""

from __future__ import annotations

import pytest

from safent_ads.mcp.application.caller_scope import CallerScope, Permission
from safent_ads.mcp.application.errors import ForbiddenScopeError
from safent_ads.mcp.presentation.args import ListBusinessesArgs
from safent_ads.mcp.presentation.dispatcher import ToolDispatcher
from safent_ads.mcp.presentation.registry import ToolClass, ToolDefinition, ToolRegistry

_PROPOSAL_TOOL = "propose_thing"
_READ_TOOL = "list_things"


class _AlwaysAllowQuota:
    async def check_and_consume(self, **_kwargs: str) -> bool:
        return True


async def _proposal_handler(
    _args: ListBusinessesArgs, _caller_scope: CallerScope
) -> dict[str, bool]:
    return {"proposed": True}


async def _read_handler(_args: ListBusinessesArgs, _caller_scope: CallerScope) -> dict[str, bool]:
    return {"read": True}


_BUSINESS_ID = "11111111-1111-1111-1111-111111111111"


def _caller(granted_scopes: frozenset[str] | None) -> CallerScope:
    """Fusion lane/003: `allowed_business_ids` es un conjunto explicito
    (nunca `None`) y el llamador lleva permiso y etiqueta de persona.
    `granted_scopes=None` sigue significando "no viene de OAuth"."""
    return CallerScope(
        caller_id="oauth:client:owner",
        allowed_business_ids=frozenset({_BUSINESS_ID}),
        permission=Permission.PROPOSE,
        person_label="Agente conectado",
        granted_scopes=granted_scopes,
    )


def _registry() -> ToolRegistry:
    return ToolRegistry(
        [
            ToolDefinition(
                name=_PROPOSAL_TOOL,
                description="fixture",
                args_model=ListBusinessesArgs,
                tool_class=ToolClass.PROPOSAL,
                handler=_proposal_handler,
                business_id_of=None,
            ),
            ToolDefinition(
                name=_READ_TOOL,
                description="fixture",
                args_model=ListBusinessesArgs,
                tool_class=ToolClass.READ,
                handler=_read_handler,
                business_id_of=None,
            ),
        ]
    )


def _dispatcher() -> ToolDispatcher:
    return ToolDispatcher(registry=_registry(), quota=_AlwaysAllowQuota())


async def test_proposal_tool_requires_the_propose_scope() -> None:
    dispatcher = _dispatcher()
    caller_scope = _caller(frozenset({"ads:read"}))

    with pytest.raises(ForbiddenScopeError):
        await dispatcher.dispatch(_PROPOSAL_TOOL, {}, caller_scope=caller_scope)


async def test_proposal_tool_succeeds_with_the_propose_scope() -> None:
    dispatcher = _dispatcher()
    caller_scope = _caller(frozenset({"ads:read", "ads:propose"}))

    result = await dispatcher.dispatch(_PROPOSAL_TOOL, {}, caller_scope=caller_scope)

    assert result == {"result": {"proposed": True}}


async def test_proposal_tool_succeeds_when_scopes_are_unrestricted() -> None:
    """`granted_scopes=None` (bearer estatico, C-53) es "sin restriccion",
    no "sin ningun alcance"."""
    dispatcher = _dispatcher()
    caller_scope = _caller(None)

    result = await dispatcher.dispatch(_PROPOSAL_TOOL, {}, caller_scope=caller_scope)

    assert result == {"result": {"proposed": True}}


async def test_read_tool_does_not_require_the_propose_scope() -> None:
    dispatcher = _dispatcher()
    caller_scope = _caller(frozenset({"ads:read"}))

    result = await dispatcher.dispatch(_READ_TOOL, {}, caller_scope=caller_scope)

    assert result == {"result": {"read": True}}

"""`ToolDispatcher` (004 tasks-2.md Carril Q, Q1; contracts/mcp.md §6): el
cubo agregado de escrituras se agota aunque cada llamada use un nombre de
herramienta distinto -- el limite de siempre, por `(persona, herramienta)`,
nunca lo veria, porque ninguna herramienta se repite."""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from datetime import UTC, datetime

import pytest

from safent_ads.mcp.application.caller_scope import CallerScope, Permission
from safent_ads.mcp.application.errors import RateLimitedError
from safent_ads.mcp.infrastructure.rate_limiter import InMemoryQuota
from safent_ads.mcp.presentation.args import BusinessId, ToolArgs
from safent_ads.mcp.presentation.dispatcher import ToolDispatcher
from safent_ads.mcp.presentation.registry import ToolClass, ToolDefinition, ToolRegistry
from safent_ads.mcp.testing.fakes import BUSINESS_A
from safent_ads.shared.clock import FixedClock

_NOW = FixedClock(datetime(2026, 9, 14, 12, 0, tzinfo=UTC))
_WRITES_LIMIT = 10


class _ProposeArgs(ToolArgs):
    business_id: BusinessId


def _business_id_of(args: _ProposeArgs) -> str:
    return args.business_id


async def _noop_handler(args: _ProposeArgs, caller_scope: CallerScope) -> dict[str, bool]:
    return {"ok": True}


def _proposal_definition(name: str) -> ToolDefinition[_ProposeArgs]:
    handler: Callable[[_ProposeArgs, CallerScope], Awaitable[object]] = _noop_handler
    return ToolDefinition(
        name=name,
        description="herramienta de prueba",
        args_model=_ProposeArgs,
        tool_class=ToolClass.PROPOSAL,
        handler=handler,
        business_id_of=_business_id_of,
    )


def _caller_scope() -> CallerScope:
    return CallerScope(
        caller_id="person:quota-test",
        allowed_business_ids=frozenset({BUSINESS_A}),
        permission=Permission.PROPOSE,
        person_label="Prueba",
    )


async def test_once_propuestas_distintas_en_un_minuto_agotan_el_cubo_de_escrituras_aunque_ninguna_repita_herramienta() -> (  # noqa: E501
    None
):
    registry = ToolRegistry(
        _proposal_definition(f"propose_test_write_{index}") for index in range(_WRITES_LIMIT + 1)
    )
    dispatcher = ToolDispatcher(registry=registry, quota=InMemoryQuota(clock=_NOW))
    caller_scope = _caller_scope()

    for index in range(_WRITES_LIMIT):
        result = await dispatcher.dispatch(
            f"propose_test_write_{index}",
            {"business_id": BUSINESS_A},
            caller_scope=caller_scope,
        )
        assert result == {"result": {"ok": True}}

    with pytest.raises(RateLimitedError):
        await dispatcher.dispatch(
            f"propose_test_write_{_WRITES_LIMIT}",
            {"business_id": BUSINESS_A},
            caller_scope=caller_scope,
        )

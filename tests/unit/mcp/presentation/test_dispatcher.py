"""`ToolDispatcher` (T044): lista blanca, cuota, autorizacion por negocio,
traza (contracts/mcp-tools.md reglas 1 y 4)."""

from __future__ import annotations

import pytest

from safent_ads.mcp.application.caller_scope import CallerScope, Permission
from safent_ads.mcp.application.errors import (
    BusinessForbiddenError,
    EntityNotFoundError,
    RateLimitedError,
    ToolNotAllowedError,
    ToolValidationError,
)
from safent_ads.mcp.presentation.dispatcher import ToolDispatcher
from safent_ads.mcp.presentation.registry import ToolRegistry
from safent_ads.mcp.testing.fakes import BUSINESS_A, BUSINESS_B

_UNRESTRICTED = CallerScope(
    caller_id="safent",
    allowed_business_ids=frozenset({BUSINESS_A, BUSINESS_B}),
    permission=Permission.APPROVE,
    person_label="Safent",
)


class _AlwaysAllowQuota:
    async def check_and_consume(self, **_kwargs: str) -> bool:
        return True


class _AlwaysDenyQuota:
    async def check_and_consume(self, **_kwargs: str) -> bool:
        return False


def _dispatcher(registry: ToolRegistry, *, quota=None) -> ToolDispatcher:
    return ToolDispatcher(registry=registry, quota=quota or _AlwaysAllowQuota())


async def test_unregistered_tool_is_rejected(registry: ToolRegistry) -> None:
    dispatcher = _dispatcher(registry)

    with pytest.raises(ToolNotAllowedError):
        await dispatcher.dispatch(
            "approve_proposal", {"business_id": BUSINESS_A}, caller_scope=_UNRESTRICTED
        )


async def test_invalid_arguments_raise_validation_error(registry: ToolRegistry) -> None:
    dispatcher = _dispatcher(registry)

    with pytest.raises(ToolValidationError):
        await dispatcher.dispatch(
            "list_campaigns", {"business_id": "not-a-uuid"}, caller_scope=_UNRESTRICTED
        )


async def test_mcp_requires_scope(registry: ToolRegistry) -> None:
    """contracts/mcp-tools.md regla 4: el dispatcher aplica autorizacion por
    negocio antes del caso de uso. Un `CallerScope` restringido a
    `BUSINESS_B` no puede leer datos de `BUSINESS_A`."""
    dispatcher = _dispatcher(registry)
    restricted_scope = CallerScope(
        caller_id="safent",
        allowed_business_ids=frozenset({BUSINESS_B}),
        permission=Permission.APPROVE,
        person_label="Safent",
    )

    with pytest.raises(BusinessForbiddenError):
        await dispatcher.dispatch(
            "list_campaigns", {"business_id": BUSINESS_A}, caller_scope=restricted_scope
        )

    result = await dispatcher.dispatch(
        "list_campaigns", {"business_id": BUSINESS_B}, caller_scope=restricted_scope
    )
    assert result["result"]["items"]


async def test_cross_business_id_lookup_is_not_found_not_forbidden(
    registry: ToolRegistry,
) -> None:
    """El `business_id` de la llamada es de A, pero `signal_id` pertenece a
    B: el propio adaptador (aqui, el fake) debe negarse sin filtrar que el
    id existe en otro negocio (mismo principio IDOR-safe que REST)."""
    dispatcher = _dispatcher(registry)

    with pytest.raises(EntityNotFoundError):
        await dispatcher.dispatch(
            "get_signal",
            {"business_id": BUSINESS_A, "signal_id": "sig-b-1"},
            caller_scope=_UNRESTRICTED,
        )


async def test_quota_exhausted_raises_rate_limited(registry: ToolRegistry) -> None:
    dispatcher = _dispatcher(registry, quota=_AlwaysDenyQuota())

    with pytest.raises(RateLimitedError):
        await dispatcher.dispatch(
            "list_businesses", {}, caller_scope=_UNRESTRICTED
        )


async def test_list_businesses_has_no_business_authorization_gate(
    registry: ToolRegistry,
) -> None:
    """Unica excepcion legitima (`business_id_of=None`): filtra por
    `CallerScope` dentro del handler, no antes."""
    dispatcher = _dispatcher(registry)
    restricted_scope = CallerScope(
        caller_id="safent",
        allowed_business_ids=frozenset({BUSINESS_A}),
        permission=Permission.APPROVE,
        person_label="Safent",
    )

    result = await dispatcher.dispatch("list_businesses", {}, caller_scope=restricted_scope)

    names = [b["business_id"] for b in result["result"]]
    assert names == [BUSINESS_A]


async def test_successful_dispatch_returns_json_compact_result(registry: ToolRegistry) -> None:
    dispatcher = _dispatcher(registry)

    result = await dispatcher.dispatch(
        "get_kill_switch_status", {"business_id": BUSINESS_A}, caller_scope=_UNRESTRICTED
    )

    assert result == {
        "result": {
            "engaged": False,
            "scope": "global",
            "mode": "ALL",
            "reason": None,
            "since": None,
        }
    }

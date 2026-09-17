"""`ToolDispatcher` audita cada llamada, exito o fallo (004 tasks.md A7,
contracts/mcp.md §4 paso 8): va en el dispatcher para que no haya camino
que lo esquive. Nunca argumentos en claro ni la credencial."""

from __future__ import annotations

import pytest

from safent_ads.mcp.application.caller_scope import CallerScope, Permission
from safent_ads.mcp.application.errors import (
    BusinessForbiddenError,
    RateLimitedError,
    ToolNotAllowedError,
)
from safent_ads.mcp.presentation.dispatcher import _UNRESOLVED_BUSINESS_ID, ToolDispatcher
from safent_ads.mcp.presentation.registry import ToolRegistry
from safent_ads.mcp.testing.fakes import BUSINESS_A, BUSINESS_B


class _AlwaysAllowQuota:
    async def check_and_consume(self, **_kwargs: str) -> bool:
        return True


class _AlwaysDenyQuota:
    async def check_and_consume(self, **_kwargs: str) -> bool:
        return False


class _RecordingAudit:
    def __init__(self) -> None:
        self.calls: list[dict[str, object]] = []

    async def record_tool_call(self, **kwargs: object) -> None:
        self.calls.append(kwargs)


def _scope(business_id: str, *, permission: Permission = Permission.VIEW) -> CallerScope:
    return CallerScope("person:ana", frozenset({business_id}), permission, "Ana")


def _dispatcher(registry: ToolRegistry, audit: _RecordingAudit, *, quota=None) -> ToolDispatcher:
    return ToolDispatcher(registry=registry, quota=quota or _AlwaysAllowQuota(), audit=audit)


async def test_successful_call_is_audited_with_ok_and_no_error_code(registry: ToolRegistry) -> None:
    audit = _RecordingAudit()
    dispatcher = _dispatcher(registry, audit)

    await dispatcher.dispatch(
        "get_kill_switch_status", {"business_id": BUSINESS_A}, caller_scope=_scope(BUSINESS_A)
    )

    assert len(audit.calls) == 1
    call = audit.calls[0]
    assert call["outcome"] == "ok"
    assert call["error_code"] is None
    assert call["caller_id"] == "person:ana"
    assert call["person_label"] == "Ana"
    assert call["business_id"] == BUSINESS_A
    assert call["tool_name"] == "get_kill_switch_status"
    assert call["permission"] == "view"
    assert isinstance(call["args_digest"], str) and len(call["args_digest"]) == 64
    assert isinstance(call["duration_ms"], int)


async def test_denied_by_permission_is_audited(registry: ToolRegistry) -> None:
    audit = _RecordingAudit()
    dispatcher = _dispatcher(registry, audit)

    with pytest.raises(ToolNotAllowedError):
        await dispatcher.dispatch(
            "approve_proposal", {"business_id": BUSINESS_A}, caller_scope=_scope(BUSINESS_A)
        )

    assert len(audit.calls) == 1
    assert audit.calls[0]["outcome"] == "denied"
    assert audit.calls[0]["error_code"] == "TOOL_NOT_ALLOWED"


async def test_denied_by_business_is_audited_under_the_callers_own_business(
    registry: ToolRegistry,
) -> None:
    audit = _RecordingAudit()
    dispatcher = _dispatcher(registry, audit)
    scope = _scope(BUSINESS_A)

    with pytest.raises(BusinessForbiddenError):
        await dispatcher.dispatch(
            "list_campaigns", {"business_id": BUSINESS_B}, caller_scope=scope
        )

    assert len(audit.calls) == 1
    assert audit.calls[0]["outcome"] == "denied"
    assert audit.calls[0]["error_code"] == "BUSINESS_FORBIDDEN"
    # El negocio auditado es el del ALCANCE del llamador, no el pedido.
    assert audit.calls[0]["business_id"] == BUSINESS_A


async def test_rate_limited_is_audited(registry: ToolRegistry) -> None:
    audit = _RecordingAudit()
    dispatcher = _dispatcher(registry, audit, quota=_AlwaysDenyQuota())

    with pytest.raises(RateLimitedError):
        await dispatcher.dispatch(
            "list_businesses", {}, caller_scope=_scope(BUSINESS_A)
        )

    assert len(audit.calls) == 1
    assert audit.calls[0]["outcome"] == "rate_limited"
    assert audit.calls[0]["error_code"] == "RATE_LIMITED"


async def test_four_distinct_outcomes_leave_four_rows(registry: ToolRegistry) -> None:
    audit = _RecordingAudit()
    scope = _scope(BUSINESS_A)

    ok_dispatcher = _dispatcher(registry, audit)
    await ok_dispatcher.dispatch(
        "get_kill_switch_status", {"business_id": BUSINESS_A}, caller_scope=scope
    )
    with pytest.raises(ToolNotAllowedError):
        await ok_dispatcher.dispatch(
            "approve_proposal", {"business_id": BUSINESS_A}, caller_scope=scope
        )
    with pytest.raises(BusinessForbiddenError):
        await ok_dispatcher.dispatch(
            "list_campaigns", {"business_id": BUSINESS_B}, caller_scope=scope
        )
    denied_dispatcher = _dispatcher(registry, audit, quota=_AlwaysDenyQuota())
    with pytest.raises(RateLimitedError):
        await denied_dispatcher.dispatch("list_businesses", {}, caller_scope=scope)

    assert len(audit.calls) == 4
    outcomes = {call["outcome"] for call in audit.calls}
    assert outcomes == {"ok", "denied", "rate_limited"}
    assert all("token" not in str(call).lower() for call in audit.calls)
    assert all("authorization" not in str(call).lower() for call in audit.calls)


async def test_empty_scope_still_leaves_a_row_with_the_unresolved_marker(
    registry: ToolRegistry,
) -> None:
    """B-4: un alcance sin negocio ya no omite la fila -- deja constancia
    con `_UNRESOLVED_BUSINESS_ID` en vez de desaparecer del `decision_log`."""
    audit = _RecordingAudit()
    dispatcher = _dispatcher(registry, audit)
    scope = CallerScope("person:ghost", frozenset(), Permission.VIEW, "Nadie")

    with pytest.raises(ToolNotAllowedError):
        await dispatcher.dispatch(
            "approve_proposal", {"business_id": BUSINESS_A}, caller_scope=scope
        )

    assert len(audit.calls) == 1
    assert audit.calls[0]["business_id"] == _UNRESOLVED_BUSINESS_ID


async def test_un_alcance_de_varios_negocios_tambien_deja_fila_con_el_business_id_del_argumento(
    registry: ToolRegistry,
) -> None:
    """B-4: `ADS_SINGLE_OWNER_MODE` con mas de un negocio en el alcance ya
    no omite la fila -- `_authorize` ya sabe el `business_id` del argumento
    de la llamada, y ese es el que se audita, no el alcance completo."""
    audit = _RecordingAudit()
    dispatcher = _dispatcher(registry, audit)
    multi_business_scope = CallerScope(
        "person:owner", frozenset({BUSINESS_A, BUSINESS_B}), Permission.VIEW, "Duena"
    )

    await dispatcher.dispatch(
        "get_kill_switch_status", {"business_id": BUSINESS_A}, caller_scope=multi_business_scope
    )

    assert len(audit.calls) == 1
    assert audit.calls[0]["outcome"] == "ok"
    assert audit.calls[0]["business_id"] == BUSINESS_A


async def test_without_an_audit_port_dispatch_still_works(registry: ToolRegistry) -> None:
    dispatcher = ToolDispatcher(registry=registry, quota=_AlwaysAllowQuota())

    result = await dispatcher.dispatch(
        "get_kill_switch_status", {"business_id": BUSINESS_A}, caller_scope=_scope(BUSINESS_A)
    )

    assert result["result"]["engaged"] is False

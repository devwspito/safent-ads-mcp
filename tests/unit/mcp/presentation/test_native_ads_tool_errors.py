from unittest.mock import AsyncMock

import pytest

from safent_ads.accounts.application.errors import BrokerRequestDeniedError
from safent_ads.accounts.infrastructure.errors import BrokerConnectionError
from safent_ads.mcp.application.caller_scope import CallerScope, Permission
from safent_ads.mcp.application.errors import (
    BrokerUnavailableError,
    BusinessForbiddenError,
    NativeAdsUnavailableError,
    PlatformAppNotConfiguredError,
)
from safent_ads.mcp.presentation.dispatcher import ToolDispatcher
from safent_ads.mcp.presentation.native_ads_tools import (
    NativeAdsToolServices,
    build_native_ads_tool_definitions,
)
from safent_ads.mcp.presentation.registry import ToolRegistry

BUSINESS = "00000000-0000-0000-0000-000000000001"

# code -> (tipo esperado, si el `code` original debe seguir viajando)
_EXPECTED_BY_CODE = {
    "NATIVE_MCP_UNAVAILABLE": NativeAdsUnavailableError,
    # Incidente de produccion (companion 0.2.21): `PLATFORM_APP_NOT_CONFIGURED`
    # escapaba crudo como `BrokerRequestDeniedError` -- el SDK MCP lo
    # convertia en el `UnexpectedToolError` opaco.
    "PLATFORM_APP_NOT_CONFIGURED": PlatformAppNotConfiguredError,
    # Sin traduccion conocida: se relanza tal cual -- `mount.py` lo
    # convierte en `TOOL_FAILED`, nunca este modulo por adivinanza.
    "UNKNOWN_BROKER_ERROR": BrokerRequestDeniedError,
}


@pytest.mark.parametrize("tool", ["get_native_ads_tools", "get_native_ads_report"])
@pytest.mark.parametrize("code", list(_EXPECTED_BY_CODE))
async def test_unavailable_is_actionable_without_exposing_provider_detail(tool, code):
    port = AsyncMock()
    error = BrokerRequestDeniedError(code, "secret-in-provider-error")
    port.list_native_tools.side_effect = error
    port.read_native_tool.side_effect = error
    definition = next(
        d for d in build_native_ads_tool_definitions(NativeAdsToolServices(port))
        if d.name == tool
    )
    raw = {"business_id": BUSINESS, "account_ref": "owned-account"}
    if tool == "get_native_ads_report":
        raw["tool"] = "get_insights"
    args = definition.args_model.model_validate(raw)
    expected = _EXPECTED_BY_CODE[code]
    with pytest.raises(expected) as result:
        await definition.handler(
            args, CallerScope("owner", frozenset({BUSINESS}), Permission.VIEW, "Owner")
        )
    if code == "UNKNOWN_BROKER_ERROR":
        assert result.value is error
    else:
        assert result.value.code == code
        assert "secret-in-provider-error" not in str(result.value)
    if code == "NATIVE_MCP_UNAVAILABLE":
        assert "Composio" in str(result.value)
        assert "list_platform_accounts" in str(result.value)


@pytest.mark.parametrize("tool", ["get_native_ads_tools", "get_native_ads_report"])
async def test_a_transport_failure_becomes_broker_unavailable(tool):
    port = AsyncMock()
    error = BrokerConnectionError("no se pudo conectar al socket")
    port.list_native_tools.side_effect = error
    port.read_native_tool.side_effect = error
    definition = next(
        d for d in build_native_ads_tool_definitions(NativeAdsToolServices(port))
        if d.name == tool
    )
    raw = {"business_id": BUSINESS, "account_ref": "owned-account"}
    if tool == "get_native_ads_report":
        raw["tool"] = "get_insights"
    args = definition.args_model.model_validate(raw)

    with pytest.raises(BrokerUnavailableError) as result:
        await definition.handler(
            args, CallerScope("owner", frozenset({BUSINESS}), Permission.VIEW, "Owner")
        )

    assert result.value.code == "BROKER_UNAVAILABLE"
    assert "socket" not in str(result.value)


async def test_account_scope_still_checked_before_native_connector():
    port = AsyncMock()
    quota = AsyncMock()
    quota.check_and_consume.return_value = True
    dispatcher = ToolDispatcher(
        registry=ToolRegistry(build_native_ads_tool_definitions(NativeAdsToolServices(port))),
        quota=quota,
    )
    with pytest.raises(BusinessForbiddenError):
        await dispatcher.dispatch(
            "get_native_ads_tools", {"business_id": BUSINESS, "account_ref": "owned-account"},
            caller_scope=CallerScope("other", frozenset(), Permission.VIEW, "Otro"),
        )
    port.list_native_tools.assert_not_called()

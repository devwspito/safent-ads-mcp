"""`connect_platform_account` accepted no way to pick which Google Ads
customer to bind: `ManagedOAuthConnectService.begin` (broker/application/
managed_oauth_connect.py) already required `google_customer_id` for Google
and raises `GoogleAccountSelectionRequiredError` without it, and the REST
payload (`accounts/presentation/payloads.py::BeginConnectRequest`) already
carried the field -- only `ConnectPlatformAccountArgs` (MCP) was missing
it. Mocks only the `BeginOAuthConnect` use case (the same one the REST
route calls), same seam-only-mocking convention as
`test_journey_accounts.py`."""

from __future__ import annotations

import uuid
from datetime import UTC, datetime
from unittest.mock import AsyncMock, patch

import pytest

from safent_ads.accounts.application.begin_oauth_connect import BeginOAuthConnectResult
from safent_ads.mcp.application.caller_scope import CallerScope, Permission
from safent_ads.mcp.application.dto import PlatformCode
from safent_ads.mcp.application.errors import ToolValidationError
from safent_ads.mcp.presentation.args import ConnectPlatformAccountArgs
from safent_ads.mcp.presentation.connection_tools import (
    ConnectionToolServices,
    build_connection_tool_definitions,
)

_BUSINESS = "11111111-1111-1111-1111-111111111111"
_PERSON = uuid.uuid4()
_RESULT = BeginOAuthConnectResult(
    session_id=uuid.uuid4(),
    authorize_url="https://accounts.google.com/o/oauth2/auth?state=x",
    expires_at=datetime(2026, 9, 16, tzinfo=UTC),
)


class _FakeSession:
    async def commit(self) -> None: ...


class _FakeSessionContext:
    async def __aenter__(self) -> _FakeSession:
        return _FakeSession()

    async def __aexit__(self, *exc_info: object) -> bool:
        return False


def _services() -> ConnectionToolServices:
    return ConnectionToolServices(
        session_factory=_FakeSessionContext,
        oauth_broker=AsyncMock(),
        id_generator=AsyncMock(),
        public_base_url="https://ads.example.com",
    )


def _connect_handler():
    definitions = build_connection_tool_definitions(_services())
    return next(d for d in definitions if d.name == "connect_platform_account").handler


def _scope() -> CallerScope:
    return CallerScope(f"person:{_PERSON}", frozenset({_BUSINESS}), Permission.APPROVE, "x")


async def test_google_customer_id_reaches_the_begin_oauth_connect_use_case() -> None:
    args = ConnectPlatformAccountArgs(
        business_id=_BUSINESS, platform=PlatformCode.GOOGLE, google_customer_id="1000000001"
    )
    with patch("safent_ads.mcp.presentation.connection_tools.BeginOAuthConnect") as use_case_cls:
        use_case_cls.return_value.execute = AsyncMock(return_value=_RESULT)
        result = await _connect_handler()(args, _scope())

    kwargs = use_case_cls.return_value.execute.await_args.kwargs
    assert kwargs["google_customer_id"] == "1000000001"
    assert result["authorize_url"] == _RESULT.authorize_url


async def test_without_google_customer_id_the_use_case_still_receives_none() -> None:
    args = ConnectPlatformAccountArgs(business_id=_BUSINESS, platform=PlatformCode.GOOGLE)
    with patch("safent_ads.mcp.presentation.connection_tools.BeginOAuthConnect") as use_case_cls:
        use_case_cls.return_value.execute = AsyncMock(return_value=_RESULT)
        await _connect_handler()(args, _scope())

    kwargs = use_case_cls.return_value.execute.await_args.kwargs
    assert kwargs["google_customer_id"] is None


@pytest.mark.parametrize("value", ["123", "abc", "12345678901", "167-779-132"])
def test_malformed_google_customer_id_is_rejected_at_the_args_boundary(value: str) -> None:
    with pytest.raises(ValueError, match="google_customer_id_invalid"):
        ConnectPlatformAccountArgs(
            business_id=_BUSINESS, platform=PlatformCode.GOOGLE, google_customer_id=value
        )


async def test_the_use_case_rejects_a_meta_customer_id_selection() -> None:
    """`normalize_google_customer_id` inside `BeginOAuthConnect.execute` is the
    authoritative, provider-aware check (the args-level validator only checks
    shape, same split as `BeginConnectRequest`/`connections_router.start`)."""
    args = ConnectPlatformAccountArgs(
        business_id=_BUSINESS, platform=PlatformCode.META, google_customer_id="1000000001"
    )
    with pytest.raises(ToolValidationError, match="número de cuenta de Google Ads"):
        await _connect_handler()(args, _scope())

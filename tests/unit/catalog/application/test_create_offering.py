from dataclasses import replace
from unittest.mock import AsyncMock

import pytest
from pydantic import ValidationError

from safent_ads.catalog.application.create_offering import CreateOffering
from safent_ads.catalog.domain.offering import OfferingDetails
from safent_ads.catalog.presentation.offering_input import CreateOfferingBody
from safent_ads.mcp.application.caller_scope import CallerScope, Permission
from safent_ads.mcp.application.errors import BusinessForbiddenError, RateLimitedError
from safent_ads.mcp.domain.errors import ForbiddenToolNameError
from safent_ads.mcp.presentation.dispatcher import ToolDispatcher
from safent_ads.mcp.presentation.offering_tools import CreateOfferingArgs, build_offering_tool
from safent_ads.mcp.presentation.registry import ToolClass, ToolRegistry

_BUSINESS = "11111111-1111-1111-1111-111111111111"
_BASE = {"code": "acme-test", "title": "Oferta explícita"}


@pytest.mark.parametrize(
    "changes",
    [
        {"code": ""},
        {"code": "a" * 65},
        {"code": "../evil"},
        {"title": " "},
        {"title": "a" * 201},
        {"title": "line\nnew"},
        {"price_amount": "1"},
        {"price_currency": "EUR"},
        {"price_amount": "-1", "price_currency": "EUR"},
        {"price_amount": "1.234", "price_currency": "EUR"},
        {"price_amount": "NaN", "price_currency": "EUR"},
        {"price_amount": "1e2", "price_currency": "EUR"},
        {"price_amount": 1.5, "price_currency": "EUR"},
        {"price_amount": True, "price_currency": "EUR"},
        {"price_amount": "10000000000", "price_currency": "EUR"},
        {"price_amount": "1", "price_currency": "eur"},
        {"unknown": "x"},
    ],
)
def test_http_and_mcp_share_strict_invalid_input_rejection(changes):
    with pytest.raises(ValidationError):
        CreateOfferingBody.model_validate(_BASE | changes)
    with pytest.raises(ValidationError):
        CreateOfferingArgs.model_validate(_BASE | changes | {"business_id": _BUSINESS})


@pytest.mark.parametrize("amount,currency", [(None, None), ("0", "EUR"), ("19.95", "USD")])
def test_no_inferred_price_or_currency(amount, currency):
    details = OfferingDetails(**_BASE, price_amount=amount, price_currency=currency)
    assert details.price_amount == amount and details.price_currency == currency


@pytest.mark.parametrize(
    "name,tool_class",
    [
        ("create_campaign", ToolClass.CATALOG_WRITE),
        ("create_offering_extra", ToolClass.CATALOG_WRITE),
        ("approve_proposal", ToolClass.CATALOG_WRITE),
        ("create_offering", ToolClass.READ),
        ("create_offering", ToolClass.PROPOSAL),
    ],
)
def test_catalog_exception_never_enables_other_create_or_read_labeled_writes(name, tool_class):
    definition = build_offering_tool(AsyncMock())
    with pytest.raises(ForbiddenToolNameError):
        ToolRegistry([replace(definition, name=name, tool_class=tool_class)])


@pytest.mark.parametrize(
    "allowed,quota_ok,error",
    [
        (False, True, BusinessForbiddenError),
        (True, False, RateLimitedError),
    ],
)
async def test_mcp_scope_and_quota_block_before_repository(allowed, quota_ok, error):
    repository = AsyncMock()
    quota = AsyncMock()
    quota.check_and_consume.return_value = quota_ok
    dispatcher = ToolDispatcher(
        registry=ToolRegistry([build_offering_tool(CreateOffering(repository))]),
        quota=quota,
    )
    caller = CallerScope(
        "test-agent",
        frozenset({_BUSINESS}) if allowed else frozenset(),
        Permission.PROPOSE,
        "Agente de prueba",
    )
    with pytest.raises(error):
        await dispatcher.dispatch(
            "create_offering", _BASE | {"business_id": _BUSINESS}, caller_scope=caller
        )
    repository.create_or_get.assert_not_called()

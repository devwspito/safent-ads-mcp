"""`search_terms_tools.py`: registro en el `ToolRegistry` real, nombres
verbo-primero validos, y que `build_default_registry` solo las anade
cuando se le pasan `search_terms_services` (retrocompatible con el resto
de tests que construyen el registro sin ellos, mismo patron que
`test_experiment_tools.py`)."""

from __future__ import annotations

from datetime import UTC, date, datetime

import pytest

from safent_ads.mcp.application.caller_scope import CallerScope, Permission
from safent_ads.mcp.application.dto import Window, WindowPreset
from safent_ads.mcp.application.search_terms_ports import BudgetEnvelope, SearchTermsResult
from safent_ads.mcp.presentation.catalog import build_default_registry
from safent_ads.mcp.presentation.read_model_ports import ReadModelPorts
from safent_ads.mcp.presentation.registry import ToolClass
from safent_ads.mcp.presentation.search_terms_tools import (
    GetBudgetEnvelopeArgs,
    ListSearchTermsArgs,
    SearchTermsToolServices,
    build_search_terms_tool_definitions,
)
from safent_ads.shared.clock import FixedClock

_EXPECTED_NAMES = frozenset({"list_search_terms", "get_budget_envelope"})
_NOW = datetime(2026, 9, 9, tzinfo=UTC)
_BUSINESS_ID = "9d9b8b1a-6b8e-4f0a-9d1e-8f2c6b7a5e10"
_ACCOUNT_REF = "google:1234567890"


class _FakeSearchTermReadPort:
    def __init__(self) -> None:
        self.calls: list[tuple[str, str, Window]] = []

    async def list_search_terms(
        self, business_id: str, account_ref: str, *, window: Window
    ) -> SearchTermsResult:
        self.calls.append((business_id, account_ref, window))
        return SearchTermsResult(account_ref=account_ref, is_supported=True, reason=None, terms=[])


class _FakeBudgetEnvelopeReadPort:
    async def get_budget_envelope(self, business_id: str) -> BudgetEnvelope:
        return BudgetEnvelope(
            business_id=business_id,
            monthly_cap_minor=None,
            spent_month_to_date_minor=None,
            headroom_minor=None,
            projected_month_end_minor=None,
            currency=None,
            as_of=date(2026, 9, 9),
            reason="no_platform_account",
        )


def _services() -> SearchTermsToolServices:
    return SearchTermsToolServices(
        search_terms=_FakeSearchTermReadPort(), budget_envelope=_FakeBudgetEnvelopeReadPort()
    )


def test_build_search_terms_tool_definitions_registers_the_two_verbs() -> None:
    definitions = build_search_terms_tool_definitions(_services())

    names = {d.name for d in definitions}
    assert names == _EXPECTED_NAMES


def test_both_tools_are_read_class() -> None:
    definitions = build_search_terms_tool_definitions(_services())

    for definition in definitions:
        assert definition.tool_class is ToolClass.READ


def test_default_registry_omits_search_terms_tools_without_services(
    read_model_ports: ReadModelPorts,
) -> None:
    registry = build_default_registry(read_model_ports, FixedClock(_NOW))

    for name in _EXPECTED_NAMES:
        assert registry.get(name) is None


def test_default_registry_includes_search_terms_tools_when_wired(
    read_model_ports: ReadModelPorts,
) -> None:
    registry = build_default_registry(
        read_model_ports, FixedClock(_NOW), search_terms_services=_services()
    )

    for name in _EXPECTED_NAMES:
        assert registry.get(name) is not None


async def test_list_search_terms_handler_builds_a_preset_only_window() -> None:
    port = _FakeSearchTermReadPort()
    services = SearchTermsToolServices(
        search_terms=port, budget_envelope=_FakeBudgetEnvelopeReadPort()
    )
    definitions = {d.name: d for d in build_search_terms_tool_definitions(services)}
    args = ListSearchTermsArgs(
        business_id=_BUSINESS_ID, account_ref=_ACCOUNT_REF, window=WindowPreset.SEVEN_DAYS
    )
    caller_scope = CallerScope(
        caller_id="test",
        allowed_business_ids=frozenset({_BUSINESS_ID}),
        permission=Permission.VIEW,
        person_label="Agente de prueba",
    )

    result = await definitions["list_search_terms"].handler(args, caller_scope)

    assert result.account_ref == _ACCOUNT_REF
    assert port.calls == [
        (
            _BUSINESS_ID,
            _ACCOUNT_REF,
            Window(preset=WindowPreset.SEVEN_DAYS, lag_days=0, date_from=None, date_to=None),
        )
    ]


_OUT_OF_RANGE_WINDOWS = [WindowPreset.TODAY, WindowPreset.THREE_DAYS, WindowPreset.MONTH_TO_DATE]
_IN_RANGE_WINDOWS = [WindowPreset.SEVEN_DAYS, WindowPreset.FOURTEEN_DAYS, WindowPreset.THIRTY_DAYS]


@pytest.mark.parametrize("window", _OUT_OF_RANGE_WINDOWS)
def test_list_search_terms_args_rejects_windows_outside_7_14_30_days(window: WindowPreset) -> None:
    with pytest.raises(ValueError, match="7D, 14D o 30D"):
        ListSearchTermsArgs(business_id=_BUSINESS_ID, account_ref=_ACCOUNT_REF, window=window)


@pytest.mark.parametrize("window", _IN_RANGE_WINDOWS)
def test_list_search_terms_args_accepts_7_14_30_day_windows(window: WindowPreset) -> None:
    args = ListSearchTermsArgs(business_id=_BUSINESS_ID, account_ref=_ACCOUNT_REF, window=window)

    assert args.window is window


async def test_get_budget_envelope_handler_delegates_to_the_port() -> None:
    definitions = {d.name: d for d in build_search_terms_tool_definitions(_services())}
    args = GetBudgetEnvelopeArgs(business_id=_BUSINESS_ID)
    caller_scope = CallerScope(
        caller_id="test",
        allowed_business_ids=frozenset({_BUSINESS_ID}),
        permission=Permission.VIEW,
        person_label="Agente de prueba",
    )

    envelope = await definitions["get_budget_envelope"].handler(args, caller_scope)

    assert envelope.business_id == _BUSINESS_ID
    assert envelope.reason == "no_platform_account"


@pytest.mark.parametrize("name", sorted(_EXPECTED_NAMES))
def test_tool_names_pass_the_registry_naming_guard(name: str) -> None:
    definitions = build_search_terms_tool_definitions(_services())
    assert any(d.name == name for d in definitions)

"""`company_tools.py` (R2/R8): registro en el `ToolRegistry` real, nombres
verbo-primero validos, y que los handlers delegan en el puerto sin logica
de negocio propia (regla de presentacion: validar -> llamar -> mapear)."""

from __future__ import annotations

from datetime import date

import pytest

from safent_ads.mcp.application.caller_scope import CallerScope, Permission
from safent_ads.mcp.application.company_read_ports import (
    ChannelRevenue,
    CrmSummary,
    CrmWindowPreset,
    TopAdsLevel,
    TopAdsMetric,
    TopAdsWindowPreset,
    TopPerformingAdsResult,
)
from safent_ads.mcp.presentation.company_tools import (
    CompanyToolServices,
    GetCrmSummaryArgs,
    ListTopPerformingAdsArgs,
    build_company_tool_definitions,
)
from safent_ads.mcp.presentation.registry import ToolClass, ToolRegistry

_EXPECTED_NAMES = frozenset({"get_crm_summary", "list_top_performing_ads"})
_BUSINESS_ID = "9d9b8b1a-6b8e-4f0a-9d1e-8f2c6b7a5e10"


def _caller_scope() -> CallerScope:
    return CallerScope(
        caller_id="test",
        allowed_business_ids=frozenset({_BUSINESS_ID}),
        permission=Permission.VIEW,
        person_label="Agente de prueba",
    )


def _summary() -> CrmSummary:
    return CrmSummary(
        business_id=_BUSINESS_ID,
        window_preset=CrmWindowPreset.THIRTY_DAYS,
        window_start=date(2026, 8, 16),
        window_end=date(2026, 9, 14),
        currency="EUR",
        new_customers=None,
        new_customers_note="cubo_con_menos_de_5_clientes",
        returning_customers=None,
        returning_customers_note="cubo_con_menos_de_5_clientes",
        total_revenue_minor=100_00,
        average_order_value_minor=50_00,
        lifetime_value_estimate_minor=200_00,
        revenue_note=None,
        revenue_by_channel=(ChannelRevenue(channel="meta", revenue_minor=100_00, note=None),),
    )


class _FakeCrmSummaryReadPort:
    def __init__(self) -> None:
        self.calls: list[tuple[str, CrmWindowPreset]] = []

    async def get_crm_summary(self, business_id: str, *, window: CrmWindowPreset) -> CrmSummary:
        self.calls.append((business_id, window))
        return _summary()


class _FakeTopPerformingAdsReadPort:
    def __init__(self) -> None:
        self.calls: list[tuple[str, TopAdsWindowPreset, TopAdsLevel, TopAdsMetric, int]] = []

    async def list_top_performing_ads(
        self,
        business_id: str,
        *,
        window: TopAdsWindowPreset,
        level: TopAdsLevel,
        metric: TopAdsMetric,
        limit: int,
    ) -> TopPerformingAdsResult:
        self.calls.append((business_id, window, level, metric, limit))
        return TopPerformingAdsResult(
            business_id=business_id, window_preset=window, level=level, metric=metric, ads=()
        )


def _services() -> CompanyToolServices:
    return CompanyToolServices(
        crm_summary=_FakeCrmSummaryReadPort(), top_performing_ads=_FakeTopPerformingAdsReadPort()
    )


def test_build_company_tool_definitions_registers_the_two_verbs() -> None:
    definitions = build_company_tool_definitions(_services())

    assert {d.name for d in definitions} == _EXPECTED_NAMES


def test_both_tools_are_read_class() -> None:
    definitions = build_company_tool_definitions(_services())

    for definition in definitions:
        assert definition.tool_class is ToolClass.READ


def test_definitions_pass_the_registry_naming_guard() -> None:
    ToolRegistry(build_company_tool_definitions(_services()))


async def test_get_crm_summary_handler_delegates_to_the_port() -> None:
    port = _FakeCrmSummaryReadPort()
    services = CompanyToolServices(
        crm_summary=port, top_performing_ads=_FakeTopPerformingAdsReadPort()
    )
    definitions = {d.name: d for d in build_company_tool_definitions(services)}
    args = GetCrmSummaryArgs(business_id=_BUSINESS_ID, window=CrmWindowPreset.SEVEN_DAYS)

    result = await definitions["get_crm_summary"].handler(args, _caller_scope())

    assert result.new_customers is None
    assert result.new_customers_note == "cubo_con_menos_de_5_clientes"
    assert port.calls == [(_BUSINESS_ID, CrmWindowPreset.SEVEN_DAYS)]


async def test_get_crm_summary_defaults_to_30_days() -> None:
    args = GetCrmSummaryArgs(business_id=_BUSINESS_ID)

    assert args.window is CrmWindowPreset.THIRTY_DAYS


async def test_list_top_performing_ads_handler_delegates_to_the_port() -> None:
    port = _FakeTopPerformingAdsReadPort()
    services = CompanyToolServices(crm_summary=_FakeCrmSummaryReadPort(), top_performing_ads=port)
    definitions = {d.name: d for d in build_company_tool_definitions(services)}
    args = ListTopPerformingAdsArgs(
        business_id=_BUSINESS_ID,
        window=TopAdsWindowPreset.SIXTY_DAYS,
        level=TopAdsLevel.AD_SET,
        metric=TopAdsMetric.ROAS,
        limit=5,
    )

    result = await definitions["list_top_performing_ads"].handler(args, _caller_scope())

    assert result.level is TopAdsLevel.AD_SET
    assert port.calls == [
        (_BUSINESS_ID, TopAdsWindowPreset.SIXTY_DAYS, TopAdsLevel.AD_SET, TopAdsMetric.ROAS, 5)
    ]


def test_list_top_performing_ads_limit_is_capped_at_25() -> None:
    with pytest.raises(ValueError, match="limit"):
        ListTopPerformingAdsArgs(business_id=_BUSINESS_ID, limit=26)


def test_list_top_performing_ads_defaults() -> None:
    args = ListTopPerformingAdsArgs(business_id=_BUSINESS_ID)

    assert args.window is TopAdsWindowPreset.THIRTY_DAYS
    assert args.level is TopAdsLevel.CAMPAIGN
    assert args.metric is TopAdsMetric.CONVERSIONS
    assert args.limit == 10

"""`economics_tools.py` (B-1, checklists/final-review.md): la capa de
adaptacion que traduce el `ToolArgs` propio de esta lane al `args_model`
original de `economics.presentation.mcp_tools` antes de llamar a
`spec.handler` -- `tests/unit/economics/presentation/test_mcp_tools.py` ya
cubre la logica de negocio de los 5 `ToolSpec`; este modulo solo prueba que
la traduccion y el registro funcionan, y que `build_default_registry` los
omite/incluye segun se le pase `economics_service` (mismo patron que
`test_experiment_tools.py`)."""

from __future__ import annotations

from datetime import UTC, datetime

from safent_ads.economics.application.query_service import EconomicsQueryService
from safent_ads.economics.testing.in_memory_repositories import (
    InMemoryLagCurveRepository,
    InMemoryPlatformDivergenceRepository,
    InMemoryUnitEconomicsProfileRepository,
)
from safent_ads.mcp.application.dto import PlatformCode
from safent_ads.mcp.presentation.catalog import build_default_registry
from safent_ads.mcp.presentation.economics_tools import (
    GetAttributionLagCurveArgs,
    GetCohortProjectionArgs,
    GetPlatformDivergenceArgs,
    GetTargetCpaArgs,
    GetUnitEconomicsArgs,
    build_economics_tool_definitions,
)
from safent_ads.mcp.presentation.read_model_ports import ReadModelPorts
from safent_ads.mcp.presentation.registry import ToolClass
from safent_ads.mcp.testing.fakes import BUSINESS_A
from safent_ads.shared.clock import FixedClock

_NOW = datetime(2026, 9, 10, tzinfo=UTC)
_PRODUCT_ID = "33333333-3333-3333-3333-333333333333"
_PLATFORM_ACCOUNT_ID = "44444444-4444-4444-4444-444444444444"
_EXPECTED_NAMES = frozenset(
    {
        "get_unit_economics",
        "get_target_cpa",
        "get_attribution_lag_curve",
        "get_cohort_projection",
        "get_platform_divergence",
    }
)


def _service() -> EconomicsQueryService:
    return EconomicsQueryService(
        profiles=InMemoryUnitEconomicsProfileRepository(),
        lag_curves=InMemoryLagCurveRepository(),
        divergences=InMemoryPlatformDivergenceRepository(),
        clock=FixedClock(_NOW),
    )


def test_build_economics_tool_definitions_registers_the_five_reads() -> None:
    definitions = build_economics_tool_definitions(_service())

    names = {d.name for d in definitions}
    assert names == _EXPECTED_NAMES
    assert all(d.tool_class is ToolClass.READ for d in definitions)
    assert all(d.business_id_of is not None for d in definitions)


async def test_get_unit_economics_translates_args_and_delegates_to_the_spec() -> None:
    definitions = {d.name: d for d in build_economics_tool_definitions(_service())}
    args = GetUnitEconomicsArgs(business_id=BUSINESS_A, product_id=_PRODUCT_ID)

    result = await definitions["get_unit_economics"].handler(args, None)

    # Sin perfil sembrado: capacidad reportada, nunca fingida (mismo
    # comportamiento que `economics/presentation/mcp_tools.py`, ya probado).
    assert result == {
        "error": {
            "code": "ENTITY_NOT_FOUND",
            "message": "no hay perfil de economia unitaria para este producto",
        }
    }


async def test_get_target_cpa_translates_args_and_delegates_to_the_spec() -> None:
    definitions = {d.name: d for d in build_economics_tool_definitions(_service())}
    args = GetTargetCpaArgs(business_id=BUSINESS_A, product_id=_PRODUCT_ID)

    result = await definitions["get_target_cpa"].handler(args, None)

    assert result["error"]["code"] == "ENTITY_NOT_FOUND"


async def test_get_attribution_lag_curve_translates_platform_enum_to_str() -> None:
    definitions = {d.name: d for d in build_economics_tool_definitions(_service())}
    args = GetAttributionLagCurveArgs(
        business_id=BUSINESS_A, product_id=_PRODUCT_ID, platform=PlatformCode.GOOGLE
    )

    result = await definitions["get_attribution_lag_curve"].handler(args, None)

    assert result["error"]["code"] == "ENTITY_NOT_FOUND"


async def test_get_cohort_projection_translates_args_and_delegates_to_the_spec() -> None:
    definitions = {d.name: d for d in build_economics_tool_definitions(_service())}
    args = GetCohortProjectionArgs(
        business_id=BUSINESS_A,
        product_id=_PRODUCT_ID,
        platform=PlatformCode.META,
        observed=10,
        age_days=7,
    )

    result = await definitions["get_cohort_projection"].handler(args, None)

    assert result["error"]["code"] == "ENTITY_NOT_FOUND"


async def test_get_platform_divergence_translates_args_and_delegates_to_the_spec() -> None:
    definitions = {d.name: d for d in build_economics_tool_definitions(_service())}
    args = GetPlatformDivergenceArgs(
        business_id=BUSINESS_A, platform_account_id=_PLATFORM_ACCOUNT_ID
    )

    result = await definitions["get_platform_divergence"].handler(args, None)

    assert result["error"]["code"] == "ENTITY_NOT_FOUND"


def test_default_registry_omits_economics_tools_without_service(
    read_model_ports: ReadModelPorts,
) -> None:
    registry = build_default_registry(read_model_ports, FixedClock(_NOW))

    for name in _EXPECTED_NAMES:
        assert registry.get(name) is None


def test_default_registry_includes_economics_tools_when_wired(
    read_model_ports: ReadModelPorts,
) -> None:
    registry = build_default_registry(
        read_model_ports, FixedClock(_NOW), economics_service=_service()
    )

    for name in _EXPECTED_NAMES:
        assert registry.get(name) is not None

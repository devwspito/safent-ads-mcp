"""`optimization_tools.py` (B-1, checklists/final-review.md): la capa de
adaptacion que traduce el `ToolArgs` propio de esta lane al `args_model`
original de `optimization.presentation.mcp_tools` antes de llamar a
`spec.handler`. La logica de negocio de los 4 `ToolSpec` ya la prueba
`tests/unit/optimization/presentation/test_mcp_tools.py`; este modulo solo
prueba que la traduccion, la clase (`READ`/`PROPOSAL`) y el registro
funcionan, y que `build_default_registry` los omite/incluye segun se le
pase `optimization_service` (mismo patron que `test_experiment_tools.py`)."""

from __future__ import annotations

from datetime import UTC, datetime

from safent_ads.mcp.presentation.catalog import build_default_registry
from safent_ads.mcp.presentation.optimization_tools import (
    DiagnoseEntityArgs,
    GetMarginalRoasArgs,
    ProposeReallocationPlanArgs,
    SimulateSpendChangeArgs,
    build_optimization_tool_definitions,
)
from safent_ads.mcp.presentation.read_model_ports import ReadModelPorts
from safent_ads.mcp.presentation.registry import ToolClass
from safent_ads.mcp.testing.fakes import BUSINESS_A
from safent_ads.optimization.application.query_service import OptimizationQueryService
from safent_ads.optimization.testing.in_memory_repositories import (
    InMemoryContributionMarginPort,
    InMemoryDiagnosisMetricsPort,
    InMemoryMarginalEstimateRepository,
    InMemoryReallocationCandidateRepository,
    InMemoryReallocationProposalPort,
    InMemoryResponseCurveRepository,
)
from safent_ads.shared.clock import FixedClock

_NOW = datetime(2026, 9, 10, tzinfo=UTC)
_ENTITY_REF = "google:campaign:1234567890"
_PRODUCT_ID = "33333333-3333-3333-3333-333333333333"
_EXPECTED_NAMES = frozenset(
    {"get_marginal_roas", "diagnose_entity", "simulate_spend_change", "propose_reallocation_plan"}
)


def _service() -> OptimizationQueryService:
    return OptimizationQueryService(
        marginal_estimates=InMemoryMarginalEstimateRepository(),
        diagnosis_metrics=InMemoryDiagnosisMetricsPort(),
        response_curves=InMemoryResponseCurveRepository(),
        contribution_margins=InMemoryContributionMarginPort(),
        reallocation_candidates=InMemoryReallocationCandidateRepository(),
        reallocation_proposals=InMemoryReallocationProposalPort(),
        clock=FixedClock(_NOW),
    )


def test_build_optimization_tool_definitions_registers_the_four_verbs() -> None:
    definitions = build_optimization_tool_definitions(_service())

    names = {d.name for d in definitions}
    assert names == _EXPECTED_NAMES


def test_only_propose_reallocation_plan_is_a_proposal_class_tool() -> None:
    definitions = {d.name: d for d in build_optimization_tool_definitions(_service())}

    assert definitions["propose_reallocation_plan"].tool_class is ToolClass.PROPOSAL
    for name in _EXPECTED_NAMES - {"propose_reallocation_plan"}:
        assert definitions[name].tool_class is ToolClass.READ


async def test_get_marginal_roas_translates_args_and_delegates_to_the_spec() -> None:
    definitions = {d.name: d for d in build_optimization_tool_definitions(_service())}
    args = GetMarginalRoasArgs(business_id=BUSINESS_A, entity_ref=_ENTITY_REF)

    result = await definitions["get_marginal_roas"].handler(args, None)

    assert result["error"]["code"] == "ENTITY_NOT_FOUND"


async def test_diagnose_entity_translates_args_and_delegates_to_the_spec() -> None:
    definitions = {d.name: d for d in build_optimization_tool_definitions(_service())}
    args = DiagnoseEntityArgs(business_id=BUSINESS_A, entity_ref=_ENTITY_REF)

    result = await definitions["diagnose_entity"].handler(args, None)

    assert result["error"]["code"] == "ENTITY_NOT_FOUND"


async def test_simulate_spend_change_translates_args_and_delegates_to_the_spec() -> None:
    definitions = {d.name: d for d in build_optimization_tool_definitions(_service())}
    args = SimulateSpendChangeArgs(
        business_id=BUSINESS_A,
        entity_ref=_ENTITY_REF,
        product_id=_PRODUCT_ID,
        current_daily_spend_amount="100.00",
        spend_multiplier=1.5,
    )

    result = await definitions["simulate_spend_change"].handler(args, None)

    assert result["error"]["code"] == "ENTITY_NOT_FOUND"


async def test_propose_reallocation_plan_translates_args_and_delegates_to_the_spec() -> None:
    definitions = {d.name: d for d in build_optimization_tool_definitions(_service())}
    args = ProposeReallocationPlanArgs(business_id=BUSINESS_A)

    result = await definitions["propose_reallocation_plan"].handler(args, None)

    assert result["error"]["code"] == "ENTITY_NOT_FOUND"


def test_default_registry_omits_optimization_tools_without_service(
    read_model_ports: ReadModelPorts,
) -> None:
    registry = build_default_registry(read_model_ports, FixedClock(_NOW))

    for name in _EXPECTED_NAMES:
        assert registry.get(name) is None


def test_default_registry_includes_optimization_tools_when_wired(
    read_model_ports: ReadModelPorts,
) -> None:
    registry = build_default_registry(
        read_model_ports, FixedClock(_NOW), optimization_service=_service()
    )

    for name in _EXPECTED_NAMES:
        assert registry.get(name) is not None

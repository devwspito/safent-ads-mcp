"""`build_economics_tool_specs` (profitability-engine.md §8 P1): verbo
primero, sin verbos de aprobacion/ejecucion, capacidad reportada nunca
fingida (contracts/mcp-tools.md reglas 2, 3 y 6)."""

from __future__ import annotations

from datetime import UTC, datetime

import pytest

from safent_ads.economics.application.query_service import EconomicsQueryService
from safent_ads.economics.presentation.mcp_tools import build_economics_tool_specs
from safent_ads.economics.testing.in_memory_repositories import (
    InMemoryLagCurveRepository,
    InMemoryPlatformDivergenceRepository,
    InMemoryUnitEconomicsProfileRepository,
)
from safent_ads.shared.clock import FixedClock

_READ_PREFIXES = ("list_", "get_", "search_", "run_", "explain_")
_FORBIDDEN_DECISION_PREFIXES = ("approve_", "execute_", "apply_")


@pytest.fixture
def specs() -> list:
    service = EconomicsQueryService(
        profiles=InMemoryUnitEconomicsProfileRepository(),
        lag_curves=InMemoryLagCurveRepository(),
        divergences=InMemoryPlatformDivergenceRepository(),
        clock=FixedClock(datetime(2026, 6, 1, tzinfo=UTC)),
    )
    return build_economics_tool_specs(service)


def test_exposes_the_five_p1_tools(specs: list) -> None:
    names = {spec.name for spec in specs}
    assert names == {
        "get_unit_economics",
        "get_target_cpa",
        "get_attribution_lag_curve",
        "get_cohort_projection",
        "get_platform_divergence",
    }


def test_all_names_are_verb_first_reads(specs: list) -> None:
    for spec in specs:
        assert spec.name.startswith(_READ_PREFIXES)
        assert not spec.name.startswith(_FORBIDDEN_DECISION_PREFIXES)
        assert spec.verb_kind == "read"


def test_descriptions_are_non_empty(specs: list) -> None:
    for spec in specs:
        assert spec.description.strip()


class TestGetUnitEconomicsHandler:
    async def test_reports_not_found_capability_instead_of_raising(self, specs: list) -> None:
        spec = next(s for s in specs if s.name == "get_unit_economics")
        args = spec.args_model(
            business_id="11111111-1111-1111-1111-111111111111",
            product_id="22222222-2222-2222-2222-222222222222",
        )

        result = await spec.handler(args)

        assert result["error"]["code"] == "ENTITY_NOT_FOUND"

    def test_rejects_extra_fields(self, specs: list) -> None:
        spec = next(s for s in specs if s.name == "get_unit_economics")
        with pytest.raises(Exception, match="extra"):
            spec.args_model(
                business_id="11111111-1111-1111-1111-111111111111",
                product_id="22222222-2222-2222-2222-222222222222",
                extra_field="not allowed",
            )

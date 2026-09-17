"""`experiment_tools.py` (tasks.md T201): registro en el `ToolRegistry`
real, nombres verbo-primero validos, y que `build_default_registry` solo
las anade cuando se le pasan `experiment_services` (retrocompatible con el
resto de tests que construyen el registro sin ellos)."""

from __future__ import annotations

from datetime import UTC, datetime

import pytest

from safent_ads.mcp.presentation.catalog import build_default_registry
from safent_ads.mcp.presentation.experiment_tools import (
    ExperimentToolServices,
    build_experiment_tool_definitions,
)
from safent_ads.mcp.presentation.read_model_ports import ReadModelPorts
from safent_ads.mcp.presentation.registry import ToolClass
from safent_ads.optimization.application.design_experiment import DesignExperiment
from safent_ads.optimization.application.get_calibration_report import GetCalibrationReport
from safent_ads.optimization.application.get_experiment_status import GetExperimentStatus
from safent_ads.optimization.application.propose_experiment import ProposeExperiment
from safent_ads.optimization.testing.in_memory_repositories import (
    InMemoryCalibrationInputPort,
    InMemoryExperimentProposalPort,
    InMemoryExperimentRepository,
)
from safent_ads.shared.clock import FixedClock

_EXPECTED_NAMES = frozenset(
    {"design_experiment", "propose_experiment", "get_experiment_status", "get_calibration_report"}
)
_NOW = datetime(2026, 9, 9, tzinfo=UTC)


def _services() -> ExperimentToolServices:
    experiments = InMemoryExperimentRepository()
    return ExperimentToolServices(
        design=DesignExperiment(),
        propose=ProposeExperiment(
            proposals=InMemoryExperimentProposalPort(experiments),
            experiments=experiments,
            clock=FixedClock(_NOW),
        ),
        status=GetExperimentStatus(experiments),
        calibration_report=GetCalibrationReport(InMemoryCalibrationInputPort()),
    )


def test_build_experiment_tool_definitions_registers_the_four_verbs() -> None:
    definitions = build_experiment_tool_definitions(_services())

    names = {d.name for d in definitions}
    assert names == _EXPECTED_NAMES


def test_only_propose_experiment_is_a_proposal_class_tool() -> None:
    definitions = build_experiment_tool_definitions(_services())

    by_name = {d.name: d for d in definitions}
    assert by_name["propose_experiment"].tool_class is ToolClass.PROPOSAL
    for name in _EXPECTED_NAMES - {"propose_experiment"}:
        assert by_name[name].tool_class is ToolClass.READ


def test_default_registry_omits_experiment_tools_without_services(
    read_model_ports: ReadModelPorts,
) -> None:
    registry = build_default_registry(read_model_ports, FixedClock(_NOW))

    for name in _EXPECTED_NAMES:
        assert registry.get(name) is None


def test_default_registry_includes_experiment_tools_when_wired(
    read_model_ports: ReadModelPorts,
) -> None:
    registry = build_default_registry(
        read_model_ports, FixedClock(_NOW), experiment_services=_services()
    )

    for name in _EXPECTED_NAMES:
        assert registry.get(name) is not None


@pytest.mark.parametrize("name", sorted(_EXPECTED_NAMES))
def test_tool_names_pass_the_registry_naming_guard(name: str) -> None:
    # Si el nombre violara la convencion verbo-primero, ToolRegistry ya
    # habria lanzado al construir el registro en los tests de arriba.
    definitions = build_experiment_tool_definitions(_services())
    assert any(d.name == name for d in definitions)

"""Las 4 herramientas MCP de experimentacion/calibracion (profitability-
engine.md §4/§6/§8 P2, tasks.md T201): `design_experiment`,
`propose_experiment`, `get_experiment_status`, `get_calibration_report`.

Modulo autonomo (mismo criterio de aislamiento que `optimization.
presentation.mcp_tools`, adaptado al `ToolRegistry` real de esta lane en
vez de al `ToolSpec` sin cablear): declara sus propios `Args` sobre
`mcp.presentation.args.ToolArgs` (mismas invariantes de seguridad --
`extra=forbid`, sin URLs libres -- y `business_id` obligatorio, regla 4
del contrato) y sus propios handlers. `catalog.py` la engancha con una
linea (`build_experiment_tool_definitions`), sin tocar `handlers.py`/
`write_handlers.py`/`read_model_ports.py`.

Solo `propose_experiment` escribe (crea una `Proposal` pendiente, nunca
autonoma -- §4); las otras tres son lectura/simulacion en seco, ninguna
persiste nada (regla 3 del contrato)."""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Any

from pydantic import Field

from safent_ads.mcp.application.caller_scope import CallerScope
from safent_ads.mcp.presentation.args import BusinessId as BusinessIdStr
from safent_ads.mcp.presentation.args import EntityRefStr, OpaqueId, ToolArgs
from safent_ads.mcp.presentation.registry import ToolClass, ToolDefinition
from safent_ads.optimization.application.design_experiment import (
    DesignExperiment,
    DesignExperimentRequest,
)
from safent_ads.optimization.application.dto import (
    CalibrationReportView,
    ExperimentDesignView,
    ExperimentStatusView,
)
from safent_ads.optimization.application.get_calibration_report import GetCalibrationReport
from safent_ads.optimization.application.get_experiment_status import GetExperimentStatus
from safent_ads.optimization.application.propose_experiment import (
    ProposeExperiment,
    ProposeExperimentRequest,
)
from safent_ads.optimization.domain.identifiers import ExperimentId
from safent_ads.shared.ids import BusinessId, EntityRef

__all__ = ["ExperimentToolServices", "build_experiment_tool_definitions"]

type Handler[ArgsT, ReturnT] = Callable[[ArgsT, CallerScope], Awaitable[ReturnT]]

_MIN_RATE = 0.0
_MAX_RATE = 1.0
_MAX_HYPOTHESIS_LENGTH = 500


@dataclass(frozen=True, slots=True)
class ExperimentToolServices:
    """Los 4 casos de uso de `optimization` que necesita esta lane --
    `catalog.py` los construye una vez (misma sesion/reloj que el resto de
    `optimization`) y se los pasa a `build_experiment_tool_definitions`."""

    design: DesignExperiment
    propose: ProposeExperiment
    status: GetExperimentStatus
    calibration_report: GetCalibrationReport


class DesignExperimentArgs(ToolArgs):
    business_id: BusinessIdStr
    baseline_rate: float = Field(ge=_MIN_RATE, le=_MAX_RATE)
    relative_mde: float = Field(gt=_MIN_RATE)
    available_units_per_arm_per_week: float = Field(ge=_MIN_RATE)
    metric_measures_business_conversion: bool = False
    median_lag_days: int | None = Field(default=None, ge=0)


class ProposeExperimentArgs(ToolArgs):
    business_id: BusinessIdStr
    entity_ref: EntityRefStr
    hypothesis: str = Field(min_length=1, max_length=_MAX_HYPOTHESIS_LENGTH)
    metric: str = Field(min_length=1, max_length=64)
    baseline_rate: float = Field(ge=_MIN_RATE, le=_MAX_RATE)
    relative_mde: float = Field(gt=_MIN_RATE)
    available_units_per_arm_per_week: float = Field(ge=_MIN_RATE)
    metric_measures_business_conversion: bool = False
    median_lag_days: int | None = Field(default=None, ge=0)


class GetExperimentStatusArgs(ToolArgs):
    business_id: BusinessIdStr
    experiment_id: OpaqueId


class GetCalibrationReportArgs(ToolArgs):
    business_id: BusinessIdStr


def build_experiment_tool_definitions(
    services: ExperimentToolServices,
) -> list[ToolDefinition[Any]]:
    return [
        ToolDefinition(
            name="design_experiment",
            description=(
                "Viabilidad de un experimento: muestra por brazo, dias y "
                "motivo tipado de rechazo si el volumen no alcanza."
            ),
            args_model=DesignExperimentArgs,
            tool_class=ToolClass.READ,
            handler=_design_experiment(services.design),
            business_id_of=_by_business_id,
        ),
        ToolDefinition(
            name="propose_experiment",
            description=(
                "Propone un experimento (diseno + hipotesis): crea una "
                "propuesta pendiente, nunca arranca solo."
            ),
            args_model=ProposeExperimentArgs,
            tool_class=ToolClass.PROPOSAL,
            handler=_propose_experiment(services.propose),
            business_id_of=_by_business_id,
        ),
        ToolDefinition(
            name="get_experiment_status",
            description="Estado y resultado de un experimento ya propuesto.",
            args_model=GetExperimentStatusArgs,
            tool_class=ToolClass.READ,
            handler=_get_experiment_status(services.status),
            business_id_of=_by_business_id,
        ),
        ToolDefinition(
            name="get_calibration_report",
            description="Precision y recomendacion de calibracion por regla.",
            args_model=GetCalibrationReportArgs,
            tool_class=ToolClass.READ,
            handler=_get_calibration_report(services.calibration_report),
            business_id_of=_by_business_id,
        ),
    ]


def _by_business_id(args: Any) -> str:  # noqa: ANN401 - extractor generico por posicion
    return str(args.business_id)


def _design_experiment(
    use_case: DesignExperiment,
) -> Handler[DesignExperimentArgs, ExperimentDesignView]:
    async def handler(
        args: DesignExperimentArgs, _caller_scope: CallerScope
    ) -> ExperimentDesignView:
        return use_case.execute(
            DesignExperimentRequest(
                baseline_rate=args.baseline_rate,
                relative_mde=args.relative_mde,
                available_units_per_arm_per_week=args.available_units_per_arm_per_week,
                metric_measures_business_conversion=args.metric_measures_business_conversion,
                median_lag_days=args.median_lag_days,
            )
        )

    return handler


def _propose_experiment(
    use_case: ProposeExperiment,
) -> Handler[ProposeExperimentArgs, ExperimentStatusView]:
    async def handler(
        args: ProposeExperimentArgs, _caller_scope: CallerScope
    ) -> ExperimentStatusView:
        return await use_case.execute(
            ProposeExperimentRequest(
                business_id=BusinessId.parse(args.business_id),
                entity_ref=EntityRef.parse(args.entity_ref),
                hypothesis=args.hypothesis,
                metric=args.metric,
                baseline_rate=args.baseline_rate,
                relative_mde=args.relative_mde,
                available_units_per_arm_per_week=args.available_units_per_arm_per_week,
                metric_measures_business_conversion=args.metric_measures_business_conversion,
                median_lag_days=args.median_lag_days,
            )
        )

    return handler


def _get_experiment_status(
    use_case: GetExperimentStatus,
) -> Handler[GetExperimentStatusArgs, ExperimentStatusView]:
    async def handler(
        args: GetExperimentStatusArgs, _caller_scope: CallerScope
    ) -> ExperimentStatusView:
        return await use_case.execute(experiment_id=ExperimentId.parse(args.experiment_id))

    return handler


def _get_calibration_report(
    use_case: GetCalibrationReport,
) -> Handler[GetCalibrationReportArgs, CalibrationReportView]:
    async def handler(
        _args: GetCalibrationReportArgs, _caller_scope: CallerScope
    ) -> CalibrationReportView:
        return await use_case.execute()

    return handler

"""`GetCalibrationReport` (contracts/mcp-tools.md P2 `get_calibration_report`,
profitability-engine.md §6): precision y recomendacion por regla, sobre el
mismo `CalibrationInputPort` que usa `RecalibrateRules` -- una sola verdad,
esto solo la presenta sin escribir nada."""

from __future__ import annotations

from safent_ads.optimization.application.dto import CalibrationReportView, RulePrecisionView
from safent_ads.optimization.application.ports import CalibrationInputPort
from safent_ads.optimization.domain.calibration import compute_precision, recommend_calibration


class GetCalibrationReport:
    def __init__(self, inputs: CalibrationInputPort) -> None:
        self._inputs = inputs

    async def execute(self) -> CalibrationReportView:
        rule_codes = await self._inputs.list_rule_codes_with_outcomes()
        rules = [await self._report_for(rule_code) for rule_code in sorted(rule_codes)]
        return CalibrationReportView(rules=tuple(rules))

    async def _report_for(self, rule_code: str) -> RulePrecisionView:
        outcomes = await self._inputs.list_outcomes_for_rule(rule_code=rule_code)
        report = compute_precision(outcomes)
        if report is None:  # pragma: no cover - rule_code ya viene de list_rule_codes_with_outcomes
            return RulePrecisionView(
                rule_code=rule_code, sample_size=0, precision=None, recommendation="sin dato"
            )
        recommendation = recommend_calibration(report)
        return RulePrecisionView(
            rule_code=rule_code,
            sample_size=report.sample_size,
            precision=report.precision,
            recommendation=recommendation.reason,
        )


__all__ = ["GetCalibrationReport"]

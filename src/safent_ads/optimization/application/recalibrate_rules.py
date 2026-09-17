"""`RecalibrateRules` (profitability-engine.md §6, tasks.md T200): dominio
hecho (`calibration.py`), superficie que le da los datos y hace el
escrito. Corre una vez por semana desde el worker (T200: "run weekly");
gate de "una vez a la semana" en la base (`calibration_adjustments`
`UNIQUE (rule_code, threshold_name, week_start)`, 0026_experiments) mas un
pre-chequeo aqui para no pagar el trabajo de calcular precision cuando ya
se sabe que esta semana esta cerrada.

**Assumption documentada, escalada a `database-engineer`/`tech-lead`**:
`rules` es catalogo `scope = 'global'` (una fila por `code`, sin variante
por negocio o cuenta, 0007_rules_guardrails). `signal_outcomes` SI guarda
`(business_id, account_id, rule_code)` completo (§6 lo exige para
`get_calibration_report`), pero el ajuste que se aplica al umbral vivo es
agregado sobre TODAS las salidas de ese `rule_code`, en cualquier negocio.
Un umbral calibrado por cuenta exigiria extender `rules` mas alla de este
lote -- fuera de alcance de `calibration-experiments`.

**Solo aprieta.** Este caso de uso nunca construye un ajuste agresivo: si
`recommend_calibration` devuelve `should_loosen`, se ignora aqui (§6:
"volver a ser agresivo en una AUTO exige aprobacion" -- esa tool,
`propose_threshold_calibration`, no esta en el alcance de T198-T203)."""

from __future__ import annotations

from datetime import date, timedelta

from safent_ads.optimization.application.ports import (
    CalibrationAdjustmentLogPort,
    CalibrationInputPort,
    RuleCalibrationState,
    RuleCalibrationStatePort,
)
from safent_ads.optimization.domain.calibration import (
    AutonomyLevel,
    CalibrationAdjustment,
    ThresholdDirection,
    build_conservative_step,
    compute_precision,
    recommend_calibration,
)
from safent_ads.optimization.domain.identifiers import CalibrationAdjustmentId
from safent_ads.shared.clock import Clock

_MAGNITUDE_PCT_RANGE = (5.0, 50.0)
_COOLDOWN_MINUTES_RANGE = (30.0, 4320.0)  # 30 min .. 3 dias


class RecalibrateRules:
    def __init__(
        self,
        *,
        inputs: CalibrationInputPort,
        state: RuleCalibrationStatePort,
        log: CalibrationAdjustmentLogPort,
        clock: Clock,
    ) -> None:
        self._inputs = inputs
        self._state = state
        self._log = log
        self._clock = clock

    async def execute(self) -> tuple[CalibrationAdjustment, ...]:
        week_start = _iso_week_start(self._clock.now().date())
        applied = []
        for rule_code in await self._inputs.list_rule_codes_with_outcomes():
            adjustment = await self._recalibrate_one(rule_code, week_start=week_start)
            if adjustment is not None:
                applied.append(adjustment)
        return tuple(applied)

    async def _recalibrate_one(
        self, rule_code: str, *, week_start: date
    ) -> CalibrationAdjustment | None:
        state = await self._state.get_state(rule_code=rule_code)
        if state is None or state.autonomy_level is not AutonomyLevel.AUTO:
            return None
        threshold_name, direction, current_value, value_range = _pick_threshold(state)
        already_done = await self._log.already_adjusted_this_week(
            rule_code=rule_code, threshold_name=threshold_name, week_start=week_start
        )
        if already_done:
            return None
        return await self._tighten_if_warranted(
            rule_code,
            threshold_name=threshold_name,
            direction=direction,
            current_value=current_value,
            value_range=value_range,
            week_start=week_start,
        )

    async def _tighten_if_warranted(
        self,
        rule_code: str,
        *,
        threshold_name: str,
        direction: ThresholdDirection,
        current_value: float,
        value_range: tuple[float, float],
        week_start: date,
    ) -> CalibrationAdjustment | None:
        outcomes = await self._inputs.list_outcomes_for_rule(rule_code=rule_code)
        report = compute_precision(outcomes)
        if report is None or not recommend_calibration(report).should_tighten:
            return None
        adjustment = build_conservative_step(
            adjustment_id=CalibrationAdjustmentId.new(),
            rule_code=rule_code,
            threshold_name=threshold_name,
            direction=direction,
            current_value=current_value,
            threshold_range=value_range,
            now=self._clock.now(),
        )
        await self._state.apply_adjustment(
            rule_code=rule_code, threshold_name=threshold_name, new_value=adjustment.new_value
        )
        await self._log.record_adjustment(
            adjustment=adjustment,
            sample_size=report.sample_size,
            precision=report.precision,
            week_start=week_start,
            business_ids=tuple(sorted({o.business_id for o in outcomes})),
        )
        return adjustment


def _pick_threshold(
    state: RuleCalibrationState,
) -> tuple[str, ThresholdDirection, float, tuple[float, float]]:
    """§6 nombra dos palancas concretas ('recorte menor, cooldown mas
    largo'); se elige `magnitude_pct` cuando la regla lo declara (un
    recorte mas pequeno es mas conservador) y `cooldown_minutes` en caso
    contrario (siempre presente, mas tiempo entre disparos es mas
    conservador). Umbrales por umbral (rango de gasto/CPA del catalogo)
    quedan fuera: no hay metadato de rango por metrica todavia."""
    if state.magnitude_pct is not None:
        return (
            "magnitude_pct",
            ThresholdDirection.LOWER_IS_MORE_CONSERVATIVE,
            state.magnitude_pct,
            _MAGNITUDE_PCT_RANGE,
        )
    return (
        "cooldown_minutes",
        ThresholdDirection.HIGHER_IS_MORE_CONSERVATIVE,
        state.cooldown_minutes,
        _COOLDOWN_MINUTES_RANGE,
    )


def _iso_week_start(day: date) -> date:
    return day - timedelta(days=day.isoweekday() - 1)


__all__ = ["RecalibrateRules"]

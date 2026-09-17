"""Puertas (rule-catalog-and-signals.md §1 'Gate'): precondiciones que una
entidad debe cumplir para que se emita una senal accionable. Puras: sin
`Clock`, sin repositorios — reciben el instante `as_of` como parametro.

Una puerta fallida siempre produce `HOLD` (`test_learning_entity_never_actionable`,
data-model.md §Signal)."""

from __future__ import annotations

from datetime import date, datetime, timedelta
from enum import StrEnum

from safent_ads.signals.domain.gate_verdict import GateName, GateVerdict


class LearningStatus(StrEnum):
    """Estado de aprendizaje de la entidad (Meta `learning_stage_info.status`;
    Google se normaliza a `SUCCESS` cuando la estrategia ya no esta en
    aprendizaje). Definido localmente: `signals` no importa `accounts`
    (plan.md §4: 'signals -> shared; recibe DTOs')."""

    LEARNING = "learning"
    FAIL = "fail"
    SUCCESS = "success"


class LearningGate:
    """M23: `learning_stage_info.status in {LEARNING, FAIL}` => HOLD."""

    @staticmethod
    def evaluate(status: LearningStatus) -> GateVerdict:
        if status is LearningStatus.SUCCESS:
            return GateVerdict.ok(GateName.LEARNING)
        return GateVerdict.blocked(
            GateName.LEARNING, f"entidad en fase de aprendizaje ({status.value})"
        )


class MinDataGate:
    """Volumen minimo antes de decidir: gasto >= k x CPA objetivo con
    impresiones suficientes, o un numero de conversiones ya observado
    (rule-catalog-and-signals.md §1 'Min data')."""

    @staticmethod
    def evaluate(
        *,
        spend_minor: int,
        target_cpa_minor: int,
        impressions: int,
        conversions: int,
        min_spend_multiple: float,
        min_impressions: int,
        min_conversions: int,
    ) -> GateVerdict:
        spend_ok = (
            spend_minor >= min_spend_multiple * target_cpa_minor and impressions >= min_impressions
        )
        if spend_ok or conversions >= min_conversions:
            return GateVerdict.ok(GateName.MIN_DATA)
        return GateVerdict.blocked(
            GateName.MIN_DATA,
            f"volumen insuficiente: gasto={spend_minor}, impresiones={impressions}, "
            f"conversiones={conversions}",
        )


class AttributionLagGate:
    """Excluye los ultimos `median_lag_days` de la ventana: las conversiones
    mas recientes aun pueden no haber llegado (rule-catalog-and-signals.md
    §1 'Conversion lag')."""

    @staticmethod
    def evaluate(*, window_end: date, median_lag_days: int, as_of: date) -> GateVerdict:
        elapsed_days = (as_of - window_end).days
        if elapsed_days >= median_lag_days:
            return GateVerdict.ok(GateName.ATTRIBUTION_LAG)
        return GateVerdict.blocked(
            GateName.ATTRIBUTION_LAG,
            f"faltan {median_lag_days - elapsed_days} dia(s) de rezago de atribucion",
        )


class CooldownGate:
    """Ultimo cambio + cooldown de la regla (rule-catalog-and-signals.md §1
    'Cooldown')."""

    @staticmethod
    def evaluate(
        *, last_change_at: datetime | None, cooldown: timedelta, as_of: datetime
    ) -> GateVerdict:
        if last_change_at is None or as_of - last_change_at >= cooldown:
            return GateVerdict.ok(GateName.COOLDOWN)
        remaining = cooldown - (as_of - last_change_at)
        return GateVerdict.blocked(GateName.COOLDOWN, f"en cooldown, faltan {remaining}")

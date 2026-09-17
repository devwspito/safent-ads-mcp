"""Reconciliacion plataforma-vs-CRM (tasks.md T116; profitability-engine.md
§2: "el CRM es la verdad; la plataforma sirve para pujar y como senal
temprana"; FR-3/FR-5): una senal accionable se contradice cuando, sobre la
MISMA ventana de evidencia que la disparo, la plataforma reporto
conversiones que el CRM -- fuente de verdad -- no confirma ni de lejos.

Reconciliacion DELGADA a proposito: no duplica el `delta_hat` con
encogimiento sobre 8 semanas cerradas de `economics.ComputePlatformDivergence`
(pensado para decisiones de cartera, otra granularidad) -- aqui basta un
umbral conservador con volumen minimo para no juzgar una senal sobre ruido
de pocas conversiones."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date

from safent_ads.shared.events import DomainEvent
from safent_ads.shared.ids import EntityRef

MIN_PLATFORM_CONVERSIONS_TO_JUDGE = 5
CONTRADICTION_RATIO_THRESHOLD = 0.5


@dataclass(frozen=True, kw_only=True, slots=True)
class ConversionReconciliation:
    """`platform_conversions`/`crm_conversions` sobre la ventana de
    evidencia de la senal (`window_start`/`window_end`, ambas inclusive --
    mismo criterio que `MetricFactRepository.find_in_window`)."""

    entity_ref: EntityRef
    window_start: date
    window_end: date
    platform_conversions: int
    crm_conversions: int

    def __post_init__(self) -> None:
        if self.platform_conversions < 0 or self.crm_conversions < 0:
            raise ValueError("platform_conversions y crm_conversions deben ser >= 0")
        if self.window_start > self.window_end:
            raise ValueError(f"window_start {self.window_start} > window_end {self.window_end}")

    @property
    def is_platform_claim_disproved(self) -> bool:
        """La plataforma reclama lo que el CRM desmiente: exige volumen
        minimo (no juzgar sobre ruido) y una razon crm/plataforma por
        debajo del umbral conservador."""
        if self.platform_conversions < MIN_PLATFORM_CONVERSIONS_TO_JUDGE:
            return False
        ratio = self.crm_conversions / self.platform_conversions
        return ratio < CONTRADICTION_RATIO_THRESHOLD


@dataclass(frozen=True, kw_only=True)
class SignalContradicted(DomainEvent):
    """Evento de dominio (data-model.md §Signal: 'Eventos: SignalEmitted,
    SignalContradicted (por conversion tardia)'). `account_id`/`rule_code`
    viajan opacos -- `metrics` no conoce el agregado `Signal` de `signals`,
    solo lo que su escritor de destino (T199, `optimization`) necesita para
    persistir el resultado (mismo patron que
    `optimization.application.ports.DueSignalOutcome`)."""

    signal_id: str
    entity_ref: EntityRef
    account_id: str
    rule_code: str
    reconciliation: ConversionReconciliation

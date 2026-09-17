"""Puertos de `orchestration` (T047). `IngestionStepPort`/
`SignalEvaluationStepPort` son el punto de entrada de un paso por negocio
que otra lane implementa (`metrics`/`signals`); `BusinessListingPort` viene
de `accounts`. `notifications` no necesita puerto adicional: `orchestration`
puede importarla directamente (transversal, plan.md §4)."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Protocol

from safent_ads.shared.ids import BusinessId


class BusinessListingPort(Protocol):
    """Puerto hacia `accounts`: negocios activos a recorrer en cada ciclo."""

    async def list_active_business_ids(self) -> list[BusinessId]: ...


class IngestionStepPort(Protocol):
    """Puerto hacia `metrics`: ingesta de metricas + frescura de un negocio
    (plan.md §7 `IngestionCycle`). Firma posicional (sin keyword-only) para
    que coincida exactamente con `StepOperation`, el `Callable` que
    `run_step_for_business` invoca."""

    async def run(self, business_id: BusinessId, cycle_id: str, now: datetime) -> None: ...


class SignalEvaluationStepPort(Protocol):
    """Puerto hacia `signals`: puertas -> senales -> anomalias -> pacing de
    un negocio (plan.md §7 `SignalCycle`)."""

    async def run(self, business_id: BusinessId, cycle_id: str, now: datetime) -> None: ...


class RuleEvaluationStepPort(Protocol):
    """Puerto hacia `rules`/`proposals`/`execution`: senal -> regla ->
    autorizacion de regla o propuesta (plan.md §7 `RuleCycle`)."""

    async def run(self, business_id: BusinessId, cycle_id: str, now: datetime) -> None: ...


class SignalOutcomeEvaluationStepPort(Protocol):
    """Puerto hacia `optimization`: contraste a 14 dias de las senales
    accionables vencidas de un negocio (profitability-engine.md §6,
    tasks.md T199 `SignalOutcomeCycle`)."""

    async def run(self, business_id: BusinessId, cycle_id: str, now: datetime) -> None: ...


class EconomicsStepPort(Protocol):
    """Puerto hacia `economics`: por negocio, llena `unit_economics_profiles`
    (`BuildUnitEconomicsProfile`), `lag_curve_snapshots`
    (`ComputeLagCurve`) y `platform_divergence_snapshots`
    (`ComputePlatformDivergence`) -- T156, plan.md §7 `EconomicsCycle`."""

    async def run(self, business_id: BusinessId, cycle_id: str, now: datetime) -> None: ...


class RuleCalibrationStepPort(Protocol):
    """Puerto hacia `optimization`: recalibra umbrales `AUTO` a partir de
    los `SignalOutcome` acumulados (profitability-engine.md §6, tasks.md
    T200 `RuleCalibrationCycle`). Global, no por negocio -- ver Assumption
    en `RecalibrateRules`."""

    async def run(self, cycle_id: str, now: datetime) -> None: ...


class OpportunityStepPort(Protocol):
    """Puerto hacia `opportunities`: por negocio, genera `OpportunityCandidate`s
    desde huecos de calendario sin cobertura y los materializa como
    `Proposal`s `CREATE_CAMPAIGN` (tasks.md T113, FR-35/FR-36)."""

    async def run(self, business_id: BusinessId, cycle_id: str, now: datetime) -> None: ...


class CredentialHealthStepPort(Protocol):
    """Puerto hacia `accounts`/`notifications`/`audit`: por negocio, pide
    al broker el estado de cada credencial (`CheckCredentialHealth`,
    threat-model.md C-21, tasks.md T126), lo persiste y alerta las
    transiciones a un estado que exige reconectar."""

    async def run(self, business_id: BusinessId, cycle_id: str, now: datetime) -> None: ...


class MaintenanceStepPort(Protocol):
    """Puerto hacia las piezas sueltas que `MaintenanceCycle` corre a
    diario (plan.md §7, tasks.md T078): expirar propuestas vencidas
    (`proposals`), purgar nonces/codigos de Telegram consumidos o
    caducados (`notifications`, SQL directo -- esta lane no importa ese
    contexto, mismo criterio que `LiveEconomicsStep` sobre `offerings`),
    verificar la cadena del `decision_log` (`audit`) y reconciliar
    plataforma vs CRM por negocio (`metrics`, T116). Los tres primeros son
    globales (`business_id=None` en el `StepResult`); el cuarto es por
    negocio."""

    async def expire_stale_proposals(self, now: datetime) -> None: ...

    async def purge_telegram_artifacts(self, now: datetime) -> None: ...

    async def verify_decision_log_chain(self, now: datetime) -> None: ...

    async def reconcile_platform_vs_crm(
        self, business_id: BusinessId, cycle_id: str, now: datetime
    ) -> None: ...


@dataclass(frozen=True, slots=True)
class NotificationTarget:
    """Lo que `NotificationCycle` necesita de cada negocio para publicar
    (nombre para la cabecera del ticker, chats del propietario)."""

    business_id: BusinessId
    business_name: str
    owner_chat_ids: tuple[int, ...]


class NotificationTargetsPort(Protocol):
    """Puerto hacia `accounts`/`iam`: negocios con notificaciones activas."""

    async def list_notification_targets(self) -> list[NotificationTarget]: ...

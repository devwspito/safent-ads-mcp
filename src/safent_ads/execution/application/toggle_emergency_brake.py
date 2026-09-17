"""`ToggleEmergencyBrake` (T061; FR-14). `POST /kill-switch` debe ser
inmediato: no espera al siguiente ciclo. `BrakeStatePort.get` se lee fresco
en cada ruta de ejecucion — nunca cacheado (threat-model.md C-18)."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime

from safent_ads.execution.application.ports import BrakeStatePort
from safent_ads.execution.domain.guardrails import (
    BrakeMode,
    BrakeScope,
    EmergencyBrake,
    EmergencyBrakeEngaged,
    EmergencyBrakeReleased,
)
from safent_ads.shared.clock import Clock
from safent_ads.shared.errors import ApplicationError
from safent_ads.shared.events import DecisionRecorder
from safent_ads.shared.ids import BusinessId


class BrakeAlreadyEngagedError(ApplicationError):
    """Invariante de `data-model.md`: un freno activo por ambito como
    maximo. Reactivar uno ya activo es un error del llamador, no un no-op
    silencioso — el propietario debe saber que ya estaba encendido."""


class NoBrakeRegisteredError(ApplicationError):
    """Se pidio liberar un freno que nunca se activo en ese ambito."""


@dataclass(frozen=True, slots=True)
class EngageBrakeCommand:
    business_id: BusinessId
    scope: BrakeScope
    mode: BrakeMode
    reason: str
    engaged_by: str = "unknown"


@dataclass(frozen=True, slots=True)
class ReleaseBrakeCommand:
    business_id: BusinessId
    scope: BrakeScope
    released_by: str = "unknown"


class ToggleEmergencyBrake:
    def __init__(self, brakes: BrakeStatePort, recorder: DecisionRecorder, clock: Clock) -> None:
        self._brakes = brakes
        self._recorder = recorder
        self._clock = clock

    async def engage(self, command: EngageBrakeCommand) -> EmergencyBrake:
        now = self._clock.now()
        existing = await self._brakes.get(command.scope)
        if existing is not None and existing.engaged:
            raise BrakeAlreadyEngagedError(f"freno ya activo en {command.scope}")
        brake = existing or EmergencyBrake(scope=command.scope, mode=command.mode)
        brake.mode = command.mode
        brake.engage(command.reason, now)
        await self._brakes.save(brake)
        await self._recorder.record(_engaged_event(command, now))
        return brake

    async def release(self, command: ReleaseBrakeCommand) -> EmergencyBrake:
        now = self._clock.now()
        brake = await self._brakes.get(command.scope)
        if brake is None:
            raise NoBrakeRegisteredError(f"no hay freno registrado en {command.scope}")
        brake.release(now)
        await self._brakes.save(brake)
        await self._recorder.record(_released_event(command, now))
        return brake


def _engaged_event(command: EngageBrakeCommand, now: datetime) -> EmergencyBrakeEngaged:
    return EmergencyBrakeEngaged(
        business_id=command.business_id,
        occurred_at=now,
        scope_kind=command.scope.kind.value,
        scope_ref=command.scope.ref,
        mode=command.mode.value,
        reason=command.reason,
        engaged_by=command.engaged_by,
    )


def _released_event(command: ReleaseBrakeCommand, now: datetime) -> EmergencyBrakeReleased:
    return EmergencyBrakeReleased(
        business_id=command.business_id,
        occurred_at=now,
        scope_kind=command.scope.kind.value,
        scope_ref=command.scope.ref,
        released_by=command.released_by,
    )

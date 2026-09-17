"""`ToggleEmergencyBrake` (T061) y la prueba estrella del freno: leerlo
fresco en cada ruta de ejecucion, nunca cacheado (threat-model.md C-18)."""

from __future__ import annotations

import pytest

from safent_ads.execution.application.toggle_emergency_brake import (
    BrakeAlreadyEngagedError,
    EngageBrakeCommand,
    NoBrakeRegisteredError,
    ReleaseBrakeCommand,
    ToggleEmergencyBrake,
)
from safent_ads.execution.domain.execution_attempt import ExecutionStatus
from safent_ads.execution.domain.guardrails import (
    BrakeMode,
    EmergencyBrakeEngaged,
    EmergencyBrakeReleased,
    brake_scope_from,
)
from safent_ads.execution.testing.fakes import FakeBrakeStatePort, FakeDecisionRecorder
from safent_ads.shared.clock import FixedClock
from safent_ads.shared.ids import BusinessId

from .conftest import NOW, budget_diff, entity_ref, entity_scope
from .test_chokepoint import _build_scenario

_SCOPE = entity_scope()
_BRAKE_SCOPE = brake_scope_from(_SCOPE)


def _use_case(brakes: FakeBrakeStatePort, recorder: FakeDecisionRecorder) -> ToggleEmergencyBrake:
    return ToggleEmergencyBrake(brakes, recorder, FixedClock(NOW))


class TestEngage:
    async def test_engage_sets_engaged_and_records_event(self) -> None:
        brakes = FakeBrakeStatePort()
        recorder = FakeDecisionRecorder()
        use_case = _use_case(brakes, recorder)
        command = EngageBrakeCommand(
            business_id=BusinessId.new(), scope=_BRAKE_SCOPE, mode=BrakeMode.ALL, reason="incidente"
        )

        brake = await use_case.engage(command)

        assert brake.engaged is True
        assert brake.reason == "incidente"
        stored = await brakes.get(_BRAKE_SCOPE)
        assert stored is not None
        assert stored.engaged is True
        assert any(isinstance(e, EmergencyBrakeEngaged) for e in recorder.recorded)

    async def test_engage_twice_raises(self) -> None:
        brakes = FakeBrakeStatePort()
        use_case = _use_case(brakes, FakeDecisionRecorder())
        command = EngageBrakeCommand(
            business_id=BusinessId.new(), scope=_BRAKE_SCOPE, mode=BrakeMode.ALL, reason="incidente"
        )
        await use_case.engage(command)

        with pytest.raises(BrakeAlreadyEngagedError):
            await use_case.engage(command)


class TestRelease:
    async def test_release_clears_engaged_and_records_event(self) -> None:
        brakes = FakeBrakeStatePort()
        recorder = FakeDecisionRecorder()
        use_case = _use_case(brakes, recorder)
        business_id = BusinessId.new()
        await use_case.engage(
            EngageBrakeCommand(
                business_id=business_id, scope=_BRAKE_SCOPE, mode=BrakeMode.ALL, reason="x"
            )
        )

        brake = await use_case.release(ReleaseBrakeCommand(business_id, _BRAKE_SCOPE))

        assert brake.engaged is False
        assert any(isinstance(e, EmergencyBrakeReleased) for e in recorder.recorded)

    async def test_release_without_prior_engage_raises(self) -> None:
        use_case = _use_case(FakeBrakeStatePort(), FakeDecisionRecorder())

        with pytest.raises(NoBrakeRegisteredError):
            await use_case.release(ReleaseBrakeCommand(BusinessId.new(), _BRAKE_SCOPE))


class TestKillSwitchBlocksInFlightWorker:
    async def test_kill_switch_blocks_in_flight_worker(self) -> None:
        """Dos ciclos sucesivos de `ExecutionChokepoint.run_once()` sobre el
        MISMO freno compartido: el primero corre sin freno; entre medias se
        activa; el segundo debe verlo SIN que nadie se lo notifique — prueba
        que `BrakeStatePort.get` se llama de nuevo en cada ciclo, nunca se
        cachea en el chokepoint ni en el worker."""
        shared_brakes = FakeBrakeStatePort()
        ref = entity_ref("shared-entity")

        first_scenario, _first_attempt = await _build_scenario(
            diff=budget_diff(ref=ref), brakes=shared_brakes
        )
        first_outcome = await first_scenario.chokepoint.run_once()
        assert first_outcome is ExecutionStatus.EXECUTED
        assert first_scenario.platform_write.call_count == 1

        toggle = _use_case(shared_brakes, FakeDecisionRecorder())
        await toggle.engage(
            EngageBrakeCommand(
                business_id=BusinessId.new(),
                scope=brake_scope_from(entity_scope(ref)),
                mode=BrakeMode.ALL,
                reason="freno de emergencia activado por el propietario",
            )
        )

        second_scenario, _second_attempt = await _build_scenario(
            diff=budget_diff(ref=ref, after="65"), brakes=shared_brakes
        )
        second_outcome = await second_scenario.chokepoint.run_once()

        assert second_outcome is ExecutionStatus.BLOCKED_BRAKE
        assert second_scenario.platform_write.call_count == 0

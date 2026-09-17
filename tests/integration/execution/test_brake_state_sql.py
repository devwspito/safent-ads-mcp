"""`SqlBrakeStatePort` contra Postgres real: el freno se lee fresco en cada
ruta de ejecucion, nunca cacheado (threat-model.md C-18)."""

from __future__ import annotations

import pytest

from safent_ads.execution.domain.guardrails import (
    BrakeMode,
    BrakeScope,
    EmergencyBrake,
    GuardrailScope,
    ScopeKind,
    brake_scope_from,
)
from safent_ads.execution.infrastructure.errors import UnknownEntityRefError
from safent_ads.execution.infrastructure.sql_brake_state import SqlBrakeStatePort
from tests.contracts.execution.conftest import NOW
from tests.integration.execution.conftest import CommittedScenario, committed_scenario

pytestmark = pytest.mark.integration


def account_scope(scenario: CommittedScenario) -> BrakeScope:
    return brake_scope_from(
        GuardrailScope(kind=ScopeKind.ENTITY, ref=str(scenario.context.entity_ref))
    )


async def test_kill_switch_blocks_in_flight_worker(isolated_database_url: str) -> None:
    """C-18: el propietario enciende el freno con el trabajador a mitad de
    ciclo y la SIGUIENTE lectura del trabajador ya lo ve. Sin cache no hay
    ventana en la que la autonomia siga actuando tras la parada."""
    async with committed_scenario(isolated_database_url) as scenario:
        scope = account_scope(scenario)

        async with scenario.session() as worker:
            brakes = SqlBrakeStatePort(worker)
            assert await brakes.get(scope) is None

            async with scenario.session() as owner:
                brake = EmergencyBrake(scope=scope, mode=BrakeMode.ALL)
                brake.engage("el propietario para todo", NOW)
                await SqlBrakeStatePort(owner).save(brake)
                await owner.commit()

            observed = await brakes.get(scope)

            assert observed is not None
            assert observed.engaged
            assert observed.reason == "el propietario para todo"


async def test_release_is_visible_to_the_next_call_too(isolated_database_url: str) -> None:
    """La misma frescura al soltar el freno: el trabajo no se queda
    congelado esperando al siguiente arranque del proceso."""
    async with committed_scenario(isolated_database_url) as scenario:
        scope = account_scope(scenario)
        brake = EmergencyBrake(scope=scope, mode=BrakeMode.ALL)
        brake.engage("incidente", NOW)
        async with scenario.session() as owner:
            await SqlBrakeStatePort(owner).save(brake)
            await owner.commit()

        async with scenario.session() as worker:
            brakes = SqlBrakeStatePort(worker)
            engaged = await brakes.get(scope)
            assert engaged is not None
            assert engaged.engaged

            async with scenario.session() as owner:
                brake.release(NOW)
                await SqlBrakeStatePort(owner).save(brake)
                await owner.commit()

            released = await brakes.get(scope)
            assert released is not None
            assert not released.engaged


async def test_a_scope_that_does_not_exist_is_denounced(isolated_database_url: str) -> None:
    """Un ambito que no resuelve a ninguna cuenta no se lee como "sin
    freno": eso seria abrir la ejecucion por un fallo de datos."""
    async with committed_scenario(isolated_database_url) as scenario:
        async with scenario.session() as worker:
            brakes = SqlBrakeStatePort(worker)
            unknown = brake_scope_from(
                GuardrailScope(kind=ScopeKind.ENTITY, ref="meta:campaign:no-existe")
            )

            with pytest.raises(UnknownEntityRefError):
                await brakes.get(unknown)

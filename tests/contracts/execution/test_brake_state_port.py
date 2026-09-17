"""Contrato de `BrakeStatePort`: identico para el doble en memoria y para
`SqlBrakeStatePort`."""

from __future__ import annotations

from datetime import timedelta

from safent_ads.execution.domain.guardrails import BrakeMode, EmergencyBrake
from safent_ads.proposals.domain.authorization import AuthorizationKind
from tests.contracts.execution.conftest import NOW, BrakeFixture


async def test_a_scope_without_brake_reads_as_none(brakes: BrakeFixture) -> None:
    scope = await brakes.given_account_scope()
    assert await brakes.brakes.get(scope) is None


async def test_engaged_brake_is_read_back_whole(brakes: BrakeFixture) -> None:
    scope = await brakes.given_account_scope()
    brake = EmergencyBrake(scope=scope, mode=BrakeMode.ALL)
    brake.engage("gasto disparado en la cuenta", NOW)

    await brakes.brakes.save(brake)
    stored = await brakes.brakes.get(scope)

    assert stored is not None
    assert stored.engaged
    assert stored.mode is BrakeMode.ALL
    assert stored.reason == "gasto disparado en la cuenta"
    assert stored.since == NOW
    assert stored.blocks(AuthorizationKind.HUMAN_APPROVAL)


async def test_autonomous_brake_only_blocks_the_rule_engine(brakes: BrakeFixture) -> None:
    scope = await brakes.given_account_scope()
    brake = EmergencyBrake(scope=scope, mode=BrakeMode.AUTONOMOUS)
    brake.engage("solo se congela lo autonomo", NOW)

    await brakes.brakes.save(brake)
    stored = await brakes.brakes.get(scope)

    assert stored is not None
    assert stored.blocks(AuthorizationKind.RULE_AUTHORIZATION)
    assert not stored.blocks(AuthorizationKind.HUMAN_APPROVAL)


async def test_released_brake_stops_blocking(brakes: BrakeFixture) -> None:
    scope = await brakes.given_account_scope()
    brake = EmergencyBrake(scope=scope, mode=BrakeMode.ALL)
    brake.engage("incidente", NOW)
    await brakes.brakes.save(brake)

    brake.release(NOW + timedelta(minutes=5))
    await brakes.brakes.save(brake)

    stored = await brakes.brakes.get(scope)
    assert stored is not None
    assert not stored.engaged
    assert not stored.blocks(AuthorizationKind.RULE_AUTHORIZATION)


async def test_brake_can_be_engaged_again_after_release(brakes: BrakeFixture) -> None:
    scope = await brakes.given_account_scope()
    brake = EmergencyBrake(scope=scope, mode=BrakeMode.ALL)
    brake.engage("primer incidente", NOW)
    await brakes.brakes.save(brake)
    brake.release(NOW + timedelta(minutes=5))
    await brakes.brakes.save(brake)

    brake.engage("segundo incidente", NOW + timedelta(minutes=10))
    await brakes.brakes.save(brake)

    stored = await brakes.brakes.get(scope)
    assert stored is not None
    assert stored.engaged
    assert stored.reason == "segundo incidente"


async def test_scopes_do_not_leak_into_each_other(brakes: BrakeFixture) -> None:
    """El freno global y el de una cuenta son ambitos distintos: encender uno
    no enciende el otro (FR-14: "freno global y por cuenta")."""
    account_scope = await brakes.given_account_scope()
    global_scope = brakes.given_global_scope()
    global_brake = EmergencyBrake(scope=global_scope, mode=BrakeMode.ALL)
    global_brake.engage("parada general", NOW)

    await brakes.brakes.save(global_brake)

    assert await brakes.brakes.get(account_scope) is None
    stored = await brakes.brakes.get(global_scope)
    assert stored is not None
    assert stored.engaged


# ---------------------------------------------------------------------------
# `get_effective`: el freno que de verdad decide una escritura (bug
# corregido -- los chokepoints solo miraban el ambito de cuenta, un freno
# GLOBAL o de NEGOCIO no paraba nada).
# ---------------------------------------------------------------------------


async def test_get_effective_with_no_brake_anywhere_is_none(brakes: BrakeFixture) -> None:
    scope = await brakes.given_account_scope()
    assert await brakes.brakes.get_effective(scope) is None


async def test_get_effective_sees_a_global_brake(brakes: BrakeFixture) -> None:
    account_scope = await brakes.given_account_scope()
    global_brake = EmergencyBrake(scope=brakes.given_global_scope(), mode=BrakeMode.ALL)
    global_brake.engage("parada general", NOW)
    await brakes.brakes.save(global_brake)

    effective = await brakes.brakes.get_effective(account_scope)

    assert effective is not None
    assert effective.blocks(AuthorizationKind.HUMAN_APPROVAL)


async def test_get_effective_sees_a_business_brake(brakes: BrakeFixture) -> None:
    account = await brakes.given_account_with_business()
    business_brake = EmergencyBrake(scope=account.business_scope, mode=BrakeMode.ALL)
    business_brake.engage("gasto disparado en el negocio", NOW)
    await brakes.brakes.save(business_brake)

    effective = await brakes.brakes.get_effective(account.account_scope)

    assert effective is not None
    assert effective.blocks(AuthorizationKind.HUMAN_APPROVAL)


async def test_get_effective_ignores_another_businesss_brake(brakes: BrakeFixture) -> None:
    account = await brakes.given_account_with_business()
    other_business = await brakes.given_account_with_business()
    other_brake = EmergencyBrake(scope=other_business.business_scope, mode=BrakeMode.ALL)
    other_brake.engage("incidente de otro negocio", NOW)
    await brakes.brakes.save(other_brake)

    assert await brakes.brakes.get_effective(account.account_scope) is None


async def test_get_effective_honours_autonomous_mode_from_the_widest_scope(
    brakes: BrakeFixture,
) -> None:
    """Un freno GLOBAL en modo `AUTONOMOUS` no bloquea una aprobacion
    humana -- el mismo criterio que `EmergencyBrake.blocks` ya aplica a un
    unico ambito, ahora tambien al freno EFECTIVO combinado."""
    account_scope = await brakes.given_account_scope()
    global_brake = EmergencyBrake(scope=brakes.given_global_scope(), mode=BrakeMode.AUTONOMOUS)
    global_brake.engage("degradacion", NOW)
    await brakes.brakes.save(global_brake)

    effective = await brakes.brakes.get_effective(account_scope)

    assert effective is not None
    assert effective.blocks(AuthorizationKind.RULE_AUTHORIZATION)
    assert not effective.blocks(AuthorizationKind.HUMAN_APPROVAL)


async def test_get_effective_prefers_all_mode_over_a_wider_autonomous_brake(
    brakes: BrakeFixture,
) -> None:
    """Un freno GLOBAL laxo (`AUTONOMOUS`) no debe tapar uno de NEGOCIO mas
    estrecho pero mas estricto (`ALL`): la aprobacion humana sigue
    bloqueada."""
    account = await brakes.given_account_with_business()
    global_brake = EmergencyBrake(scope=brakes.given_global_scope(), mode=BrakeMode.AUTONOMOUS)
    global_brake.engage("degradacion global", NOW)
    await brakes.brakes.save(global_brake)
    business_brake = EmergencyBrake(scope=account.business_scope, mode=BrakeMode.ALL)
    business_brake.engage("incidente de negocio", NOW)
    await brakes.brakes.save(business_brake)

    effective = await brakes.brakes.get_effective(account.account_scope)

    assert effective is not None
    assert effective.blocks(AuthorizationKind.HUMAN_APPROVAL)


async def test_get_effective_released_business_brake_stops_blocking(brakes: BrakeFixture) -> None:
    account = await brakes.given_account_with_business()
    business_brake = EmergencyBrake(scope=account.business_scope, mode=BrakeMode.ALL)
    business_brake.engage("incidente", NOW)
    await brakes.brakes.save(business_brake)

    business_brake.release(NOW + timedelta(minutes=5))
    await brakes.brakes.save(business_brake)

    assert await brakes.brakes.get_effective(account.account_scope) is None

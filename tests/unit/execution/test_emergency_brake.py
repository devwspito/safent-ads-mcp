"""`EmergencyBrake` (FR-14) — un freno activo detiene la actuacion al instante."""

from __future__ import annotations

from safent_ads.execution.domain.guardrails import (
    BrakeMode,
    BrakeScope,
    BrakeScopeKind,
    EmergencyBrake,
)
from safent_ads.proposals.domain.authorization import AuthorizationKind

from .conftest import NOW


def _brake(mode: BrakeMode) -> EmergencyBrake:
    return EmergencyBrake(scope=BrakeScope(BrakeScopeKind.GLOBAL), mode=mode)


class TestDisengagedBrakeNeverBlocks:
    def test_disengaged_blocks_nothing(self) -> None:
        brake = _brake(BrakeMode.ALL)

        assert brake.blocks(AuthorizationKind.RULE_AUTHORIZATION) is False
        assert brake.blocks(AuthorizationKind.HUMAN_APPROVAL) is False


class TestAutonomousModeOnlyBlocksRuleAuthorizations:
    def test_blocks_rule_authorization(self) -> None:
        brake = _brake(BrakeMode.AUTONOMOUS)
        brake.engage("degradacion detectada", NOW)

        assert brake.blocks(AuthorizationKind.RULE_AUTHORIZATION) is True

    def test_does_not_block_human_approval(self) -> None:
        brake = _brake(BrakeMode.AUTONOMOUS)
        brake.engage("degradacion detectada", NOW)

        assert brake.blocks(AuthorizationKind.HUMAN_APPROVAL) is False

    def test_blocks_package_step(self) -> None:
        """`003-paquete-de-campana` data-model.md Revision 2 §R2.9 (AL-1):
        un paso de publicacion corre sin humano delante -- para el freno
        cuenta como autonomo aunque derive de una aprobacion humana."""
        brake = _brake(BrakeMode.AUTONOMOUS)
        brake.engage("degradacion detectada", NOW)

        assert brake.blocks(AuthorizationKind.PACKAGE_STEP) is True


class TestAllModeBlocksEverything:
    def test_blocks_rule_authorization(self) -> None:
        brake = _brake(BrakeMode.ALL)
        brake.engage("incidente critico", NOW)

        assert brake.blocks(AuthorizationKind.RULE_AUTHORIZATION) is True

    def test_blocks_human_approval_too(self) -> None:
        brake = _brake(BrakeMode.ALL)
        brake.engage("incidente critico", NOW)

        assert brake.blocks(AuthorizationKind.HUMAN_APPROVAL) is True

    def test_blocks_package_step_too(self) -> None:
        brake = _brake(BrakeMode.ALL)
        brake.engage("incidente critico", NOW)

        assert brake.blocks(AuthorizationKind.PACKAGE_STEP) is True


class TestReleaseClearsBlocking:
    def test_release_stops_blocking(self) -> None:
        brake = _brake(BrakeMode.ALL)
        brake.engage("incidente", NOW)

        brake.release(NOW)

        assert brake.blocks(AuthorizationKind.HUMAN_APPROVAL) is False
        assert brake.engaged is False
        assert brake.reason is None

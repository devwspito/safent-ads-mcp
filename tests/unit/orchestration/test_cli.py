"""`_cycle_runner`/`_CYCLE_CHOICES` (T113/T114/T078, quickstart.md §8):
cada opcion de `--cycle` declarada debe resolver a un runner real -- una
opcion sin runner (o viceversa) rompe la CLI en silencio hasta que alguien
la prueba a mano."""

from __future__ import annotations

from unittest.mock import MagicMock

import pytest

from safent_ads.orchestration.presentation.cli import _CYCLE_CHOICES, _cycle_runner


@pytest.mark.parametrize("cycle_name", _CYCLE_CHOICES)
def test_every_declared_cycle_choice_has_a_runner(cycle_name: str) -> None:
    runtime = MagicMock()

    runner = _cycle_runner(runtime, cycle_name)

    assert runner is not None


def test_opportunities_and_maintenance_are_declared() -> None:
    assert "opportunities" in _CYCLE_CHOICES
    assert "maintenance" in _CYCLE_CHOICES
    assert "credential_health" in _CYCLE_CHOICES

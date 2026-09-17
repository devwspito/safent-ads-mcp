"""`Measure`/`MeasureStatus` (026, tasks.md T006, contracts/cockpit-read-model.md
§2): hace irrepresentable "un numero sin estado" -- ninguna combinacion
fuera de `available()`/`unavailable()` construye."""

from __future__ import annotations

import pytest

from safent_ads.shared.read_models.dto import Measure, MeasureInvariantError, MeasureStatus

_NON_AVAILABLE_STATUSES = [
    status for status in MeasureStatus if status is not MeasureStatus.AVAILABLE
]


def test_available_carries_the_value_and_no_reason() -> None:
    measure = Measure.available(42)

    assert measure.status is MeasureStatus.AVAILABLE
    assert measure.value == 42
    assert measure.reason is None


@pytest.mark.parametrize("status", _NON_AVAILABLE_STATUSES)
def test_unavailable_never_carries_a_value(status: MeasureStatus) -> None:
    measure: Measure[int] = Measure.unavailable(status, reason="sin datos suficientes")

    assert measure.status is status
    assert measure.value is None
    assert measure.reason == "sin datos suficientes"


def test_unavailable_requires_a_reason() -> None:
    with pytest.raises(MeasureInvariantError):
        Measure.unavailable(MeasureStatus.NO_DATA, reason="")


def test_unavailable_rejects_the_available_status() -> None:
    with pytest.raises(MeasureInvariantError):
        Measure.unavailable(MeasureStatus.AVAILABLE, reason="no debería llegar aquí")


def test_constructing_available_without_a_value_is_rejected() -> None:
    with pytest.raises(MeasureInvariantError):
        Measure(status=MeasureStatus.AVAILABLE, value=None)


def test_constructing_available_with_a_reason_is_rejected() -> None:
    with pytest.raises(MeasureInvariantError):
        Measure(status=MeasureStatus.AVAILABLE, value=1, reason="no debería llevar motivo")


@pytest.mark.parametrize("status", _NON_AVAILABLE_STATUSES)
def test_constructing_non_available_with_a_value_is_rejected(status: MeasureStatus) -> None:
    with pytest.raises(MeasureInvariantError):
        Measure(status=status, value=1, reason="motivo")


@pytest.mark.parametrize("status", _NON_AVAILABLE_STATUSES)
def test_constructing_non_available_without_a_reason_is_rejected(status: MeasureStatus) -> None:
    with pytest.raises(MeasureInvariantError):
        Measure(status=status, value=None, reason=None)

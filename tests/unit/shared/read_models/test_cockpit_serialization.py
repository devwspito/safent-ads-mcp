"""`to_cockpit_json_value` (026, tasks.md T006, contracts/cockpit-read-model.md
§2): propiedad -- ningun `Measure` no-`available` serializa un numero;
dinero como cadena decimal, nunca `float`."""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal

import pytest

from safent_ads.shared.read_models.dto import Measure, MeasureStatus, Money
from safent_ads.shared.read_models.serialization import to_cockpit_json_dict, to_cockpit_json_value

_NON_AVAILABLE_STATUSES = [s for s in MeasureStatus if s is not MeasureStatus.AVAILABLE]


@pytest.mark.parametrize("status", _NON_AVAILABLE_STATUSES)
def test_non_available_measure_never_serializes_a_number(status: MeasureStatus) -> None:
    measure: Measure[float] = Measure.unavailable(status, reason="sin ventana madura")

    body = to_cockpit_json_value(measure)

    assert body["status"] == status.value
    assert body["value"] is None
    assert body["reason"] == "sin ventana madura"
    assert "value" in body and not isinstance(body["value"], int | float)


@pytest.mark.parametrize("status", _NON_AVAILABLE_STATUSES)
def test_non_available_measure_of_money_never_serializes_a_number(status: MeasureStatus) -> None:
    measure: Measure[Money] = Measure.unavailable(status, reason="sin fuente de clientes")

    body = to_cockpit_json_value(measure)

    assert body["value"] is None


def test_available_measure_serializes_its_value_without_a_reason_key() -> None:
    measure = Measure.available(3.5)

    body = to_cockpit_json_value(measure)

    assert body == {"status": "available", "value": 3.5}
    assert "reason" not in body


def test_available_measure_of_money_renders_amount_as_decimal_string() -> None:
    measure = Measure.available(Money(Decimal("12.50"), "EUR"))

    body = to_cockpit_json_value(measure)

    assert body["value"] == {"amount": "12.50", "currency": "EUR"}
    assert isinstance(body["value"]["amount"], str)


def test_plain_money_renders_amount_as_decimal_string_never_float() -> None:
    money = Money(Decimal("1234.5"), "EUR")

    body = to_cockpit_json_value(money)

    assert body == {"amount": "1234.5", "currency": "EUR"}


def test_money_amount_keeps_exact_decimal_representation_no_float_rounding() -> None:
    # 0.1 + 0.2 en float da 0.30000000000000004 -- Decimal("0.30") no.
    money = Money(Decimal("0.10") + Decimal("0.20"), "EUR")

    body = to_cockpit_json_value(money)

    assert body["amount"] == "0.30"


@dataclass(frozen=True, slots=True)
class _NestedFixture:
    label: str
    spend: Money
    roi: Measure[float]


def test_nested_dataclasses_recurse_through_both_special_cases() -> None:
    fixture = _NestedFixture(
        label="campaign-1",
        spend=Money(Decimal("99.99"), "EUR"),
        roi=Measure.unavailable(MeasureStatus.NO_CUSTOMER_SOURCE, reason="CRM no conectado"),
    )

    body = to_cockpit_json_dict(fixture)

    assert body["spend"] == {"amount": "99.99", "currency": "EUR"}
    assert body["roi"] == {
        "status": "no_customer_source",
        "value": None,
        "reason": "CRM no conectado",
    }


def test_list_and_tuple_of_measures_recurse_too() -> None:
    values: tuple[Measure[int], ...] = (
        Measure.available(3),
        Measure.unavailable(MeasureStatus.LEARNING, reason="en aprendizaje"),
    )

    body = to_cockpit_json_value(values)

    assert body == [
        {"status": "available", "value": 3},
        {"status": "learning", "value": None, "reason": "en aprendizaje"},
    ]

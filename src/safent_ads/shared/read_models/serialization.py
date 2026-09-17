"""JSON compacto para las respuestas REST y MCP (contracts/mcp-tools.md:
"numeros, no prosa"). Recorre dataclasses/enums/`Decimal`/fechas y produce
solo tipos JSON nativos; nunca serializa un objeto de dominio de otro
contexto (los handlers ya reciben DTOs propios de `panel`/`mcp`). Antes de
esta extraccion vivia duplicado a proposito en ambos contextos hermanos de
N7 (simplification-audit.md #9); al ser una funcion pura de reflexion sin
logica de negocio, vivir en `shared` (N0) no viola plan.md §4."""

from __future__ import annotations

import dataclasses
from datetime import date, datetime
from decimal import Decimal
from enum import Enum
from typing import Any, cast

from safent_ads.shared.read_models.dto import Measure, Money


def to_json_dict(value: Any) -> dict[str, Any]:  # noqa: ANN401
    """Igual que `to_json_value`, tipado para el caso comun de un handler
    que devuelve un unico DTO como cuerpo JSON de nivel superior."""
    result = to_json_value(value)
    return cast(dict[str, Any], result)


def to_json_value(value: Any) -> Any:  # noqa: ANN401 - frontera de serializacion generica
    if dataclasses.is_dataclass(value) and not isinstance(value, type):
        return _dataclass_to_json(value)
    if isinstance(value, dict):
        return {key: to_json_value(item) for key, item in value.items()}
    if isinstance(value, list | tuple):
        return [to_json_value(item) for item in value]
    return _to_json_scalar(value)


def _dataclass_to_json(value: Any) -> dict[str, Any]:  # noqa: ANN401
    return {f.name: to_json_value(getattr(value, f.name)) for f in dataclasses.fields(value)}


def _to_json_scalar(value: Any) -> Any:  # noqa: ANN401
    if isinstance(value, Enum):
        return value.value
    if isinstance(value, Decimal):
        return float(value)
    if isinstance(value, datetime | date):
        return value.isoformat()
    return value


def to_cockpit_json_dict(value: Any) -> dict[str, Any]:  # noqa: ANN401
    """Como `to_json_dict`, para el cuerpo de nivel superior de
    `GET /api/v1/cockpit` (026, contracts/cockpit-read-model.md §2/§3)."""
    result = to_cockpit_json_value(value)
    return cast(dict[str, Any], result)


def to_cockpit_json_value(value: Any) -> Any:  # noqa: ANN401 - frontera de serializacion generica
    """Como `to_json_value`, salvo dos formas propias del cockpit que
    `to_json_value` no conoce (y no debe conocer -- el resto de la API
    sigue sirviendo dinero como `float`, cambiar `to_json_value` ahi
    romperia esos contratos ya publicados):

    - `Measure[T]`: la forma discriminada de cockpit-read-model.md §2 --
      `available` lleva `value` (sin `reason`); cualquier otro estado lleva
      `value: null` y `reason`. Nunca produce un numero fuera de esa forma.
    - `Money`: `amount` como cadena decimal (`str(Decimal)`), nunca `float`
      -- cockpit-read-model.md §2: "decimal como cadena, nunca float"."""
    if isinstance(value, Measure):
        return _measure_to_json(value)
    if isinstance(value, Money):
        return _money_to_json(value)
    if dataclasses.is_dataclass(value) and not isinstance(value, type):
        return {
            f.name: to_cockpit_json_value(getattr(value, f.name)) for f in dataclasses.fields(value)
        }
    if isinstance(value, dict):
        return {key: to_cockpit_json_value(item) for key, item in value.items()}
    if isinstance(value, list | tuple):
        return [to_cockpit_json_value(item) for item in value]
    return _to_json_scalar(value)


def _measure_to_json(measure: Measure[Any]) -> dict[str, Any]:
    if measure.value is not None:
        return {"status": measure.status.value, "value": to_cockpit_json_value(measure.value)}
    return {"status": measure.status.value, "value": None, "reason": measure.reason}


def _money_to_json(money: Money) -> dict[str, str]:
    return {"amount": str(money.amount), "currency": money.currency}

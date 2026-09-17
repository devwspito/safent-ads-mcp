"""JSON compacto para las respuestas MCP/REST de `optimization` (mismo
criterio que `economics.presentation.serialization` y
`shared.read_models.serialization` -- esta ultima ya consolidada para
`panel`/`mcp` -- duplicado a proposito: contextos hermanos en
presentacion, sin import cruzado, plan.md §4)."""

from __future__ import annotations

import dataclasses
from datetime import date, datetime
from decimal import Decimal
from enum import Enum
from typing import Any, cast


def to_json_dict(value: Any) -> dict[str, Any]:  # noqa: ANN401
    return cast(dict[str, Any], to_json_value(value))


def to_json_value(value: Any) -> Any:  # noqa: ANN401
    if dataclasses.is_dataclass(value) and not isinstance(value, type):
        return {f.name: to_json_value(getattr(value, f.name)) for f in dataclasses.fields(value)}
    if isinstance(value, dict):
        return {key: to_json_value(item) for key, item in value.items()}
    if isinstance(value, list | tuple):
        return [to_json_value(item) for item in value]
    return _to_json_scalar(value)


def _to_json_scalar(value: Any) -> Any:  # noqa: ANN401
    if isinstance(value, Enum):
        return value.value
    if isinstance(value, Decimal):
        return float(value)
    if isinstance(value, datetime | date):
        return value.isoformat()
    return value

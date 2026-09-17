"""Identificadores de `economics`. `ProductId` es generico a proposito
(regla del producto: 'product', no el nombre vertical de la oferta) y
opaco al identificador de `catalog` -- `economics/application` traduce en
el borde, la unica capa que conoce ambos vocabularios (patron de
`signals.domain.MetricWindow` frente a `metrics.domain.MetricWindow`)."""

from __future__ import annotations

import uuid
from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class ProductId:
    value: uuid.UUID

    @classmethod
    def parse(cls, raw: str) -> ProductId:
        return cls(uuid.UUID(raw))

    def __str__(self) -> str:
        return str(self.value)


@dataclass(frozen=True, slots=True)
class UnitEconomicsProfileId:
    value: uuid.UUID

    @classmethod
    def new(cls) -> UnitEconomicsProfileId:
        return cls(uuid.uuid4())

    def __str__(self) -> str:
        return str(self.value)

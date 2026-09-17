"""`PaymentPlan` (T131, `offering_economics.payment_plan`): si el dueno
cobra de una vez o a plazos. Persistido para uso futuro -- no cambia
`collection_rate` todavia (`BuildUnitEconomicsProfile` sigue fijandolo en
`Rate.one()`, profitability-engine.md §10.5 queda fuera de alcance de este
ticket, ver 0029_economics_inputs)."""

from __future__ import annotations

from enum import StrEnum


class PaymentPlan(StrEnum):
    NONE = "none"
    INSTALMENTS = "instalments"

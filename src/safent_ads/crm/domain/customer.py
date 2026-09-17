"""`Customer` (raiz de agregado, data-model.md §Customer, spec 027): el
valor de un negocio de servicios no esta en la primera venta, esta en el
ingreso recurrente neto de la identidad entera -- este agregado es quien
esa identidad ES, nunca lo que gasto ayer.

Invariante no negociable: prohibido el dato personal. La identidad es
`HashedIdentity` (sal por negocio); no existe campo para email, telefono
ni nombre, ni opcional (mismo criterio que `LeadAttribution`,
threat-model.md C-31)."""

from __future__ import annotations

import uuid
from dataclasses import dataclass, replace
from datetime import datetime
from enum import StrEnum

from safent_ads.crm.domain.hashed_identity import HashedIdentity
from safent_ads.crm.domain.lead_attribution import AttributionRung
from safent_ads.shared.ids import BusinessId, EntityRef


class CustomerState(StrEnum):
    """`lead -> customer (activo) -> churned`, con retorno a `activo` si
    vuelve a pagar (data-model.md §Customer)."""

    LEAD = "lead"
    ACTIVE = "active"
    CHURNED = "churned"


@dataclass(frozen=True, slots=True)
class CustomerId:
    value: uuid.UUID

    @classmethod
    def new(cls) -> CustomerId:
        return cls(uuid.uuid4())

    def __str__(self) -> str:
        return str(self.value)


@dataclass(frozen=True, kw_only=True, slots=True)
class Customer:
    customer_id: CustomerId
    business_id: BusinessId
    hashed_identity: HashedIdentity
    entity_ref: EntityRef | None
    attribution_rung: AttributionRung
    first_paid_conversion_at: datetime | None
    state: CustomerState
    currency: str
    first_seen_at: datetime
    last_seen_at: datetime
    source_connector_id: str | None = None

    def __post_init__(self) -> None:
        if self.hashed_identity.business_id != self.business_id:
            raise ValueError("hashed_identity pertenece a otro negocio")
        if self.last_seen_at < self.first_seen_at:
            raise ValueError("last_seen_at no puede ser anterior a first_seen_at")

    @classmethod
    def first_seen(
        cls,
        *,
        business_id: BusinessId,
        hashed_identity: HashedIdentity,
        entity_ref: EntityRef | None,
        attribution_rung: AttributionRung,
        currency: str,
        seen_at: datetime,
        source_connector_id: str | None = None,
    ) -> Customer:
        return cls(
            customer_id=CustomerId.new(),
            business_id=business_id,
            hashed_identity=hashed_identity,
            entity_ref=entity_ref,
            attribution_rung=attribution_rung,
            first_paid_conversion_at=None,
            state=CustomerState.LEAD,
            currency=currency,
            first_seen_at=seen_at,
            last_seen_at=seen_at,
            source_connector_id=source_connector_id,
        )

    def touch(self, *, seen_at: datetime, source_connector_id: str | None = None) -> Customer:
        """Re-vista por una entrega posterior: `first_seen_at` nunca se
        mueve, `last_seen_at` avanza si el nuevo dato es mas reciente."""
        if seen_at <= self.last_seen_at:
            return self
        return replace(
            self,
            last_seen_at=seen_at,
            source_connector_id=source_connector_id or self.source_connector_id,
        )

    def record_paid_event(self, *, occurred_at: datetime) -> Customer:
        """`first_paid_conversion_at` es el minimo `occurred_at` de sus
        `RevenueEvent` de tipo `first_payment`/`recurring_payment`; se
        deriva incrementalmente en cada hecho nuevo, nunca se reescribe
        hacia adelante (data-model.md §Customer invariante 2)."""
        first_paid = (
            occurred_at
            if self.first_paid_conversion_at is None
            else min(self.first_paid_conversion_at, occurred_at)
        )
        next_state = CustomerState.ACTIVE if self.state is not CustomerState.CHURNED else self.state
        return replace(
            self,
            first_paid_conversion_at=first_paid,
            state=next_state,
            last_seen_at=max(self.last_seen_at, occurred_at),
        )

    def reactivate(self, *, occurred_at: datetime) -> Customer:
        """Vuelve a pagar tras una baja: retorno a `activo` (data-model.md
        §Customer: 'con retorno a activo si vuelve a pagar')."""
        return replace(
            self,
            state=CustomerState.ACTIVE,
            last_seen_at=max(self.last_seen_at, occurred_at),
        )

    def record_churn(self, *, occurred_at: datetime) -> Customer:
        """`state = churned` no borra nada: la historia queda
        (data-model.md §Customer invariante 4)."""
        return replace(
            self, state=CustomerState.CHURNED, last_seen_at=max(self.last_seen_at, occurred_at)
        )

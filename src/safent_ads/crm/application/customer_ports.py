"""Puertos de `customers`/`revenue_events`/`crm_bridge_health` (spec 027,
data-model.md §Customer/§RevenueEvent/§CrmBridgeHealth). Adaptadores SQL en
`crm/infrastructure/`, dobles en memoria en `crm/testing/` (plan.md §4:
`application` depende de puertos, nunca de SQLAlchemy)."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Protocol

from safent_ads.crm.domain.bridge_health import CrmBridgeHealth
from safent_ads.crm.domain.customer import Customer, CustomerId
from safent_ads.crm.domain.identity_mapping import IdentityMapping
from safent_ads.crm.domain.revenue_event import RevenueEvent
from safent_ads.shared.ids import BusinessId


@dataclass(frozen=True, kw_only=True, slots=True)
class RevenueCohortStats:
    """Agregado puro sobre `revenue_events`: solo sumas y conteos, nunca
    una fila con un digest (FR-009 de spec 026, `ComputeCustomerValue`)."""

    cohort_size: int
    observed_contribution_minor: int
    currency: str | None


class CustomerRepository(Protocol):
    """`customers` (0031_customers): una fila por `(business_id,
    identity_digest)`."""

    async def find_by_identity(
        self, *, business_id: BusinessId, identity_digest: str
    ) -> Customer | None: ...

    async def upsert(self, customer: Customer) -> None: ...

    async def delete_for_identity(self, *, business_id: BusinessId, identity_digest: str) -> int:
        """Borra la fila de `customers` para este digest (A-2, `ForgetCustomer`).
        Devuelve el numero de filas borradas (0 o 1)."""
        ...

    async def count_active_in_window(
        self,
        *,
        business_id: BusinessId,
        entity_ref: str | None,
        window_start: datetime,
        window_end: datetime,
    ) -> int:
        """Clientes cuyo `first_paid_conversion_at` cae en la ventana --
        cockpit (spec 026 T007/T012, spec 027 T017). `entity_ref=None`
        cuenta todo el negocio."""
        ...


class RevenueEventRepository(Protocol):
    """`revenue_events` (0032_revenue_events): solo-anexable, idempotente
    por `(business_id, connector_id, source_event_id)`."""

    async def insert_if_new(
        self, event: RevenueEvent, *, business_id: BusinessId, connector_id: str
    ) -> bool:
        """La idempotencia es del esquema (UNIQUE), no del codigo. Devuelve
        `True` si el hecho era nuevo, `False` si ya existia (reenviar un
        lote entero es seguro por construccion)."""
        ...

    async def delete_for_customer(self, *, business_id: BusinessId, customer_id: CustomerId) -> int:
        """A-2: parte de `ForgetCustomer` -- borra todos los hechos de esa
        identidad dentro de la misma transaccion que `customers`."""
        ...

    async def cohort_stats(
        self, *, business_id: BusinessId, entity_ref: str | None
    ) -> RevenueCohortStats:
        """Suma/cuenta pura para `ComputeCustomerValue` y el cuadro de
        mando -- `entity_ref=None` agrega todo el negocio."""
        ...

    async def last_event_at(self, *, business_id: BusinessId, connector_id: str) -> datetime | None:
        """Salud del puente (A-3): cuando llego el ultimo hecho de este
        conector, para `RecordBridgeHealth`."""
        ...


class IdentityMappingRepository(Protocol):
    """`identity_mappings` (0031_customers): solo-anexable."""

    async def record(self, mapping: IdentityMapping) -> None: ...

    async def delete_for_identity(self, *, business_id: BusinessId, identity_digest: str) -> int:
        """A-2: filas de `identity_mappings` de esta identidad."""
        ...


class CrmBridgeHealthRepository(Protocol):
    """`crm_bridge_health` (0033_crm_bridge_health): una fila viva por
    `(business_id, connector_id)`."""

    async def upsert(self, health: CrmBridgeHealth) -> None: ...

    async def get_for_business(self, *, business_id: BusinessId) -> CrmBridgeHealth | None:
        """Cualquier puente configurado para este negocio. `None` cuando no
        hay ninguno -- el freeze gate NUNCA congela por AUSENCIA de puente
        (spec 027 T017: 'un puente sin configurar no congela nada')."""
        ...


class CustomerForgottenRecorder(Protocol):
    """Anota la supresion en `decision_log`, sin el identificador crudo
    (A-2) -- mismo patron que `brand.application.ports.
    BrandClaimsDecisionRecorder`."""

    async def record(
        self, *, business_id: BusinessId, customer_hash: str, rows_deleted: int
    ) -> None: ...

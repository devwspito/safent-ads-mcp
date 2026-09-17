"""Puertos de `economics` (plan.md §5 patron: casos de uso declaran sus
puertos; los adaptadores concretos viven en `infrastructure/`).

`platform` es un `str` (`"google"|"meta"`) y no `accounts.PlatformCode`
porque `economics` no depende de `accounts` en el grafo de contextos
(profitability-engine.md: 'economics (N2.5, sobre catalog/crm/metrics)')."""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from datetime import date, datetime
from decimal import Decimal
from typing import Protocol

from safent_ads.economics.domain.identifiers import ProductId
from safent_ads.economics.domain.lag_curve import LagCurve, LagObservation
from safent_ads.economics.domain.money import Money
from safent_ads.economics.domain.payment_plan import PaymentPlan
from safent_ads.economics.domain.platform_divergence import PlatformDivergence
from safent_ads.economics.domain.rate import Rate, Theta
from safent_ads.economics.domain.unit_economics import UnitEconomicsProfile
from safent_ads.shared.ids import BusinessId


class UnitEconomicsProfileRepository(Protocol):
    async def get_current(
        self, *, business_id: BusinessId, product_id: ProductId, as_of: date
    ) -> UnitEconomicsProfile | None: ...

    async def list_versions(
        self, *, business_id: BusinessId, product_id: ProductId
    ) -> list[UnitEconomicsProfile]: ...

    async def save(self, profile: UnitEconomicsProfile) -> None: ...


class LagObservationRepository(Protocol):
    """Fuente cruda para reconstruir la curva (leads con su edad y si ya
    convirtieron). El adaptador real traduce `crm.LeadAttribution` +
    `metrics` a esta forma (capa anticorrupcion, igual que `signals`)."""

    async def fetch_observations(
        self, *, business_id: BusinessId, product_id: ProductId, platform: str, as_of: date
    ) -> list[LagObservation]: ...


class LagCurveRepository(Protocol):
    """Curva materializada (recomputar Kaplan-Meier en cada lectura es
    caro): el `MaintenanceCycle` la recalcula periodicamente."""

    async def get_current(
        self, *, business_id: BusinessId, product_id: ProductId, platform: str
    ) -> LagCurve | None: ...

    async def save(
        self, *, business_id: BusinessId, product_id: ProductId, platform: str, curve: LagCurve
    ) -> None: ...


class PlatformDivergenceRepository(Protocol):
    async def get_latest(
        self, *, business_id: BusinessId, platform_account_id: str
    ) -> PlatformDivergence | None: ...

    async def get_previous(
        self, *, business_id: BusinessId, platform_account_id: str
    ) -> PlatformDivergence | None: ...

    async def save(
        self, *, business_id: BusinessId, platform_account_id: str, divergence: PlatformDivergence
    ) -> None: ...


class CalendarEventLookupPort(Protocol):
    """Puerto hacia `catalog` (permitido: 'economics sobre catalog/crm/
    metrics'): un lead se atribuye a un `calendar_event`, no directamente a
    un producto -- este puerto resuelve que eventos de calendario pertenecen
    a un `product_id`, la unica costura que `ComputeLagCurve`/
    `ComputePlatformDivergence` necesitan de `catalog`."""

    async def list_calendar_event_ids_for_product(
        self, *, business_id: BusinessId, product_id: ProductId
    ) -> Sequence[str]: ...


class OfferingPricePort(Protocol):
    """Puerto hacia `catalog`: precio de lista vigente de un producto,
    entrada de `BuildUnitEconomicsProfile` (profitability-engine.md §1:
    `net_revenue = list_price x ...`)."""

    async def get_list_price(
        self, *, business_id: BusinessId, product_id: ProductId
    ) -> Money | None: ...


@dataclass(frozen=True, kw_only=True, slots=True)
class MarginInputs:
    """Lo que el dueño aporta por producto (profitability-engine.md §10,
    T132) y no se puede inferir del CRM: IVA, coste variable de impartir,
    coste MENSUAL del equipo comercial (`BuildUnitEconomicsProfile` lo
    divide entre los cierres reales del CRM para `sales_cost_per_close`,
    §1: 'coste mensual ÷ cierres'), `theta` y horizonte de margen.
    `refund_rate_override`: si el dueño no lo aporta, cae al 10 % por
    defecto documentado (§10.4) -- no hay evento de reembolso en el
    esquema todavia para calcular un percentil real."""

    vat_rate: Rate
    delivery_cost: Money
    monthly_sales_team_cost: Money
    theta: Theta
    margin_horizon_days: int
    refund_rate_override: Rate | None = None


class MarginInputsPort(Protocol):
    """Puerto hacia `offering_economics` (T132, entrada del propietario
    desde el panel -- 0029_economics_inputs): `None` si el dueño todavia no
    ha rellenado esos numeros -- `BuildUnitEconomicsProfile` cae entonces a
    `provisional_from_price_only` (§1: 'el motor no propone ninguna subida
    de gasto')."""

    async def get_margin_inputs(
        self, *, business_id: BusinessId, product_id: ProductId
    ) -> MarginInputs | None: ...


@dataclass(frozen=True, kw_only=True, slots=True)
class OfferingEconomicsInput:
    """Forma cruda de `PUT /offerings/{id}/economics` (contracts/rest-api.md
    §Economia unitaria): porcentajes en [0,100] y dinero en unidades
    menores, tal cual los teclea el dueño -- la traduccion a `Rate`/`Money`
    de dominio vive en `SqlMarginInputsPort`, no aqui (esta forma es la que
    persiste `offering_economics` 1:1, sin theta/margin_horizon_days:
    Assumptions de profitability-engine.md los fija por defecto)."""

    vat_rate_pct: Decimal
    delivery_cost_minor: int
    sales_cost_minor: int
    refund_rate_pct: Decimal | None
    payment_plan: PaymentPlan
    currency: str


@dataclass(frozen=True, kw_only=True, slots=True)
class OfferingEconomics(OfferingEconomicsInput):
    updated_at: datetime


@dataclass(frozen=True, kw_only=True, slots=True)
class OfferingSummary:
    """Una fila de `GET /offerings`: la oferta (de `catalog`, leida via la
    misma costura que `OfferingPricePort`) con su `OfferingEconomics` si el
    dueño ya la relleno, o `None` -- el panel pinta "provisional" a partir
    de esto sin adivinar nada."""

    # `str`, no `ProductId`: `presentation.serialization.to_json_value`
    # recorre dataclasses generico (sin caso especial por tipo) -- un
    # `ProductId` ahi saldria como `{"value": "<uuid>"}` en vez de
    # `"<uuid>"` (mismo criterio que `dto.UnitEconomicsView.product_id`).
    offering_id: str
    code: str
    title: str
    is_active: bool
    list_price: Money | None
    economics: OfferingEconomics | None


class OfferingEconomicsRepository(Protocol):
    """`offering_economics` (0029_economics_inputs) desde el lado de
    escritura/listado del panel -- `MarginInputsPort` de arriba es el lado
    de lectura que consume `BuildUnitEconomicsProfile`, misma tabla, dos
    puertos por ISP (uno devuelve `MarginInputs` de dominio, este devuelve
    la forma cruda que el panel edita)."""

    async def list_offerings_with_economics(
        self, *, business_id: BusinessId
    ) -> list[OfferingSummary]: ...

    async def offering_exists(
        self, *, business_id: BusinessId, offering_id: ProductId
    ) -> bool: ...

    async def upsert(
        self, *, business_id: BusinessId, offering_id: ProductId, economics: OfferingEconomicsInput
    ) -> OfferingEconomics: ...

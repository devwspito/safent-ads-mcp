"""Dobles en memoria de los puertos de `economics` (application/ports.py),
usados por los tests de los casos de uso y por `mcp`/`panel` hasta que la
integracion cablee los adaptadores SQL reales."""

from __future__ import annotations

from collections.abc import Sequence
from datetime import UTC, date, datetime

from safent_ads.economics.application.ports import (
    MarginInputs,
    OfferingEconomics,
    OfferingEconomicsInput,
    OfferingSummary,
)
from safent_ads.economics.domain.identifiers import ProductId
from safent_ads.economics.domain.lag_curve import LagCurve, LagObservation
from safent_ads.economics.domain.money import Money
from safent_ads.economics.domain.platform_divergence import PlatformDivergence
from safent_ads.economics.domain.unit_economics import UnitEconomicsProfile
from safent_ads.shared.ids import BusinessId

_PREVIOUS_MIN_LENGTH = 2


class InMemoryUnitEconomicsProfileRepository:
    def __init__(self) -> None:
        self.saved: list[UnitEconomicsProfile] = []

    async def get_current(
        self, *, business_id: BusinessId, product_id: ProductId, as_of: date
    ) -> UnitEconomicsProfile | None:
        candidates = [
            p
            for p in self.saved
            if p.business_id == business_id
            and p.product_id == product_id
            and p.effective_from <= as_of
        ]
        if not candidates:
            return None
        return max(candidates, key=lambda p: p.effective_from)

    async def list_versions(
        self, *, business_id: BusinessId, product_id: ProductId
    ) -> list[UnitEconomicsProfile]:
        return sorted(
            (
                p
                for p in self.saved
                if p.business_id == business_id and p.product_id == product_id
            ),
            key=lambda p: p.effective_from,
        )

    async def save(self, profile: UnitEconomicsProfile) -> None:
        self.saved.append(profile)


class InMemoryLagObservationRepository:
    def __init__(self) -> None:
        self._by_key: dict[tuple[str, str, str], list[LagObservation]] = {}

    def seed(
        self,
        *,
        business_id: BusinessId,
        product_id: ProductId,
        platform: str,
        observations: list[LagObservation],
    ) -> None:
        self._by_key[(str(business_id), str(product_id), platform)] = observations

    async def fetch_observations(
        self,
        *,
        business_id: BusinessId,
        product_id: ProductId,
        platform: str,
        as_of: date,  # noqa: ARG002 - forma exacta del puerto
    ) -> list[LagObservation]:
        return self._by_key.get((str(business_id), str(product_id), platform), [])


class InMemoryLagCurveRepository:
    def __init__(self) -> None:
        self._by_key: dict[tuple[str, str, str], LagCurve] = {}

    async def get_current(
        self, *, business_id: BusinessId, product_id: ProductId, platform: str
    ) -> LagCurve | None:
        return self._by_key.get((str(business_id), str(product_id), platform))

    async def save(
        self, *, business_id: BusinessId, product_id: ProductId, platform: str, curve: LagCurve
    ) -> None:
        self._by_key[(str(business_id), str(product_id), platform)] = curve


class InMemoryPlatformDivergenceRepository:
    def __init__(self) -> None:
        self._history: dict[tuple[str, str], list[PlatformDivergence]] = {}

    def seed(
        self,
        *,
        business_id: BusinessId,
        platform_account_id: str,
        snapshots: list[PlatformDivergence],
    ) -> None:
        """`snapshots` en orden cronologico ascendente: el ultimo es `latest`."""
        self._history[(str(business_id), platform_account_id)] = snapshots

    async def get_latest(
        self, *, business_id: BusinessId, platform_account_id: str
    ) -> PlatformDivergence | None:
        history = self._history.get((str(business_id), platform_account_id), [])
        return history[-1] if history else None

    async def get_previous(
        self, *, business_id: BusinessId, platform_account_id: str
    ) -> PlatformDivergence | None:
        history = self._history.get((str(business_id), platform_account_id), [])
        return history[-2] if len(history) >= _PREVIOUS_MIN_LENGTH else None

    async def save(
        self, *, business_id: BusinessId, platform_account_id: str, divergence: PlatformDivergence
    ) -> None:
        key = (str(business_id), platform_account_id)
        self._history.setdefault(key, []).append(divergence)


class InMemoryCalendarEventLookupPort:
    def __init__(self) -> None:
        self._by_product: dict[tuple[str, str], list[str]] = {}

    def seed(
        self, *, business_id: BusinessId, product_id: ProductId, calendar_event_ids: list[str]
    ) -> None:
        self._by_product[(str(business_id), str(product_id))] = calendar_event_ids

    async def list_calendar_event_ids_for_product(
        self, *, business_id: BusinessId, product_id: ProductId
    ) -> Sequence[str]:
        return self._by_product.get((str(business_id), str(product_id)), [])


class InMemoryOfferingPricePort:
    def __init__(self) -> None:
        self._by_product: dict[tuple[str, str], Money] = {}

    def seed(self, *, business_id: BusinessId, product_id: ProductId, list_price: Money) -> None:
        self._by_product[(str(business_id), str(product_id))] = list_price

    async def get_list_price(
        self, *, business_id: BusinessId, product_id: ProductId
    ) -> Money | None:
        return self._by_product.get((str(business_id), str(product_id)))


class InMemoryMarginInputsPort:
    def __init__(self) -> None:
        self._by_product: dict[tuple[str, str], MarginInputs] = {}

    def seed(
        self, *, business_id: BusinessId, product_id: ProductId, margin_inputs: MarginInputs
    ) -> None:
        self._by_product[(str(business_id), str(product_id))] = margin_inputs

    async def get_margin_inputs(
        self, *, business_id: BusinessId, product_id: ProductId
    ) -> MarginInputs | None:
        return self._by_product.get((str(business_id), str(product_id)))


class InMemoryOfferingEconomicsRepository:
    """Doble de `OfferingEconomicsRepository` (T131/T132): `offerings` no
    existe en memoria aqui (economics.testing no reimplementa `catalog`),
    asi que `seed_offering` registra directamente la fila que `GET
    /offerings` vería -- `upsert()` solo puede escribir sobre un
    `offering_id` ya sembrado, igual que la FK compuesta real."""

    def __init__(self) -> None:
        self._offerings: dict[tuple[str, str], OfferingSummary] = {}

    def seed_offering(
        self, *, business_id: BusinessId, offering: OfferingSummary
    ) -> None:
        self._offerings[(str(business_id), offering.offering_id)] = offering

    async def list_offerings_with_economics(
        self, *, business_id: BusinessId
    ) -> list[OfferingSummary]:
        return [
            offering
            for (owner, _), offering in self._offerings.items()
            if owner == str(business_id)
        ]

    async def offering_exists(self, *, business_id: BusinessId, offering_id: ProductId) -> bool:
        return (str(business_id), str(offering_id)) in self._offerings

    async def upsert(
        self,
        *,
        business_id: BusinessId,
        offering_id: ProductId,
        economics: OfferingEconomicsInput,
    ) -> OfferingEconomics:
        key = (str(business_id), str(offering_id))
        current = self._offerings[key]
        saved = OfferingEconomics(
            vat_rate_pct=economics.vat_rate_pct,
            delivery_cost_minor=economics.delivery_cost_minor,
            sales_cost_minor=economics.sales_cost_minor,
            refund_rate_pct=economics.refund_rate_pct,
            payment_plan=economics.payment_plan,
            currency=economics.currency,
            updated_at=datetime.now(UTC),
        )
        self._offerings[key] = OfferingSummary(
            offering_id=current.offering_id,
            code=current.code,
            title=current.title,
            is_active=current.is_active,
            list_price=current.list_price,
            economics=saved,
        )
        return saved

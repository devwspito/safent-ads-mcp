"""`BuildUnitEconomicsProfile` (T156, profitability-engine.md §1): llena
`unit_economics_profiles` a partir de lo que aporta el dueno
(`MarginInputsPort`, T132) y lo que se infiere del CRM (`discount_rate`,
`cvr_lead_to_business_conversion`, `sales_cost_per_close`). Sin entradas
del dueno o sin conversiones suficientes, el perfil nace `provisional`
(`UnitEconomicsProfile.provisional_from_price_only`): el motor no propone
ninguna subida de gasto sobre un perfil asi (regla de `optimization`, no de
este caso de uso)."""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from decimal import Decimal

from safent_ads.crm.application.ports import LeadAttributionRepository
from safent_ads.crm.domain.conversion_kind import ConversionKind
from safent_ads.crm.domain.lead_attribution import LeadAttribution
from safent_ads.economics.application.errors import OfferingPriceMissingError
from safent_ads.economics.application.ports import (
    CalendarEventLookupPort,
    MarginInputs,
    MarginInputsPort,
    OfferingPricePort,
    UnitEconomicsProfileRepository,
)
from safent_ads.economics.domain.identifiers import ProductId, UnitEconomicsProfileId
from safent_ads.economics.domain.money import Money
from safent_ads.economics.domain.rate import Rate
from safent_ads.economics.domain.unit_economics import UnitEconomicsProfile, next_version
from safent_ads.shared.clock import Clock
from safent_ads.shared.ids import BusinessId

# profitability-engine.md §10.4: "si falta [devolucion/impago], percentil
# 75 o 10%". Sin un evento de reembolso modelado en el esquema (hueco
# preexistente, no de este ticket) no hay percentil que calcular contra
# datos reales -- se aplica siempre el 10% documentado hasta que exista esa
# fuente.
_DEFAULT_REFUND_RATE_FALLBACK = Rate.of("0.10")


@dataclass(frozen=True, kw_only=True, slots=True)
class _CrmInferredRates:
    discount_rate: Rate
    cvr_lead_to_business_conversion: Rate
    sales_cost_per_close: Money


class BuildUnitEconomicsProfile:
    def __init__(
        self,
        *,
        profiles: UnitEconomicsProfileRepository,
        margin_inputs: MarginInputsPort,
        offering_prices: OfferingPricePort,
        calendar_events: CalendarEventLookupPort,
        lead_attributions: LeadAttributionRepository,
        clock: Clock,
    ) -> None:
        self._profiles = profiles
        self._margin_inputs = margin_inputs
        self._offering_prices = offering_prices
        self._calendar_events = calendar_events
        self._lead_attributions = lead_attributions
        self._clock = clock

    async def execute(
        self, *, business_id: BusinessId, product_id: ProductId
    ) -> UnitEconomicsProfile:
        effective_from = self._clock.now().date()
        existing = await self._profiles.get_current(
            business_id=business_id, product_id=product_id, as_of=effective_from
        )
        if existing is not None and existing.effective_from == effective_from:
            return existing  # idempotente: ya se construyo la version de hoy

        list_price = await self._offering_prices.get_list_price(
            business_id=business_id, product_id=product_id
        )
        if list_price is None:
            raise OfferingPriceMissingError(str(product_id))

        margin_inputs = await self._margin_inputs.get_margin_inputs(
            business_id=business_id, product_id=product_id
        )

        if margin_inputs is None:
            profile = UnitEconomicsProfile.provisional_from_price_only(
                profile_id=UnitEconomicsProfileId.new(),
                business_id=business_id,
                product_id=product_id,
                list_price=list_price,
                effective_from=effective_from,
            )
        else:
            inferred = await self._infer_from_crm(
                business_id=business_id,
                product_id=product_id,
                list_price=list_price,
                margin_inputs=margin_inputs,
            )
            if inferred is None:
                profile = UnitEconomicsProfile.provisional_from_price_only(
                    profile_id=UnitEconomicsProfileId.new(),
                    business_id=business_id,
                    product_id=product_id,
                    list_price=list_price,
                    effective_from=effective_from,
                    theta=margin_inputs.theta,
                    margin_horizon_days=margin_inputs.margin_horizon_days,
                )
            else:
                versions = await self._profiles.list_versions(
                    business_id=business_id, product_id=product_id
                )
                profile = UnitEconomicsProfile.create(
                    profile_id=UnitEconomicsProfileId.new(),
                    business_id=business_id,
                    product_id=product_id,
                    version=next_version(versions, effective_from),
                    effective_from=effective_from,
                    list_price=list_price,
                    vat_rate=margin_inputs.vat_rate,
                    discount_rate=inferred.discount_rate,
                    refund_rate=margin_inputs.refund_rate_override or _DEFAULT_REFUND_RATE_FALLBACK,
                    delivery_cost=margin_inputs.delivery_cost,
                    sales_cost_per_close=inferred.sales_cost_per_close,
                    collection_rate=Rate.one(),
                    cvr_lead_to_business_conversion=inferred.cvr_lead_to_business_conversion,
                    theta=margin_inputs.theta,
                    margin_horizon_days=margin_inputs.margin_horizon_days,
                )

        await self._profiles.save(profile)
        return profile

    async def _infer_from_crm(
        self,
        *,
        business_id: BusinessId,
        product_id: ProductId,
        list_price: Money,
        margin_inputs: MarginInputs,
    ) -> _CrmInferredRates | None:
        calendar_event_ids = await self._calendar_events.list_calendar_event_ids_for_product(
            business_id=business_id, product_id=product_id
        )
        if not calendar_event_ids:
            return None
        attributions = await self._lead_attributions.find_for_calendar_events(
            business_id=business_id, calendar_event_ids=calendar_event_ids
        )
        return _infer_rates(attributions, list_price=list_price, margin_inputs=margin_inputs)


def _infer_rates(
    attributions: Sequence[LeadAttribution], *, list_price: Money, margin_inputs: MarginInputs
) -> _CrmInferredRates | None:
    leads = sum(1 for a in attributions if a.conversion_kind is ConversionKind.LEAD)
    conversions = [
        a for a in attributions if a.conversion_kind is ConversionKind.BUSINESS_CONVERSION
    ]
    if leads == 0 or not conversions:
        return None
    cvr = Rate.of(min(Decimal(len(conversions)) / Decimal(leads), Decimal("1")))
    discount_rate = _infer_discount_rate(conversions, list_price=list_price)
    months_spanned = _distinct_months_spanned(conversions)
    closes_per_month = Decimal(len(conversions)) / Decimal(months_spanned)
    sales_cost_per_close = margin_inputs.monthly_sales_team_cost.scaled_by(
        Decimal("1") / closes_per_month
    )
    return _CrmInferredRates(
        discount_rate=discount_rate,
        cvr_lead_to_business_conversion=cvr,
        sales_cost_per_close=sales_cost_per_close,
    )


def _infer_discount_rate(conversions: Sequence[LeadAttribution], *, list_price: Money) -> Rate:
    if list_price.amount <= 0:
        return Rate.zero()
    total_collected = sum((a.value_minor for a in conversions), start=0)
    avg_collected = Decimal(total_collected) / Decimal(len(conversions)) / Decimal(100)
    ratio = min(avg_collected / list_price.amount, Decimal("1"))
    return Rate(max(Decimal("1") - ratio, Decimal("0")))


def _distinct_months_spanned(conversions: Sequence[LeadAttribution]) -> int:
    months = {(a.occurred_at.year, a.occurred_at.month) for a in conversions}
    return max(len(months), 1)

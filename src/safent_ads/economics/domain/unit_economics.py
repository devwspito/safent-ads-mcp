"""`UnitEconomicsProfile` — agregado raiz de `economics` (profitability-
engine.md §1, data-model.md handoff `0016_economics`).

```
net_revenue  = list_price x (1 - discount_rate) / (1 + vat_rate)
collected(H) = net_revenue x collection_rate(H)
CM           = collected(H) x (1 - refund_rate) - delivery_cost - sales_cost_per_close
target_cpe   = CM x (1 - theta)
target_cpl   = target_cpe x cvr_lead_to_business_conversion
target_roas  = net_revenue / target_cpe
```

Versionado solo-anexable: cambiar el precio crea una version nueva
(`effective_from` posterior), nunca reescribe la vigente (NFR-7). Sin datos
suficientes, `contribution_margin_override` fija `CM = price x 0,60` y el
perfil nace `status = PROVISIONAL`: el motor no propone ninguna subida de
gasto sobre un perfil provisional (regla que aplica `optimization`, no esta
clase -- este agregado solo declara el hecho)."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from decimal import ROUND_HALF_UP, Decimal
from enum import StrEnum

from safent_ads.economics.domain.errors import (
    MarginHorizonTooShortError,
    NonPositiveTargetCpeError,
    ProfileVersionOverlapError,
)
from safent_ads.economics.domain.identifiers import ProductId, UnitEconomicsProfileId
from safent_ads.economics.domain.money import Money
from safent_ads.economics.domain.rate import Rate, Theta
from safent_ads.shared.ids import BusinessId

_CENTS = Decimal("0.01")
_PROVISIONAL_CM_FACTOR = Decimal("0.60")


class ProfileStatus(StrEnum):
    PROVISIONAL = "provisional"
    CONFIRMED = "confirmed"


def _round_money(amount: Decimal, currency: str) -> Money:
    return Money(amount.quantize(_CENTS, rounding=ROUND_HALF_UP), currency)


@dataclass(frozen=True, kw_only=True, slots=True)
class UnitEconomicsProfile:
    profile_id: UnitEconomicsProfileId
    business_id: BusinessId
    product_id: ProductId
    version: int
    effective_from: date
    status: ProfileStatus
    list_price: Money
    vat_rate: Rate
    discount_rate: Rate
    refund_rate: Rate
    delivery_cost: Money
    sales_cost_per_close: Money
    collection_rate: Rate
    cvr_lead_to_business_conversion: Rate
    theta: Theta
    margin_horizon_days: int
    contribution_margin_override: Money | None = None

    def __post_init__(self) -> None:
        if self.version < 1:
            raise ValueError(f"version debe ser >= 1: {self.version}")
        if self.margin_horizon_days < 0:
            raise ValueError(f"margin_horizon_days negativo: {self.margin_horizon_days}")
        if self.delivery_cost.amount < 0 or self.sales_cost_per_close.amount < 0:
            raise ValueError("delivery_cost y sales_cost_per_close deben ser >= 0")
        net_revenue = self.net_revenue()
        target_cpe = self.target_cost_per_conversion()
        if target_cpe.amount <= 0 or target_cpe.amount >= net_revenue.amount:
            raise NonPositiveTargetCpeError(
                f"target_cpe {target_cpe.amount} fuera de (0, {net_revenue.amount})"
            )

    # ------------------------------------------------------------------
    # Formulas (profitability-engine.md §1)
    # ------------------------------------------------------------------

    def net_revenue(self) -> Money:
        gross = self.list_price.amount * self.discount_rate.complement.value
        net = gross / (Decimal("1") + self.vat_rate.value)
        return _round_money(net, self.list_price.currency)

    def collected(self) -> Money:
        collected = self.net_revenue().amount * self.collection_rate.value
        return _round_money(collected, self.list_price.currency)

    def contribution_margin(self) -> Money:
        if self.contribution_margin_override is not None:
            return self.contribution_margin_override
        collected_after_refund = self.collected().amount * self.refund_rate.complement.value
        cm = collected_after_refund - self.delivery_cost.amount - self.sales_cost_per_close.amount
        return _round_money(cm, self.list_price.currency)

    def target_cost_per_conversion(self) -> Money:
        target_cpe = self.contribution_margin().amount * self.theta.complement.value
        return _round_money(target_cpe, self.list_price.currency)

    def target_cost_per_lead(self) -> Money:
        target_cpe = self.target_cost_per_conversion().amount
        target_cpl = target_cpe * self.cvr_lead_to_business_conversion.value
        return _round_money(target_cpl, self.list_price.currency)

    def target_roas(self) -> float:
        target_cpe = self.target_cost_per_conversion().amount
        if target_cpe == 0:
            return 0.0
        return float(self.net_revenue().amount / target_cpe)

    def assert_horizon_covers_lag(self, median_lag_days: int) -> None:
        """Invariante cruzada con `LagCurve` (profitability-engine.md §1:
        `margin_horizon_days >= median_lag_days`). No entra en
        `__post_init__` porque `LagCurve` es otro agregado; la orquesta la
        capa de aplicacion tras cargar ambos."""
        if self.margin_horizon_days < median_lag_days:
            raise MarginHorizonTooShortError(
                f"margin_horizon_days={self.margin_horizon_days} < "
                f"median_lag_days={median_lag_days}"
            )

    @classmethod
    def create(
        cls,
        *,
        profile_id: UnitEconomicsProfileId,
        business_id: BusinessId,
        product_id: ProductId,
        version: int,
        effective_from: date,
        list_price: Money,
        vat_rate: Rate,
        discount_rate: Rate,
        refund_rate: Rate,
        delivery_cost: Money,
        sales_cost_per_close: Money,
        collection_rate: Rate,
        cvr_lead_to_business_conversion: Rate,
        theta: Theta,
        margin_horizon_days: int,
    ) -> UnitEconomicsProfile:
        return cls(
            profile_id=profile_id,
            business_id=business_id,
            product_id=product_id,
            version=version,
            effective_from=effective_from,
            status=ProfileStatus.CONFIRMED,
            list_price=list_price,
            vat_rate=vat_rate,
            discount_rate=discount_rate,
            refund_rate=refund_rate,
            delivery_cost=delivery_cost,
            sales_cost_per_close=sales_cost_per_close,
            collection_rate=collection_rate,
            cvr_lead_to_business_conversion=cvr_lead_to_business_conversion,
            theta=theta,
            margin_horizon_days=margin_horizon_days,
        )

    @classmethod
    def provisional_from_price_only(
        cls,
        *,
        profile_id: UnitEconomicsProfileId,
        business_id: BusinessId,
        product_id: ProductId,
        list_price: Money,
        effective_from: date,
        theta: Theta | None = None,
        margin_horizon_days: int = 120,
    ) -> UnitEconomicsProfile:
        """Sin datos del CRM (profitability-engine.md §1: 'Sin datos: CM =
        price x 0,60 marcado provisional'). `theta` sigue siendo
        configurable porque no depende del CRM."""
        cm_override = list_price.scaled_by(_PROVISIONAL_CM_FACTOR)
        return cls(
            profile_id=profile_id,
            business_id=business_id,
            product_id=product_id,
            version=1,
            effective_from=effective_from,
            status=ProfileStatus.PROVISIONAL,
            list_price=list_price,
            vat_rate=Rate.zero(),
            discount_rate=Rate.zero(),
            refund_rate=Rate.zero(),
            delivery_cost=Money.zero(list_price.currency),
            sales_cost_per_close=Money.zero(list_price.currency),
            collection_rate=Rate.one(),
            cvr_lead_to_business_conversion=Rate.one(),
            theta=theta or Theta.default(),
            margin_horizon_days=margin_horizon_days,
            contribution_margin_override=cm_override,
        )


def next_version(existing: list[UnitEconomicsProfile], candidate_effective_from: date) -> int:
    """Version siguiente para un producto, o rechaza si `candidate_effective_from`
    no es estrictamente posterior a toda version existente (profitability-
    engine.md §1: 'versiones sin solapamiento')."""
    if not existing:
        return 1
    latest = max(existing, key=lambda p: p.effective_from)
    if candidate_effective_from <= latest.effective_from:
        raise ProfileVersionOverlapError(
            f"nueva version {candidate_effective_from} no es posterior a la vigente "
            f"{latest.effective_from}"
        )
    return latest.version + 1

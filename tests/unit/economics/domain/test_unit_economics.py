"""`UnitEconomicsProfile` (profitability-engine.md §1). El ejemplo de
Secundaria Matematicas es el oraculo: `net_revenue=1104`, `collected=1015.68`,
`CM=724.74`, `target_cpe=471.08`, `target_cpl=21.20` (redondeo a centimo)."""

from __future__ import annotations

import uuid
from datetime import date
from decimal import Decimal

import pytest

from safent_ads.economics.domain.errors import (
    NonPositiveTargetCpeError,
    ProfileVersionOverlapError,
    RateOutOfRangeError,
    ThetaBelowFloorError,
)
from safent_ads.economics.domain.identifiers import ProductId, UnitEconomicsProfileId
from safent_ads.economics.domain.money import Money
from safent_ads.economics.domain.rate import Rate, Theta
from safent_ads.economics.domain.unit_economics import (
    ProfileStatus,
    UnitEconomicsProfile,
    next_version,
)
from safent_ads.shared.ids import BusinessId

_BUSINESS_ID = BusinessId.new()
_PRODUCT_ID = ProductId(uuid.uuid4())


def _secundaria_matematicas() -> UnitEconomicsProfile:
    return UnitEconomicsProfile.create(
        profile_id=UnitEconomicsProfileId.new(),
        business_id=_BUSINESS_ID,
        product_id=_PRODUCT_ID,
        version=1,
        effective_from=date(2026, 1, 1),
        list_price=Money.of("1200"),
        vat_rate=Rate.zero(),  # exento art. 20.1.9 LIVA
        discount_rate=Rate.of("0.08"),
        refund_rate=Rate.of("0.06"),
        delivery_cost=Money.of("90"),
        sales_cost_per_close=Money.of("140"),
        collection_rate=Rate.of("0.92"),
        cvr_lead_to_business_conversion=Rate.of("0.045"),
        theta=Theta(Decimal("0.35")),
        margin_horizon_days=90,
    )


class TestWorkedExample:
    """profitability-engine.md §1, el ejemplo completo de Secundaria Matematicas."""

    def test_net_revenue(self) -> None:
        assert _secundaria_matematicas().net_revenue() == Money.of("1104.00")

    def test_collected(self) -> None:
        assert _secundaria_matematicas().collected() == Money.of("1015.68")

    def test_contribution_margin(self) -> None:
        assert _secundaria_matematicas().contribution_margin() == Money.of("724.74")

    def test_target_cost_per_conversion(self) -> None:
        assert _secundaria_matematicas().target_cost_per_conversion() == Money.of("471.08")

    def test_target_cost_per_lead(self) -> None:
        assert _secundaria_matematicas().target_cost_per_lead() == Money.of("21.20")

    def test_target_roas_positive(self) -> None:
        assert _secundaria_matematicas().target_roas() == pytest.approx(1104 / 471.08, rel=1e-3)


class TestGoldenCasesVat:
    def test_vat_21_pct_no_payment_plan(self) -> None:
        """121 € con IVA 21% da net_revenue exacto de 100 € -- caso limpio
        sin plan de pago (cobro integro) ni descuento."""
        profile = UnitEconomicsProfile.create(
            profile_id=UnitEconomicsProfileId.new(),
            business_id=_BUSINESS_ID,
            product_id=_PRODUCT_ID,
            version=1,
            effective_from=date(2026, 1, 1),
            list_price=Money.of("121"),
            vat_rate=Rate.of("0.21"),
            discount_rate=Rate.zero(),
            refund_rate=Rate.zero(),
            delivery_cost=Money.zero(),
            sales_cost_per_close=Money.zero(),
            collection_rate=Rate.one(),
            cvr_lead_to_business_conversion=Rate.of("0.10"),
            theta=Theta(Decimal("0.20")),  # suelo
            margin_horizon_days=30,
        )
        assert profile.net_revenue() == Money.of("100.00")
        assert profile.contribution_margin() == Money.of("100.00")
        assert profile.target_cost_per_conversion() == Money.of("80.00")
        assert profile.target_cost_per_lead() == Money.of("8.00")

    def test_vat_exempt_matches_worked_example(self) -> None:
        assert _secundaria_matematicas().net_revenue() == Money.of("1104.00")


class TestInvariants:
    def test_rate_out_of_range_rejected(self) -> None:
        with pytest.raises(RateOutOfRangeError):
            Rate.of("1.5")

    def test_theta_below_floor_rejected(self) -> None:
        with pytest.raises(ThetaBelowFloorError):
            Theta(Decimal("0.10"))

    def test_target_cpe_must_be_below_net_revenue(self) -> None:
        with pytest.raises(NonPositiveTargetCpeError):
            UnitEconomicsProfile.create(
                profile_id=UnitEconomicsProfileId.new(),
                business_id=_BUSINESS_ID,
                product_id=_PRODUCT_ID,
                version=1,
                effective_from=date(2026, 1, 1),
                list_price=Money.of("100"),
                vat_rate=Rate.zero(),
                discount_rate=Rate.zero(),
                refund_rate=Rate.zero(),
                delivery_cost=Money.of("200"),  # devora todo el margen
                sales_cost_per_close=Money.zero(),
                collection_rate=Rate.one(),
                cvr_lead_to_business_conversion=Rate.of("0.10"),
                theta=Theta(Decimal("0.20")),
                margin_horizon_days=30,
            )

    def test_margin_horizon_must_cover_median_lag(self) -> None:
        profile = _secundaria_matematicas()
        profile.assert_horizon_covers_lag(60)  # 90 >= 60, no lanza
        with pytest.raises(Exception, match="margin_horizon_days"):
            profile.assert_horizon_covers_lag(120)


class TestProvisionalProfile:
    def test_provisional_cm_is_60_pct_of_price(self) -> None:
        profile = UnitEconomicsProfile.provisional_from_price_only(
            profile_id=UnitEconomicsProfileId.new(),
            business_id=_BUSINESS_ID,
            product_id=_PRODUCT_ID,
            list_price=Money.of("1000"),
            effective_from=date(2026, 1, 1),
        )
        assert profile.status is ProfileStatus.PROVISIONAL
        assert profile.contribution_margin() == Money.of("600.00")


class TestMonotonicity:
    """Property test manual (sin hypothesis en el pyproject): `target_cpe`
    decrece cuando sube `refund_rate` o `theta` (profitability-engine.md §1)."""

    def _profile_with(self, *, refund_rate: str, theta: str) -> UnitEconomicsProfile:
        return UnitEconomicsProfile.create(
            profile_id=UnitEconomicsProfileId.new(),
            business_id=_BUSINESS_ID,
            product_id=_PRODUCT_ID,
            version=1,
            effective_from=date(2026, 1, 1),
            list_price=Money.of("1000"),
            vat_rate=Rate.zero(),
            discount_rate=Rate.zero(),
            refund_rate=Rate.of(refund_rate),
            delivery_cost=Money.zero(),
            sales_cost_per_close=Money.zero(),
            collection_rate=Rate.one(),
            cvr_lead_to_business_conversion=Rate.of("0.10"),
            theta=Theta(Decimal(theta)),
            margin_horizon_days=30,
        )

    def test_target_cpe_decreases_with_refund_rate(self) -> None:
        values = [
            self._profile_with(refund_rate=str(r), theta="0.35").target_cost_per_conversion().amount
            for r in ("0.00", "0.10", "0.20", "0.30")
        ]
        assert values == sorted(values, reverse=True)

    def test_target_cpe_decreases_with_theta(self) -> None:
        values = [
            self._profile_with(refund_rate="0.05", theta=t).target_cost_per_conversion().amount
            for t in ("0.20", "0.35", "0.50", "0.70")
        ]
        assert values == sorted(values, reverse=True)


class TestVersioning:
    def test_first_version_is_one(self) -> None:
        assert next_version([], date(2026, 1, 1)) == 1

    def test_next_version_after_later_effective_from(self) -> None:
        existing = [_secundaria_matematicas()]
        assert next_version(existing, date(2026, 6, 1)) == 2

    def test_overlapping_effective_from_rejected(self) -> None:
        existing = [_secundaria_matematicas()]
        with pytest.raises(ProfileVersionOverlapError):
            next_version(existing, date(2025, 12, 1))

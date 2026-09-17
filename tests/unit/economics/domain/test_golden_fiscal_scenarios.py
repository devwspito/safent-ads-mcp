"""T163 (tasks.md): casos dorados fiscales que `test_unit_economics.py`,
`test_lag_curve.py` y `test_platform_divergence.py` no cubren todavia como
tabla explicita -- IVA exento/21 % cruzado con plan de pago, `delta_hat` con
poco volumen (nunca un numero extremo/confiado), truncamiento sin sesgo con
una curva de Kaplan-Meier hecha a mano, y el margen de contribucion sobre
una tabla de tasas de devolucion. Cada valor esperado se deriva a mano con
la formula de `profitability-engine.md §1/§2` (aritmetica `Decimal`
independiente, ver el historial de este fichero), no llamando al propio
codigo de produccion para generar el oraculo."""

from __future__ import annotations

from datetime import date
from decimal import Decimal
from typing import NamedTuple

import pytest

from safent_ads.economics.domain.identifiers import ProductId, UnitEconomicsProfileId
from safent_ads.economics.domain.lag_curve import LagCurve, LagObservation
from safent_ads.economics.domain.money import Money
from safent_ads.economics.domain.platform_divergence import PlatformDivergence
from safent_ads.economics.domain.rate import Rate, Theta
from safent_ads.economics.domain.unit_economics import UnitEconomicsProfile
from safent_ads.shared.ids import BusinessId

_BUSINESS_ID = BusinessId.new()
_PRODUCT_ID = ProductId.parse("00000000-0000-0000-0000-000000000001")
_D_MAX = 30


def _profile(*, vat_rate: str, collection_rate: str) -> UnitEconomicsProfile:
    """Precio 1.000 €, sin descuento ni devolucion ni costes, `theta` en el
    suelo (0,20): aisla el efecto de IVA y plan de pago de todo lo demas."""
    return UnitEconomicsProfile.create(
        profile_id=UnitEconomicsProfileId.new(),
        business_id=_BUSINESS_ID,
        product_id=_PRODUCT_ID,
        version=1,
        effective_from=date(2026, 1, 1),
        list_price=Money.of("1000"),
        vat_rate=Rate.of(vat_rate),
        discount_rate=Rate.zero(),
        refund_rate=Rate.zero(),
        delivery_cost=Money.zero(),
        sales_cost_per_close=Money.zero(),
        collection_rate=Rate.of(collection_rate),
        cvr_lead_to_business_conversion=Rate.of("0.10"),
        theta=Theta(Decimal("0.20")),
        margin_horizon_days=30,
    )


# ---------------------------------------------------------------------------
# IVA exento vs 21 %, cruzado con plan de pago (cobro parcial) vs cobro
# integro -- las 4 combinaciones de profitability-engine.md §1. Valores
# calculados a mano (Decimal independiente, ver bash history de esta tarea):
# 1000/1.21 = 826.446... -> redondeo HALF_UP a 826.45.
# ---------------------------------------------------------------------------


class _VatPaymentPlanCase(NamedTuple):
    vat_rate: str
    collection_rate: str
    net_revenue: str
    collected: str
    contribution_margin: str
    target_cpe: str
    target_cpl: str


@pytest.mark.parametrize(
    "case",
    [
        pytest.param(
            _VatPaymentPlanCase("0", "1", "1000.00", "1000.00", "1000.00", "800.00", "80.00"),
            id="exento_sin_plan",
        ),
        pytest.param(
            _VatPaymentPlanCase("0", "0.90", "1000.00", "900.00", "900.00", "720.00", "72.00"),
            id="exento_con_plan",
        ),
        pytest.param(
            _VatPaymentPlanCase("0.21", "1", "826.45", "826.45", "826.45", "661.16", "66.12"),
            id="21pct_sin_plan",
        ),
        pytest.param(
            _VatPaymentPlanCase("0.21", "0.90", "826.45", "743.81", "743.81", "595.05", "59.51"),
            id="21pct_con_plan",
        ),
    ],
)
def test_vat_and_payment_plan_golden_matrix(case: _VatPaymentPlanCase) -> None:
    profile = _profile(vat_rate=case.vat_rate, collection_rate=case.collection_rate)

    assert profile.net_revenue() == Money.of(case.net_revenue)
    assert profile.collected() == Money.of(case.collected)
    assert profile.contribution_margin() == Money.of(case.contribution_margin)
    assert profile.target_cost_per_conversion() == Money.of(case.target_cpe)
    assert profile.target_cost_per_lead() == Money.of(case.target_cpl)


# ---------------------------------------------------------------------------
# Margen de contribucion sobre una tabla de tasas de devolucion: `collected`
# fijo en 1.000 €, entrega 50 €, comercial 100 € -- `CM = 1000*(1-refund) -
# 150`, calculado a mano por fila.
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("refund_rate", "expected_cm"),
    [
        ("0.00", "850.00"),
        ("0.05", "800.00"),
        ("0.10", "750.00"),
        ("0.25", "600.00"),
        ("0.50", "350.00"),
    ],
)
def test_contribution_margin_over_refund_rate_table(refund_rate: str, expected_cm: str) -> None:
    profile = UnitEconomicsProfile.create(
        profile_id=UnitEconomicsProfileId.new(),
        business_id=_BUSINESS_ID,
        product_id=_PRODUCT_ID,
        version=1,
        effective_from=date(2026, 1, 1),
        list_price=Money.of("1000"),
        vat_rate=Rate.zero(),
        discount_rate=Rate.zero(),
        refund_rate=Rate.of(refund_rate),
        delivery_cost=Money.of("50"),
        sales_cost_per_close=Money.of("100"),
        collection_rate=Rate.one(),
        cvr_lead_to_business_conversion=Rate.of("0.10"),
        theta=Theta(Decimal("0.20")),
        margin_horizon_days=30,
    )
    assert profile.contribution_margin() == Money.of(expected_cm)


# ---------------------------------------------------------------------------
# delta_hat con poco volumen: el shrinkage (m=10) mantiene el valor lejos de
# los extremos 0 y de cualquier numero grande incluso en el peor caso
# (0 conversiones de un lado, ratio crudo 0 o infinito) -- "provisional",
# nunca un numero confiado. Valores calculados a mano:
# (crm+10)/(platform+10).
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("crm_conversions", "platform_conversions", "expected_value"),
    [
        (0, 0, "1.0"),
        (0, 1, "0.9090909090909091"),
        (1, 0, "1.1"),
        (0, 5, "0.6666666666666666"),
        (5, 0, "1.5"),
        (1, 1, "1.0"),
    ],
)
def test_delta_hat_low_volume_never_reports_an_extreme_number(
    crm_conversions: int, platform_conversions: int, expected_value: str
) -> None:
    divergence = PlatformDivergence.compute(
        crm_conversions=crm_conversions, platform_conversions=platform_conversions
    )

    assert divergence.value == pytest.approx(float(expected_value), abs=1e-9)
    # Ni con el ratio crudo mas extremo posible a este volumen (0 eventos de
    # un lado) delta_hat sale de una banda estrecha alrededor de 1,0: la
    # cuenta con datos suficientes es la unica que puede alejarse de verdad
    # (`test_high_volume_converges_to_raw_ratio`, ya existente).
    assert 0.6 <= divergence.value <= 1.6, (
        f"delta_hat={divergence.value} deberia quedar dentro de la banda de sanidad "
        "con tan poco volumen -- shrinkage roto"
    )


# ---------------------------------------------------------------------------
# Truncamiento sin sesgo: curva de Kaplan-Meier calculada a mano sobre 10
# leads (2 convierten dia 1, 3 dia 3, 1 dia 6, 4 nunca convierten) frente a
# la MISMA cohorte vista solo hasta el dia 4 -- la parte ya observada
# (F(1), F(3)) tiene que coincidir exactamente; solo diverge donde la vista
# truncada todavia no puede saber lo que pasara (dia 6, fuera de su
# horizonte).
#
# Calculo a mano (formula KM, profitability-engine.md §2):
#   dia 1: en_riesgo=10, eventos=2 -> superviv.=0.8            F(1)=0.20
#   dia 3: en_riesgo=8,  eventos=3 -> superviv.=0.8*0.625=0.5  F(3)=0.50
#   dia 6 (solo en la vista completa): en_riesgo=5, eventos=1
#          -> superviv.=0.5*0.8=0.4                            F(6)=0.60
# ---------------------------------------------------------------------------


def _ten_lead_cohort() -> list[LagObservation]:
    converted = [1, 1, 3, 3, 3, 6]
    non_converters = 4
    return [LagObservation(duration_days=d, converted=True) for d in converted] + [
        LagObservation(duration_days=_D_MAX, converted=False) for _ in range(non_converters)
    ]


def _truncated_at(cutoff: int) -> list[LagObservation]:
    """Misma cohorte, vista como si hoy solo pudieramos observar hasta
    `cutoff` dias: todo lo que convertiria despues (o que aun no ha
    convertido) queda censurado en `cutoff`, no en `_D_MAX`."""
    full = _ten_lead_cohort()
    return [
        obs if obs.converted and obs.duration_days <= cutoff
        else LagObservation(duration_days=cutoff, converted=False)
        for obs in full
    ]


class TestHandComputedTruncationIsUnbiased:
    def test_full_curve_matches_hand_computed_km(self) -> None:
        curve = LagCurve.from_observations(_ten_lead_cohort(), d_max=_D_MAX)

        assert curve.f(1) == pytest.approx(0.20, abs=1e-9)
        assert curve.f(3) == pytest.approx(0.50, abs=1e-9)
        assert curve.f(6) == pytest.approx(0.60, abs=1e-9)

    def test_truncated_view_matches_the_full_curve_before_the_cutoff(self) -> None:
        full_curve = LagCurve.from_observations(_ten_lead_cohort(), d_max=_D_MAX)
        truncated_curve = LagCurve.from_observations(_truncated_at(4), d_max=_D_MAX)

        # Ya observado en ambas vistas (dias 1 y 3): coincidencia exacta,
        # cero sesgo por la censura futura.
        assert truncated_curve.f(1) == pytest.approx(full_curve.f(1), abs=1e-9)
        assert truncated_curve.f(3) == pytest.approx(full_curve.f(3), abs=1e-9)
        assert truncated_curve.f(1) == pytest.approx(0.20, abs=1e-9)
        assert truncated_curve.f(3) == pytest.approx(0.50, abs=1e-9)

    def test_truncated_view_cannot_see_past_its_own_horizon(self) -> None:
        """No es sesgo: es el limite honesto de una cohorte cuyo evento del
        dia 6 todavia no ha ocurrido a fecha del corte (dia 4)."""
        full_curve = LagCurve.from_observations(_ten_lead_cohort(), d_max=_D_MAX)
        truncated_curve = LagCurve.from_observations(_truncated_at(4), d_max=_D_MAX)

        assert full_curve.f(6) == pytest.approx(0.60, abs=1e-9)
        assert truncated_curve.f(6) == pytest.approx(0.50, abs=1e-9)
        assert truncated_curve.f(6) < full_curve.f(6)

"""Adaptadores SQL sobre `offering_economics` (0029_economics_inputs):
`SqlMarginInputsPort` (lado de lectura de `BuildUnitEconomicsProfile`,
`economics/application/ports.py::MarginInputsPort`) y
`SqlOfferingEconomicsRepository` (lado de listado/escritura del panel,
`OfferingEconomicsRepository`) -- misma tabla, dos puertos por ISP, un
unico mapeador de fila compartido para no duplicar la traduccion NUMERIC/
BIGINT -> `Decimal`/`int`.

`theta`/`margin_horizon_days` de `MarginInputs` NO viven en la tabla (el
dueño no los teclea en este ticket): `SqlMarginInputsPort` aplica los
valores por defecto de profitability-engine.md §1/Assumptions (`theta`
0,35 via `Theta.default()`; horizonte 90 dias, el mismo que usa el resto
del banco de pruebas de `economics` para perfiles confirmados)."""

from __future__ import annotations

from decimal import Decimal
from typing import Final

from sqlalchemy import RowMapping, text
from sqlalchemy.ext.asyncio import AsyncSession

from safent_ads.economics.application.ports import (
    MarginInputs,
    OfferingEconomics,
    OfferingEconomicsInput,
    OfferingSummary,
)
from safent_ads.economics.domain.identifiers import ProductId
from safent_ads.economics.domain.money import Money
from safent_ads.economics.domain.payment_plan import PaymentPlan
from safent_ads.economics.domain.rate import Rate, Theta
from safent_ads.shared.ids import BusinessId

__all__ = ["SqlMarginInputsPort", "SqlOfferingEconomicsRepository"]

_CENTS_PER_UNIT = 100
# profitability-engine.md §1/Assumptions: "theta 0,35 y suelo 0,20; margen
# a 90 dias" -- el dueño no aporta ninguno de los dos en este ticket
# (0029_economics_inputs no tiene columnas para ellos).
_DEFAULT_MARGIN_HORIZON_DAYS = 90

_SELECT_ECONOMICS_ROW = """
    SELECT vat_rate_pct, delivery_cost_minor, sales_cost_minor, refund_rate_pct,
           payment_plan, currency, updated_at
      FROM offering_economics
     WHERE offering_id = :offering_id AND business_id = :business_id
"""

_UPSERT_ECONOMICS = text("""
    INSERT INTO offering_economics (
        offering_id, business_id, vat_rate_pct, delivery_cost_minor, sales_cost_minor,
        refund_rate_pct, payment_plan, currency
    ) VALUES (
        :offering_id, :business_id, :vat_rate_pct, :delivery_cost_minor, :sales_cost_minor,
        :refund_rate_pct, :payment_plan, :currency
    )
    ON CONFLICT (offering_id) DO UPDATE SET
        vat_rate_pct        = EXCLUDED.vat_rate_pct,
        delivery_cost_minor = EXCLUDED.delivery_cost_minor,
        sales_cost_minor    = EXCLUDED.sales_cost_minor,
        refund_rate_pct     = EXCLUDED.refund_rate_pct,
        payment_plan        = EXCLUDED.payment_plan,
        currency            = EXCLUDED.currency,
        updated_at          = now()
    RETURNING vat_rate_pct, delivery_cost_minor, sales_cost_minor, refund_rate_pct,
              payment_plan, currency, updated_at
""")

_OFFERING_EXISTS = text(
    "SELECT 1 FROM offerings WHERE id = :offering_id AND business_id = :business_id"
)

_LIST_OFFERINGS_WITH_ECONOMICS: Final = """
    SELECT o.id AS offering_id, o.code, o.title, o.is_active,
           o.price_amount, o.price_currency,
           e.vat_rate_pct, e.delivery_cost_minor, e.sales_cost_minor, e.refund_rate_pct,
           e.payment_plan, e.currency, e.updated_at
      FROM offerings o
      LEFT JOIN offering_economics e ON e.offering_id = o.id
     WHERE o.business_id = :business_id
     ORDER BY o.title
"""


def _row_to_economics(row: RowMapping) -> OfferingEconomics:
    return OfferingEconomics(
        vat_rate_pct=row["vat_rate_pct"],
        delivery_cost_minor=row["delivery_cost_minor"],
        sales_cost_minor=row["sales_cost_minor"],
        refund_rate_pct=row["refund_rate_pct"],
        payment_plan=PaymentPlan(row["payment_plan"]),
        currency=row["currency"],
        updated_at=row["updated_at"],
    )


class SqlMarginInputsPort:
    """`economics.application.ports.MarginInputsPort` sobre
    `offering_economics`."""

    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def get_margin_inputs(
        self, *, business_id: BusinessId, product_id: ProductId
    ) -> MarginInputs | None:
        result = await self._session.execute(
            text(_SELECT_ECONOMICS_ROW),
            {"offering_id": product_id.value, "business_id": business_id.value},
        )
        row = result.mappings().one_or_none()
        if row is None:
            return None
        currency = row["currency"]
        refund_pct = row["refund_rate_pct"]
        return MarginInputs(
            vat_rate=Rate.of(row["vat_rate_pct"] / Decimal(100)),
            delivery_cost=Money.of(
                Decimal(row["delivery_cost_minor"]) / _CENTS_PER_UNIT, currency
            ),
            monthly_sales_team_cost=Money.of(
                Decimal(row["sales_cost_minor"]) / _CENTS_PER_UNIT, currency
            ),
            theta=Theta.default(),
            margin_horizon_days=_DEFAULT_MARGIN_HORIZON_DAYS,
            refund_rate_override=None if refund_pct is None else Rate.of(refund_pct / Decimal(100)),
        )


class SqlOfferingEconomicsRepository:
    """`economics.application.ports.OfferingEconomicsRepository` sobre
    `offerings`/`offering_economics`."""

    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def list_offerings_with_economics(
        self, *, business_id: BusinessId
    ) -> list[OfferingSummary]:
        result = await self._session.execute(
            text(_LIST_OFFERINGS_WITH_ECONOMICS), {"business_id": business_id.value}
        )
        return [_row_to_summary(row) for row in result.mappings().all()]

    async def offering_exists(self, *, business_id: BusinessId, offering_id: ProductId) -> bool:
        result = await self._session.execute(
            _OFFERING_EXISTS, {"offering_id": offering_id.value, "business_id": business_id.value}
        )
        return result.one_or_none() is not None

    async def upsert(
        self, *, business_id: BusinessId, offering_id: ProductId, economics: OfferingEconomicsInput
    ) -> OfferingEconomics:
        result = await self._session.execute(
            _UPSERT_ECONOMICS,
            {
                "offering_id": offering_id.value,
                "business_id": business_id.value,
                "vat_rate_pct": economics.vat_rate_pct,
                "delivery_cost_minor": economics.delivery_cost_minor,
                "sales_cost_minor": economics.sales_cost_minor,
                "refund_rate_pct": economics.refund_rate_pct,
                "payment_plan": economics.payment_plan.value,
                "currency": economics.currency,
            },
        )
        await self._session.flush()
        return _row_to_economics(result.mappings().one())


def _row_to_summary(row: RowMapping) -> OfferingSummary:
    price_amount = row["price_amount"]
    economics = None if row["vat_rate_pct"] is None else _row_to_economics(row)
    return OfferingSummary(
        offering_id=str(row["offering_id"]),
        code=row["code"],
        title=row["title"],
        is_active=row["is_active"],
        list_price=(
            None if price_amount is None else Money(price_amount, row["price_currency"])
        ),
        economics=economics,
    )

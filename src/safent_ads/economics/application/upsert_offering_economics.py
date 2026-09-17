"""`UpsertOfferingEconomics` (T131/T132, `PUT /offerings/{id}/economics`):
valida los rangos del dueño (contracts/rest-api.md: '0 ≤ vat ≤ 100, money ≥
0, refund 0–100') y escribe `offering_economics` -- desde ahi
`SqlMarginInputsPort` deja de devolver `None` y `BuildUnitEconomicsProfile`
puede dejar de nacer `provisional`."""

from __future__ import annotations

from decimal import Decimal

from safent_ads.economics.application.errors import (
    InvalidOfferingEconomicsError,
    OfferingNotFoundError,
)
from safent_ads.economics.application.ports import (
    OfferingEconomics,
    OfferingEconomicsInput,
    OfferingEconomicsRepository,
)
from safent_ads.economics.domain.identifiers import ProductId
from safent_ads.shared.ids import BusinessId

_PCT_MIN = Decimal("0")
_PCT_MAX = Decimal("100")
_CURRENCY_LENGTH = 3


class UpsertOfferingEconomics:
    def __init__(self, *, offerings: OfferingEconomicsRepository) -> None:
        self._offerings = offerings

    async def execute(
        self,
        *,
        business_id: BusinessId,
        offering_id: ProductId,
        economics: OfferingEconomicsInput,
    ) -> OfferingEconomics:
        _require_valid(economics)
        if not await self._offerings.offering_exists(
            business_id=business_id, offering_id=offering_id
        ):
            raise OfferingNotFoundError(str(offering_id))
        return await self._offerings.upsert(
            business_id=business_id, offering_id=offering_id, economics=economics
        )


def _require_valid(economics: OfferingEconomicsInput) -> None:
    _require_pct_in_range("vat_rate_pct", economics.vat_rate_pct)
    if economics.refund_rate_pct is not None:
        _require_pct_in_range("refund_rate_pct", economics.refund_rate_pct)
    _require_non_negative("delivery_cost_minor", economics.delivery_cost_minor)
    _require_non_negative("sales_cost_minor", economics.sales_cost_minor)
    if len(economics.currency) != _CURRENCY_LENGTH or not economics.currency.isupper():
        raise InvalidOfferingEconomicsError(
            f"currency debe ser ISO-4217 en mayusculas: {economics.currency!r}"
        )


def _require_pct_in_range(field: str, value: Decimal) -> None:
    if not (_PCT_MIN <= value <= _PCT_MAX):
        raise InvalidOfferingEconomicsError(f"{field} fuera de [0,100]: {value}")


def _require_non_negative(field: str, value: int) -> None:
    if value < 0:
        raise InvalidOfferingEconomicsError(f"{field} debe ser >= 0: {value}")

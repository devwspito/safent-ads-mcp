"""Repositorios SQL de `economics` sobre `unit_economics_profiles`,
`lag_curve_snapshots` y `platform_divergence_snapshots` (migracion 0016).

`unit_economics_profiles` es solo-anexable en el esquema (trigger que
deniega UPDATE/DELETE): `save()` siempre hace INSERT, nunca UPSERT. Las
otras dos tablas son cache (`lag_curve_snapshots`, UPSERT) o historico
append-only por diseno de aplicacion, no de esquema
(`platform_divergence_snapshots`, siempre INSERT)."""

from __future__ import annotations

import json
from collections.abc import Sequence
from datetime import date
from decimal import Decimal
from typing import Any, Final
from uuid import UUID

from sqlalchemy import RowMapping, text
from sqlalchemy.ext.asyncio import AsyncSession

from safent_ads.economics.domain.identifiers import ProductId, UnitEconomicsProfileId
from safent_ads.economics.domain.lag_curve import LagCurve
from safent_ads.economics.domain.money import Money
from safent_ads.economics.domain.platform_divergence import PlatformDivergence
from safent_ads.economics.domain.rate import Rate, Theta
from safent_ads.economics.domain.unit_economics import ProfileStatus, UnitEconomicsProfile
from safent_ads.shared.ids import BusinessId

__all__ = [
    "SqlCalendarEventLookupPort",
    "SqlLagCurveRepository",
    "SqlOfferingPricePort",
    "SqlPlatformDivergenceRepository",
    "SqlUnitEconomicsProfileRepository",
]

_INSERT_PROFILE: Final = """
    INSERT INTO unit_economics_profiles (
        id, business_id, product_id, version, effective_from, status,
        list_price_amount, list_price_currency, vat_rate, discount_rate, refund_rate,
        delivery_cost_amount, sales_cost_per_close_amount, collection_rate,
        cvr_lead_to_business_conversion, theta, margin_horizon_days,
        contribution_margin_override_amount
    ) VALUES (
        :id, :business_id, :product_id, :version, :effective_from, :status,
        :list_price_amount, :list_price_currency, :vat_rate, :discount_rate, :refund_rate,
        :delivery_cost_amount, :sales_cost_per_close_amount, :collection_rate,
        :cvr_lead_to_business_conversion, :theta, :margin_horizon_days,
        :contribution_margin_override_amount
    )
"""

_SELECT_PROFILE_COLUMNS: Final = """
    SELECT id, business_id, product_id, version, effective_from, status,
           list_price_amount, list_price_currency, vat_rate, discount_rate, refund_rate,
           delivery_cost_amount, sales_cost_per_close_amount, collection_rate,
           cvr_lead_to_business_conversion, theta, margin_horizon_days,
           contribution_margin_override_amount
      FROM unit_economics_profiles
"""

_SELECT_CURRENT_PROFILE: Final = f"""
    {_SELECT_PROFILE_COLUMNS}
     WHERE business_id = :business_id AND product_id = :product_id
       AND effective_from <= :as_of
     ORDER BY effective_from DESC
     LIMIT 1
"""

_SELECT_PROFILE_VERSIONS: Final = f"""
    {_SELECT_PROFILE_COLUMNS}
     WHERE business_id = :business_id AND product_id = :product_id
     ORDER BY effective_from ASC
"""


class SqlUnitEconomicsProfileRepository:
    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def get_current(
        self, *, business_id: BusinessId, product_id: ProductId, as_of: date
    ) -> UnitEconomicsProfile | None:
        result = await self._session.execute(
            text(_SELECT_CURRENT_PROFILE),
            {"business_id": business_id.value, "product_id": product_id.value, "as_of": as_of},
        )
        row = result.mappings().first()
        return None if row is None else _row_to_profile(row)

    async def list_versions(
        self, *, business_id: BusinessId, product_id: ProductId
    ) -> list[UnitEconomicsProfile]:
        result = await self._session.execute(
            text(_SELECT_PROFILE_VERSIONS),
            {"business_id": business_id.value, "product_id": product_id.value},
        )
        return [_row_to_profile(row) for row in result.mappings().all()]

    async def save(self, profile: UnitEconomicsProfile) -> None:
        await self._session.execute(text(_INSERT_PROFILE), _profile_params(profile))
        await self._session.flush()


def _profile_params(profile: UnitEconomicsProfile) -> dict[str, Any]:
    override = profile.contribution_margin_override
    return {
        "id": profile.profile_id.value,
        "business_id": profile.business_id.value,
        "product_id": profile.product_id.value,
        "version": profile.version,
        "effective_from": profile.effective_from,
        "status": profile.status.value,
        "list_price_amount": profile.list_price.amount,
        "list_price_currency": profile.list_price.currency,
        "vat_rate": profile.vat_rate.value,
        "discount_rate": profile.discount_rate.value,
        "refund_rate": profile.refund_rate.value,
        "delivery_cost_amount": profile.delivery_cost.amount,
        "sales_cost_per_close_amount": profile.sales_cost_per_close.amount,
        "collection_rate": profile.collection_rate.value,
        "cvr_lead_to_business_conversion": profile.cvr_lead_to_business_conversion.value,
        "theta": profile.theta.value,
        "margin_horizon_days": profile.margin_horizon_days,
        "contribution_margin_override_amount": None if override is None else override.amount,
    }


def _row_to_profile(row: RowMapping) -> UnitEconomicsProfile:
    currency = row["list_price_currency"]
    override_amount = row["contribution_margin_override_amount"]
    return UnitEconomicsProfile(
        profile_id=UnitEconomicsProfileId(row["id"]),
        business_id=BusinessId(row["business_id"]),
        product_id=ProductId(row["product_id"]),
        version=row["version"],
        effective_from=row["effective_from"],
        status=ProfileStatus(row["status"]),
        list_price=Money(row["list_price_amount"], currency),
        vat_rate=Rate(row["vat_rate"]),
        discount_rate=Rate(row["discount_rate"]),
        refund_rate=Rate(row["refund_rate"]),
        delivery_cost=Money(row["delivery_cost_amount"], currency),
        sales_cost_per_close=Money(row["sales_cost_per_close_amount"], currency),
        collection_rate=Rate(row["collection_rate"]),
        cvr_lead_to_business_conversion=Rate(row["cvr_lead_to_business_conversion"]),
        theta=Theta(row["theta"]),
        margin_horizon_days=row["margin_horizon_days"],
        contribution_margin_override=(
            None if override_amount is None else Money(override_amount, currency)
        ),
    )


_UPSERT_LAG_CURVE: Final = """
    INSERT INTO lag_curve_snapshots (
        business_id, product_id, platform, d_max, sample_size, cumulative_by_day, computed_at
    )
    VALUES (
        :business_id, :product_id, :platform, :d_max, :sample_size,
        CAST(:cumulative_by_day AS jsonb), now()
    )
    ON CONFLICT ON CONSTRAINT lag_curve_snapshots_unique DO UPDATE
        SET d_max             = EXCLUDED.d_max,
            sample_size       = EXCLUDED.sample_size,
            cumulative_by_day = EXCLUDED.cumulative_by_day,
            computed_at       = EXCLUDED.computed_at
"""

_SELECT_LAG_CURVE: Final = """
    SELECT d_max, sample_size, cumulative_by_day
      FROM lag_curve_snapshots
     WHERE business_id = :business_id AND product_id = :product_id AND platform = :platform
"""

_SELECT_MOST_MATURE_LAG_CURVE: Final = """
    SELECT d_max, sample_size, cumulative_by_day
      FROM lag_curve_snapshots
     WHERE business_id = :business_id AND platform = :platform
     ORDER BY sample_size DESC
     LIMIT 1
"""


class SqlLagCurveRepository:
    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def get_current(
        self, *, business_id: BusinessId, product_id: ProductId, platform: str
    ) -> LagCurve | None:
        result = await self._session.execute(
            text(_SELECT_LAG_CURVE),
            {
                "business_id": business_id.value,
                "product_id": product_id.value,
                "platform": platform,
            },
        )
        row = result.mappings().first()
        if row is None:
            return None
        cumulative = [float(v) for v in row["cumulative_by_day"]]
        return LagCurve.from_persisted(
            d_max=row["d_max"], sample_size=row["sample_size"], cumulative_by_day=cumulative
        )

    async def save(
        self, *, business_id: BusinessId, product_id: ProductId, platform: str, curve: LagCurve
    ) -> None:
        await self._session.execute(
            text(_UPSERT_LAG_CURVE),
            {
                "business_id": business_id.value,
                "product_id": product_id.value,
                "platform": platform,
                "d_max": curve.d_max,
                "sample_size": curve.sample_size,
                "cumulative_by_day": json.dumps(list(curve.as_cumulative_sequence())),
            },
        )
        await self._session.flush()

    async def get_most_mature_for_business(
        self, *, business_id: BusinessId, platform: str
    ) -> LagCurve | None:
        """T157: `orchestration` no conoce todavia que producto vende cada
        campana (`AdEntity` no lleva `product_id`) -- hasta que exista esa
        costura, la puerta de rezago usa la curva de mayor `sample_size`
        del negocio para esa plataforma en vez de una constante fija.
        Metodo propio de esta clase (no del puerto `LagCurveRepository`):
        solo lo llama `orchestration`, mismo patron que otros adaptadores
        SQL concretos que otra lane usa directamente (`rule_step.py` con
        `SqlRuleRepository`)."""
        result = await self._session.execute(
            text(_SELECT_MOST_MATURE_LAG_CURVE),
            {"business_id": business_id.value, "platform": platform},
        )
        row = result.mappings().first()
        if row is None:
            return None
        cumulative = [float(v) for v in row["cumulative_by_day"]]
        return LagCurve.from_persisted(
            d_max=row["d_max"], sample_size=row["sample_size"], cumulative_by_day=cumulative
        )


_INSERT_DIVERGENCE: Final = """
    INSERT INTO platform_divergence_snapshots (
        business_id, platform_account_id, crm_conversions, platform_conversions,
        shrinkage_m, value, window_start, window_end
    ) VALUES (
        :business_id, :platform_account_id, :crm_conversions, :platform_conversions,
        :shrinkage_m, :value, :window_start, :window_end
    )
"""

_SELECT_DIVERGENCE_HISTORY: Final = """
    SELECT crm_conversions, platform_conversions, shrinkage_m, value, window_start, window_end
      FROM platform_divergence_snapshots
     WHERE business_id = :business_id AND platform_account_id = :platform_account_id
     ORDER BY computed_at DESC
     LIMIT :n
"""


class SqlPlatformDivergenceRepository:
    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def get_latest(
        self, *, business_id: BusinessId, platform_account_id: str
    ) -> PlatformDivergence | None:
        history = await self._recent(business_id, platform_account_id, n=1)
        return history[0] if history else None

    async def get_previous(
        self, *, business_id: BusinessId, platform_account_id: str
    ) -> PlatformDivergence | None:
        history = await self._recent(business_id, platform_account_id, n=2)
        return history[1] if len(history) > 1 else None

    async def save(
        self, *, business_id: BusinessId, platform_account_id: str, divergence: PlatformDivergence
    ) -> None:
        await self._session.execute(
            text(_INSERT_DIVERGENCE),
            {
                "business_id": business_id.value,
                "platform_account_id": UUID(platform_account_id),
                "crm_conversions": divergence.crm_conversions,
                "platform_conversions": divergence.platform_conversions,
                "shrinkage_m": divergence.shrinkage_m,
                "value": Decimal(str(divergence.value)),
                "window_start": divergence.window_start,
                "window_end": divergence.window_end,
            },
        )
        await self._session.flush()

    async def _recent(
        self, business_id: BusinessId, platform_account_id: str, *, n: int
    ) -> list[PlatformDivergence]:
        result = await self._session.execute(
            text(_SELECT_DIVERGENCE_HISTORY),
            {
                "business_id": business_id.value,
                "platform_account_id": UUID(platform_account_id),
                "n": n,
            },
        )
        return [
            PlatformDivergence(
                crm_conversions=row["crm_conversions"],
                platform_conversions=row["platform_conversions"],
                shrinkage_m=row["shrinkage_m"],
                value=float(row["value"]),
                window_start=row["window_start"],
                window_end=row["window_end"],
            )
            for row in result.mappings().all()
        ]


_SELECT_CALENDAR_EVENT_IDS_FOR_OFFERING: Final = """
    SELECT id FROM calendar_events WHERE business_id = :business_id AND offering_id = :offering_id
"""

_SELECT_OFFERING_PRICE: Final = """
    SELECT price_amount, price_currency FROM offerings
     WHERE id = :offering_id AND business_id = :business_id
"""


class SqlCalendarEventLookupPort:
    """`economics.application.ports.CalendarEventLookupPort` sobre
    `calendar_events` (tabla de `catalog`, 0025_vocabulary): la unica
    costura de lectura que `economics` necesita de ese contexto, sin
    importar sus tipos de dominio (permitido: 'economics sobre catalog/
    crm/metrics')."""

    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def list_calendar_event_ids_for_product(
        self, *, business_id: BusinessId, product_id: ProductId
    ) -> Sequence[str]:
        result = await self._session.execute(
            text(_SELECT_CALENDAR_EVENT_IDS_FOR_OFFERING),
            {"business_id": business_id.value, "offering_id": product_id.value},
        )
        return [str(row["id"]) for row in result.mappings().all()]


class SqlOfferingPricePort:
    """`economics.application.ports.OfferingPricePort` sobre `offerings`."""

    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def get_list_price(
        self, *, business_id: BusinessId, product_id: ProductId
    ) -> Money | None:
        result = await self._session.execute(
            text(_SELECT_OFFERING_PRICE),
            {"business_id": business_id.value, "offering_id": product_id.value},
        )
        row = result.mappings().one_or_none()
        if row is None or row["price_amount"] is None:
            return None
        return Money(row["price_amount"], row["price_currency"])

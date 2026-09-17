"""Vistas de lectura de `economics` (patron `panel.application.dto`): forma
propia de presentacion, no el agregado de dominio, para que MCP/REST no
acoplen su serializacion a los invariantes internos de `UnitEconomicsProfile`."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime

from safent_ads.economics.domain.money import Money


@dataclass(frozen=True, kw_only=True, slots=True)
class UnitEconomicsView:
    product_id: str
    version: int
    effective_from: date
    status: str
    net_revenue: Money
    collected: Money
    contribution_margin: Money
    target_cost_per_conversion: Money
    target_cost_per_lead: Money
    target_roas: float
    theta: float
    margin_horizon_days: int


@dataclass(frozen=True, kw_only=True, slots=True)
class TargetCpaView:
    product_id: str
    target_cost_per_conversion: Money
    target_cost_per_lead: Money
    confidence: str  # "confirmed" | "provisional"


@dataclass(frozen=True, kw_only=True, slots=True)
class LagCurveView:
    product_id: str
    platform: str
    d_max: int
    sample_size: int
    median_lag_days: int | None
    curve: tuple[tuple[int, float], ...]  # (day, F(day)) muestreado


@dataclass(frozen=True, kw_only=True, slots=True)
class CohortProjectionView:
    product_id: str
    platform: str
    age_days: int
    observed: int
    maturity: float
    projected: float
    projected_low: float
    projected_high: float
    can_raise: bool


@dataclass(frozen=True, kw_only=True, slots=True)
class PlatformDivergenceView:
    platform_account_id: str
    crm_conversions: int
    platform_conversions: int
    value: float
    is_outside_sanity_band: bool
    is_jump_anomaly: bool | None  # None si no hay periodo anterior con el que comparar


@dataclass(frozen=True, kw_only=True, slots=True)
class CrmReconciliationView:
    """`get_crm_reconciliation` (tool-surface.md §2.4, T160): plataforma vs
    CRM sobre una ventana, con el rezago declarado (FR-3) -- nunca corrige
    gasto, solo dice donde miente la plataforma."""

    platform_conversions: int
    crm_conversions: int
    lag_days: int
    gap_pct: float
    # Spec 027: entrada del puente CRM->anuncios en el MISMO informe de
    # reconciliacion. `None` = ningun puente configurado para el negocio
    # (nunca `True`/`False` fabricado); ver `CrmBridgeHealth.evaluate`.
    customer_bridge_healthy: bool | None = None


@dataclass(frozen=True, kw_only=True, slots=True)
class AttributionWindowRow:
    """Una fila de `compare_attribution_windows`: misma entidad, ventana de
    `window_days` terminando en `as_of`."""

    window_days: int
    platform_conversions: int
    crm_conversions: int
    gap_pct: float


@dataclass(frozen=True, kw_only=True, slots=True)
class ConversionBridgeHealthView:
    """`get_conversion_bridge_health` (T160): WhatsApp y llamada como
    conversion -- ¿siguen llegando?"""

    action: str
    last_event_at: datetime | None
    daily_count: int
    healthy: bool

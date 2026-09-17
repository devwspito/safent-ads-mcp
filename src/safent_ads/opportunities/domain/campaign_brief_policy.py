"""Politica de brief por defecto para un hueco de calendario sin cobertura
(tasks.md T113). Puro: siempre el minimo de prueba del bundle (20 EUR/dia,
7 dias) -- ninguna subida de gasto sin medicion (profitability-engine.md
§1); ampliar el presupuesto es decision del propietario al aprobar, nunca
de este ciclo."""

from __future__ import annotations

from datetime import date

from safent_ads.opportunities.domain.campaign_brief import (
    MIN_DURATION_DAYS,
    MIN_TEST_BUDGET,
    CampaignBrief,
)
from safent_ads.shared.ids import PlatformCode

_MAX_OFFERING_NAME_IN_TEXT = 60


def default_brief_for_calendar_event_gap(
    *,
    platform: PlatformCode,
    offering_id: str,
    offering_name: str,
    calendar_event_id: str,
    window_end: date,
    region: str | None,
) -> CampaignBrief:
    short_name = offering_name[:_MAX_OFFERING_NAME_IN_TEXT]
    return CampaignBrief(
        objective=f"Cubrir demanda de {short_name} antes de {window_end.isoformat()}",
        platform=platform,
        offering_id=offering_id,
        daily_budget=MIN_TEST_BUDGET,
        duration_days=MIN_DURATION_DAYS,
        success_criterion=(
            f"Coste por lead <= objetivo de {short_name} sostenido 3 dias seguidos"
        ),
        kill_criterion="Cero conversiones con >= 3x el coste por lead objetivo en 5 dias",
        angle=f"Cobertura de {short_name}",
        targeting_seed=short_name,
        geo=region,
        calendar_event_id=calendar_event_id,
    )

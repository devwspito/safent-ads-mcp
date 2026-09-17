"""`build_lag_observations` (T156): la capa anticorrupcion que menciona
`economics.application.ports.LagObservationRepository` -- traduce
`crm.LeadAttribution` en `economics.domain.lag_curve.LagObservation` por
cohorte diaria de lead (profitability-engine.md §2). Pura: sin `Clock`
propio (`as_of` llega como parametro), sin I/O -- el adaptador de
`infrastructure/` hace la lectura, esta funcion solo traduce."""

from __future__ import annotations

from collections.abc import Sequence
from datetime import date

from safent_ads.crm.domain.conversion_kind import ConversionKind
from safent_ads.crm.domain.lead_attribution import LeadAttribution
from safent_ads.economics.domain.lag_curve import LagObservation


def build_lag_observations(
    attributions: Sequence[LeadAttribution], *, platform: str, as_of: date
) -> list[LagObservation]:
    """Cohorte = primer `lead` visto en `platform` por identidad hasheada.
    `duration_days` es hasta el primer `business_conversion` de esa misma
    identidad (converge en cualquier plataforma: la atribucion puede
    cambiar de peldano entre el lead y el cierre) o, si no ha convertido
    todavia, hasta `as_of` -- censura por la derecha (Kaplan-Meier,
    `LagCurve.from_observations`)."""
    first_lead_by_identity = _earliest_by_identity(
        attributions, kind=ConversionKind.LEAD, platform=platform
    )
    first_conversion_by_identity = _earliest_by_identity(
        attributions, kind=ConversionKind.BUSINESS_CONVERSION, platform=None
    )
    observations: list[LagObservation] = []
    for identity, lead_date in first_lead_by_identity.items():
        conversion_date = first_conversion_by_identity.get(identity)
        if conversion_date is not None and conversion_date >= lead_date:
            observations.append(
                LagObservation(duration_days=(conversion_date - lead_date).days, converted=True)
            )
        else:
            observations.append(
                LagObservation(duration_days=max((as_of - lead_date).days, 0), converted=False)
            )
    return observations


def _earliest_by_identity(
    attributions: Sequence[LeadAttribution], *, kind: ConversionKind, platform: str | None
) -> dict[str, date]:
    earliest: dict[str, date] = {}
    for attribution in attributions:
        if attribution.conversion_kind is not kind or attribution.hashed_identity is None:
            continue
        if platform is not None and (
            attribution.entity_ref is None or attribution.entity_ref.platform.value != platform
        ):
            continue
        identity = attribution.hashed_identity.digest
        occurred_date = attribution.occurred_at.date()
        current = earliest.get(identity)
        if current is None or occurred_date < current:
            earliest[identity] = occurred_date
    return earliest

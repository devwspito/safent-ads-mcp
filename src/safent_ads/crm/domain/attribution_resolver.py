"""`AttributionResolver` (plan.md §5): escalera gclid/fbclid -> UTM ->
identidad hasheada -> agregado.

Servicio de dominio puro: recibe los mapas ya resueltos (click id -> entidad,
UTM -> entidad, identidad hasheada -> entidad) porque `crm` no tiene puertos
de infraestructura en el dominio; la resolucion de esos mapas es
responsabilidad de `crm/application`."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime

from safent_ads.crm.domain.conversion_kind import ConversionKind
from safent_ads.crm.domain.hashed_identity import HashedIdentity
from safent_ads.crm.domain.lead_attribution import (
    AttributionRung,
    LeadAttribution,
    LeadAttributionId,
)
from safent_ads.shared.ids import BusinessId, EntityRef


@dataclass(frozen=True, kw_only=True, slots=True)
class ConversionSignal:
    """Senal de conversion ya despseudonimizada: ningun campo crudo de lead
    (threat-model.md C-31)."""

    business_id: BusinessId
    conversion_kind: ConversionKind
    value_minor: int
    occurred_at: datetime
    observed_at: datetime
    gclid: str | None = None
    fbclid: str | None = None
    utm_campaign: str | None = None
    hashed_identity: HashedIdentity | None = None
    calendar_event_id: str | None = None


class AttributionResolver:
    def resolve(
        self,
        signal: ConversionSignal,
        *,
        click_id_to_entity: Mapping[str, EntityRef],
        utm_campaign_to_entity: Mapping[str, EntityRef],
        hashed_identity_to_entity: Mapping[HashedIdentity, EntityRef],
    ) -> LeadAttribution:
        rung, entity_ref = self._climb(
            signal, click_id_to_entity, utm_campaign_to_entity, hashed_identity_to_entity
        )
        return LeadAttribution(
            lead_attribution_id=LeadAttributionId.new(),
            business_id=signal.business_id,
            hashed_identity=signal.hashed_identity,
            entity_ref=entity_ref,
            attribution_rung=rung,
            conversion_kind=signal.conversion_kind,
            value_minor=signal.value_minor,
            occurred_at=signal.occurred_at,
            observed_at=signal.observed_at,
            calendar_event_id=signal.calendar_event_id,
        )

    def _climb(
        self,
        signal: ConversionSignal,
        click_id_to_entity: Mapping[str, EntityRef],
        utm_campaign_to_entity: Mapping[str, EntityRef],
        hashed_identity_to_entity: Mapping[HashedIdentity, EntityRef],
    ) -> tuple[AttributionRung, EntityRef | None]:
        for click_id in (signal.gclid, signal.fbclid):
            if click_id is not None and click_id in click_id_to_entity:
                return AttributionRung.CLICK_ID, click_id_to_entity[click_id]
        if signal.utm_campaign is not None and signal.utm_campaign in utm_campaign_to_entity:
            return AttributionRung.UTM, utm_campaign_to_entity[signal.utm_campaign]
        identity = signal.hashed_identity
        if identity is not None and identity in hashed_identity_to_entity:
            return AttributionRung.HASHED_IDENTITY, hashed_identity_to_entity[identity]
        return AttributionRung.AGGREGATE, None

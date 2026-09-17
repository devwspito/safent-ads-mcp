"""`LeadAttribution` (data-model.md §LeadAttribution, tabla `lead_attributions`).

Invariante: prohibido el dato personal. Solo `HashedIdentity` o una
`EntityRef`; jamas un email, telefono o nombre."""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum

from safent_ads.crm.domain.conversion_kind import ConversionKind
from safent_ads.crm.domain.hashed_identity import HashedIdentity
from safent_ads.shared.ids import BusinessId, EntityRef


class AttributionRung(StrEnum):
    """Peldano de la escalera de atribucion usado para resolver la conversion
    (rule-catalog-and-signals.md; data-model.md: 'click_id | hashed_identity |
    aggregate'). `UTM` se anade como peldano intermedio, documentado en
    plan.md §5 ('gclid/fbclid/UTM -> identidad hasheada -> agregado')."""

    CLICK_ID = "click_id"
    UTM = "utm"
    HASHED_IDENTITY = "hashed_identity"
    AGGREGATE = "aggregate"


@dataclass(frozen=True, slots=True)
class LeadAttributionId:
    value: uuid.UUID

    @classmethod
    def new(cls) -> LeadAttributionId:
        return cls(uuid.uuid4())

    def __str__(self) -> str:
        return str(self.value)


@dataclass(frozen=True, kw_only=True, slots=True)
class LeadAttribution:
    lead_attribution_id: LeadAttributionId
    business_id: BusinessId
    hashed_identity: HashedIdentity | None
    entity_ref: EntityRef | None
    attribution_rung: AttributionRung
    conversion_kind: ConversionKind
    value_minor: int
    occurred_at: datetime
    observed_at: datetime
    calendar_event_id: str | None = None

    @property
    def is_attributed_to_entity(self) -> bool:
        return self.entity_ref is not None

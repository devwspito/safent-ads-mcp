"""`SqlLeadAttributionRepository` sobre `lead_attributions`
(`0005_catalog_crm:91`, T145): la atribucion ya resuelta por
`AttributionResolver` que `economics`/`optimization` leen a traves de sus
propios puertos (capa anticorrupcion).

`identity_salt_ref` (NOT NULL en el esquema) es el alias que resuelve el
broker de secretos, nunca la sal (`hashed_identity.py`): ese broker vive
fuera de este carril (`broker/`, otra rama). Hasta que exista un puerto de
rotacion real, el alias es deterministico por negocio (`business-<uuid>`) --
Assumption documentada, no un secreto ni un intento de generar uno."""

from __future__ import annotations

from collections.abc import Sequence
from datetime import date, datetime

from sqlalchemy import RowMapping, text
from sqlalchemy.ext.asyncio import AsyncSession

from safent_ads.crm.domain.conversion_kind import ConversionKind
from safent_ads.crm.domain.hashed_identity import HashedIdentity
from safent_ads.crm.domain.lead_attribution import (
    AttributionRung,
    LeadAttribution,
    LeadAttributionId,
)
from safent_ads.crm.infrastructure.errors import MissingHashedIdentityError
from safent_ads.shared.ids import BusinessId, EntityRef

__all__ = ["SqlLeadAttributionRepository"]

_INSERT = text("""
    INSERT INTO lead_attributions (
        id, business_id, hashed_identity, identity_salt_ref, click_id_hash, entity_ref,
        calendar_event_id, attribution_rung, conversion_kind, value_amount, value_currency,
        occurred_at, observed_at
    ) VALUES (
        :id, :business_id, :hashed_identity, :identity_salt_ref, NULL, :entity_ref,
        :calendar_event_id, :attribution_rung, :conversion_kind, :value_amount, :value_currency,
        :occurred_at, :observed_at
    )
    ON CONFLICT ON CONSTRAINT lead_attributions_natural_unique DO NOTHING
    RETURNING id
""")

_SELECT_COLUMNS = """
    SELECT id, business_id, hashed_identity, entity_ref, calendar_event_id, attribution_rung,
           conversion_kind, value_amount, occurred_at, observed_at
      FROM lead_attributions
"""

_FIND_FOR_CALENDAR_EVENTS = text(f"""
    {_SELECT_COLUMNS}
     WHERE business_id = :business_id AND calendar_event_id = ANY(:calendar_event_ids)
     ORDER BY occurred_at
""")

_COUNT_BY_KIND_IN_WINDOW = text("""
    SELECT count(*) AS n
      FROM lead_attributions
     WHERE business_id = :business_id
       AND conversion_kind = :conversion_kind
       AND occurred_at >= :window_start AND occurred_at < :window_end
       AND (CAST(:entity_ref AS TEXT) IS NULL OR entity_ref = :entity_ref)
""")

_LAST_EVENT_AT = text("""
    SELECT max(occurred_at) AS last_event_at
      FROM lead_attributions
     WHERE business_id = :business_id AND conversion_kind = :conversion_kind
""")

_SELECT_DISTINCT_ENTITY_REFS_IN_WINDOW = text("""
    SELECT DISTINCT entity_ref
      FROM lead_attributions
     WHERE business_id = :business_id
       AND entity_ref IS NOT NULL
       AND occurred_at >= :window_start AND occurred_at < :window_end
""")

# `value_amount`/`value_currency` (columnas de la tabla, NUMERIC(12,2) + ISO
# 4217) frente a `value_minor` (int, centimos) del dominio: la misma
# conversion que ya hace `economics.infrastructure.sql_repositories` para
# `Money`. El negocio de referencia opera en EUR (0001_bootstrap); sin un
# `currency` propio en `LeadAttribution` (el dominio no lo modela: es un
# hueco preexistente, no de este ticket), se asume EUR aqui igual que el
# resto del esquema de conversiones.
_CENTS_PER_UNIT = 100
_DEFAULT_CURRENCY = "EUR"


class SqlLeadAttributionRepository:
    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def save(self, attribution: LeadAttribution) -> bool:
        if attribution.hashed_identity is None:
            raise MissingHashedIdentityError(str(attribution.lead_attribution_id))
        result = await self._session.execute(_INSERT, _attribution_params(attribution))
        await self._session.flush()
        # `RETURNING id` con `ON CONFLICT ... DO NOTHING`: sin fila devuelta,
        # la insercion no tuvo efecto (ya existia) -- mas portable que
        # `.rowcount` (mypy: `CursorResult` generico no lo tipa) y evita
        # confiar en un detalle del driver.
        return result.mappings().one_or_none() is not None

    async def find_for_calendar_events(
        self, *, business_id: BusinessId, calendar_event_ids: Sequence[str]
    ) -> Sequence[LeadAttribution]:
        if not calendar_event_ids:
            return []
        result = await self._session.execute(
            _FIND_FOR_CALENDAR_EVENTS,
            {"business_id": business_id.value, "calendar_event_ids": list(calendar_event_ids)},
        )
        return [_row_to_attribution(row) for row in result.mappings().all()]

    async def count_by_kind_in_window(
        self,
        *,
        business_id: BusinessId,
        conversion_kind: ConversionKind,
        window_start: date,
        window_end: date,
        entity_ref: str | None = None,
    ) -> int:
        result = await self._session.execute(
            _COUNT_BY_KIND_IN_WINDOW,
            {
                "business_id": business_id.value,
                "conversion_kind": conversion_kind.value,
                "window_start": window_start,
                "window_end": window_end,
                "entity_ref": entity_ref,
            },
        )
        return int(result.scalar_one())

    async def last_event_at(
        self, *, business_id: BusinessId, conversion_kind: ConversionKind
    ) -> datetime | None:
        result = await self._session.execute(
            _LAST_EVENT_AT,
            {"business_id": business_id.value, "conversion_kind": conversion_kind.value},
        )
        return result.scalar_one_or_none()

    async def list_distinct_entity_refs_in_window(
        self, *, business_id: BusinessId, window_start: date, window_end: date
    ) -> Sequence[str]:
        result = await self._session.execute(
            _SELECT_DISTINCT_ENTITY_REFS_IN_WINDOW,
            {
                "business_id": business_id.value,
                "window_start": window_start,
                "window_end": window_end,
            },
        )
        return [str(row["entity_ref"]) for row in result.mappings().all()]


def _attribution_params(attribution: LeadAttribution) -> dict[str, object]:
    identity = attribution.hashed_identity
    assert identity is not None  # noqa: S101 - `save()` ya lo comprobo
    return {
        "id": attribution.lead_attribution_id.value,
        "business_id": attribution.business_id.value,
        "hashed_identity": identity.digest,
        "identity_salt_ref": f"business-{attribution.business_id.value}",
        "entity_ref": (
            str(attribution.entity_ref) if attribution.entity_ref is not None else None
        ),
        "calendar_event_id": attribution.calendar_event_id,
        "attribution_rung": _to_stored_rung(attribution.attribution_rung),
        "conversion_kind": attribution.conversion_kind.value,
        "value_amount": attribution.value_minor / _CENTS_PER_UNIT,
        "value_currency": _DEFAULT_CURRENCY,
        "occurred_at": attribution.occurred_at,
        "observed_at": attribution.observed_at,
    }


def _to_stored_rung(rung: AttributionRung) -> str:
    """`lead_attributions.attribution_rung` (0005_catalog_crm) no tiene el
    peldano `UTM` que `AttributionRung` anadio despues (plan.md §5): se
    persiste como `hashed_identity`, el peldano inmediato inferior --
    Assumption documentada, escalar a `database-engineer` si UTM necesita
    su propia columna algun dia."""
    if rung is AttributionRung.UTM:
        return AttributionRung.HASHED_IDENTITY.value
    return rung.value


def _row_to_attribution(row: RowMapping) -> LeadAttribution:
    entity_ref = row["entity_ref"]
    calendar_event_id = row["calendar_event_id"]
    return LeadAttribution(
        lead_attribution_id=LeadAttributionId(row["id"]),
        business_id=BusinessId(row["business_id"]),
        hashed_identity=HashedIdentity.from_digest(
            business_id=BusinessId(row["business_id"]), digest=row["hashed_identity"]
        ),
        entity_ref=EntityRef.parse(entity_ref) if entity_ref is not None else None,
        attribution_rung=AttributionRung(row["attribution_rung"]),
        conversion_kind=ConversionKind(row["conversion_kind"]),
        value_minor=round(row["value_amount"] * _CENTS_PER_UNIT),
        occurred_at=row["occurred_at"],
        observed_at=row["observed_at"],
        calendar_event_id=str(calendar_event_id) if calendar_event_id is not None else None,
    )

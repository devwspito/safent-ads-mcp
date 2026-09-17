"""Nucleo compartido de `POST /conversions/import` (CSV, T220) y
`POST /conversions/webhook` (una fila JSON, misma forma): construir un
`ConversionSignal` valido a partir de lo que trae el propietario, y
resolverlo con `AttributionResolver` -- reusa la escalera de atribucion
del dominio en vez de fijar `AGGREGATE` a mano (si algun dia este carril
recibe mapas reales de click_id/UTM -> entidad, funciona sin tocar este
fichero).

`offering_id`/`gclid`/`fbclid` se validan (forma) pero no se resuelven
todavia a `calendar_event_id`/`entity_ref` -- Assumption documentada: esa
resolucion pertenece a `catalog`/`accounts` (otro carril, `broker`/
`accounts` fuera de alcance aqui) y `LeadAttribution` los admite `None`."""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import datetime

from safent_ads.crm.domain.attribution_resolver import AttributionResolver, ConversionSignal
from safent_ads.crm.domain.conversion_kind import ConversionKind
from safent_ads.crm.domain.hashed_identity import HashedIdentity
from safent_ads.crm.domain.lead_attribution import LeadAttribution
from safent_ads.shared.errors import ApplicationError
from safent_ads.shared.ids import BusinessId

_SUPPORTED_CURRENCY = "EUR"
_MAX_FIELD_LENGTH = 512
_UUID_PATTERN = re.compile(
    r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$", re.IGNORECASE
)


class ConversionRowError(ApplicationError):
    """Fila de conversion (CSV o webhook) invalida: motivo legible por el
    propietario, nunca una traza tecnica."""


@dataclass(frozen=True, kw_only=True, slots=True)
class ConversionRow:
    """Forma tipada de una fila de conversion, ya coercionada desde texto
    (CSV) o JSON (webhook) -- `build_conversion_signal` es lo unico que
    ambos caminos comparten."""

    kind: ConversionKind
    occurred_at: datetime
    amount_minor: int
    currency: str
    offering_id: str | None
    gclid: str | None
    fbclid: str | None
    external_ref: str | None
    email: str | None
    phone: str | None


def build_conversion_signal(
    business_id: BusinessId, row: ConversionRow, *, salt: str, observed_at: datetime
) -> ConversionSignal:
    _require_supported_currency(row.currency)
    _require_valid_offering_id(row.offering_id)
    for field_name, value in (("gclid", row.gclid), ("fbclid", row.fbclid)):
        _require_reasonable_length(field_name, value)
    identity = _resolve_hashed_identity(business_id, row, salt)
    return ConversionSignal(
        business_id=business_id,
        conversion_kind=row.kind,
        value_minor=row.amount_minor,
        occurred_at=row.occurred_at,
        observed_at=observed_at,
        gclid=row.gclid,
        fbclid=row.fbclid,
        hashed_identity=identity,
    )


def resolve_conversion_attribution(signal: ConversionSignal) -> LeadAttribution:
    return AttributionResolver().resolve(
        signal, click_id_to_entity={}, utm_campaign_to_entity={}, hashed_identity_to_entity={}
    )


def _require_supported_currency(currency: str) -> None:
    if currency and currency != _SUPPORTED_CURRENCY:
        raise ConversionRowError(f"currency no soportada: {currency!r} (solo EUR por ahora)")


def _require_valid_offering_id(offering_id: str | None) -> None:
    if offering_id is not None and not _UUID_PATTERN.match(offering_id):
        raise ConversionRowError(f"offering_id no es un UUID valido: {offering_id!r}")


def _require_reasonable_length(field_name: str, value: str | None) -> None:
    if value is not None and len(value) > _MAX_FIELD_LENGTH:
        raise ConversionRowError(f"{field_name} supera {_MAX_FIELD_LENGTH} caracteres")


def _resolve_hashed_identity(
    business_id: BusinessId, row: ConversionRow, salt: str
) -> HashedIdentity:
    raw_identifier = row.email or row.phone or row.external_ref
    if raw_identifier is None:
        raise ConversionRowError("falta email, phone o external_ref: nada que hashear")
    return HashedIdentity.compute(business_id=business_id, raw_identifier=raw_identifier, salt=salt)

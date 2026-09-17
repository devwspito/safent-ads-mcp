"""Mapeo generico de un payload JSON de CRM -> `ConversionSignal`
(threat-model.md C-31: 'CRM como agregados pseudonimizados; ningun campo
crudo al modelo, Telegram ni logs').

`ConversionFieldMapping` declara, por integracion, el nombre de campo JSON
externo que alimenta cada campo canonico: un CRM con otro vocabulario de
campos solo necesita su propia instancia, nunca tocar `parse_conversion_signal`.
Lista blanca derivada de esa instancia: cualquier campo ajeno (nombre,
email, telefono...) que el payload incluyera por error se ignora, nunca se
propaga al dominio (`test_no_pii_in_model_context`)."""

from __future__ import annotations

from dataclasses import dataclass, fields
from datetime import datetime
from typing import Any

from safent_ads.crm.domain.attribution_resolver import ConversionSignal
from safent_ads.crm.domain.conversion_kind import ConversionKind
from safent_ads.crm.domain.hashed_identity import HashedIdentity
from safent_ads.crm.infrastructure.errors import CrmRequestError
from safent_ads.shared.ids import BusinessId


@dataclass(frozen=True, kw_only=True, slots=True)
class ConversionFieldMapping:
    """Nombre del campo JSON externo que alimenta cada campo canonico de
    `ConversionSignal`. El valor por defecto asume que el CRM ya habla en
    estos nombres."""

    conversion_kind: str = "conversion_kind"
    value_minor: str = "value_minor"
    occurred_at: str = "occurred_at"
    observed_at: str = "observed_at"
    gclid: str = "gclid"
    fbclid: str = "fbclid"
    utm_campaign: str = "utm_campaign"
    hashed_identity_digest: str = "hashed_identity_digest"
    calendar_event_id: str = "calendar_event_id"

    def allowed_source_fields(self) -> frozenset[str]:
        return frozenset(getattr(self, field.name) for field in fields(self))


DEFAULT_CONVERSION_FIELD_MAPPING = ConversionFieldMapping()


def parse_conversion_signal(
    business_id: BusinessId,
    raw: dict[str, Any],
    field_mapping: ConversionFieldMapping = DEFAULT_CONVERSION_FIELD_MAPPING,
) -> ConversionSignal:
    _reject_unexpected_fields(raw, field_mapping)
    try:
        return ConversionSignal(
            business_id=business_id,
            conversion_kind=ConversionKind(raw[field_mapping.conversion_kind]),
            value_minor=int(raw[field_mapping.value_minor]),
            occurred_at=datetime.fromisoformat(raw[field_mapping.occurred_at]),
            observed_at=datetime.fromisoformat(raw[field_mapping.observed_at]),
            gclid=raw.get(field_mapping.gclid),
            fbclid=raw.get(field_mapping.fbclid),
            utm_campaign=raw.get(field_mapping.utm_campaign),
            hashed_identity=_parse_hashed_identity(
                business_id, raw.get(field_mapping.hashed_identity_digest)
            ),
            calendar_event_id=raw.get(field_mapping.calendar_event_id),
        )
    except (KeyError, ValueError, TypeError) as exc:
        raise CrmRequestError("payload de conversion con forma inesperada") from exc


def _parse_hashed_identity(business_id: BusinessId, digest: str | None) -> HashedIdentity | None:
    if digest is None:
        return None
    return HashedIdentity.from_digest(business_id=business_id, digest=digest)


def _reject_unexpected_fields(raw: dict[str, Any], field_mapping: ConversionFieldMapping) -> None:
    unexpected = set(raw) - field_mapping.allowed_source_fields()
    if unexpected:
        raise CrmRequestError(f"campos no permitidos en payload: {unexpected}")

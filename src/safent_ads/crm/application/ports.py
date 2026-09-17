"""`CrmPort` (plan.md §5: 'Puerto: CrmPort'). El adaptador HTTP concreto
vive en `crm/infrastructure/`, detras de este puerto (oposads-assessment:
'Adapter is CRM-specific; keep behind CrmPort')."""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from datetime import date, datetime
from typing import Protocol

from safent_ads.crm.domain.attribution_resolver import ConversionSignal
from safent_ads.crm.domain.conversion_kind import ConversionKind
from safent_ads.crm.domain.lead_attribution import LeadAttribution
from safent_ads.shared.ids import BusinessId


@dataclass(frozen=True, kw_only=True, slots=True)
class CrmConversionsRequest:
    business_id: BusinessId
    window_start: date
    window_end: date


class CrmPort(Protocol):
    """Devuelve conversiones ya agregadas/pseudonimizadas: nunca un email,
    telefono o nombre crudo (NFR-8, threat-model.md C-31)."""

    async def fetch_conversions(
        self, request: CrmConversionsRequest
    ) -> Sequence[ConversionSignal]: ...


class LeadAttributionRepository(Protocol):
    """`lead_attributions` (0005_catalog_crm): la atribucion YA resuelta
    (`AttributionResolver.resolve`) que este contexto persiste y que
    `economics`/`optimization` leen a traves de sus propios puertos (capa
    anticorrupcion, plan.md §4: 'economics sobre catalog/crm/metrics').

    `save()` es idempotente sobre la clave natural de la tabla
    (`business_id, hashed_identity, conversion_kind, occurred_at`): una
    reingesta del CRM nunca duplica una fila. Devuelve `True` si la fila
    era nueva, `False` si ya existia (T220, `POST /conversions/import`
    necesita distinguir `imported` de `duplicates` en su resumen)."""

    async def save(self, attribution: LeadAttribution) -> bool: ...

    async def find_for_calendar_events(
        self, *, business_id: BusinessId, calendar_event_ids: Sequence[str]
    ) -> Sequence[LeadAttribution]:
        """Atribuciones cuya conversion ocurrio dentro de alguno de estos
        eventos de calendario (`economics`: traducir a `LagObservation` por
        `(product_id, platform)` via `calendar_events.offering_id`)."""
        ...

    async def count_by_kind_in_window(
        self,
        *,
        business_id: BusinessId,
        conversion_kind: ConversionKind,
        window_start: date,
        window_end: date,
        entity_ref: str | None = None,
    ) -> int:
        """Conteo del lado CRM para reconciliacion/divergencia
        (profitability-engine.md §2). `entity_ref=None` cuenta todo el
        negocio; con `entity_ref` se acota a una sola entidad (`compare_
        attribution_windows`)."""
        ...

    async def last_event_at(
        self, *, business_id: BusinessId, conversion_kind: ConversionKind
    ) -> datetime | None:
        """Salud del puente de conversion (`get_conversion_bridge_health`):
        cuando fue la ultima vez que este tipo de conversion llego."""
        ...

    async def list_distinct_entity_refs_in_window(
        self, *, business_id: BusinessId, window_start: date, window_end: date
    ) -> Sequence[str]:
        """Entidades que el CRM SI pudo atribuir en la ventana
        (`get_crm_reconciliation`): la contrapartida de plataforma
        (`metrics`) solo se suma sobre estas, nunca sobre toda la cuenta --
        `economics` no depende de `accounts` para enumerar campanas."""
        ...


class IdentitySaltProvider(Protocol):
    """Sal de `HashedIdentity.compute` para identidades crudas que llegan
    POR PRIMERA VEZ a este carril (`POST /conversions/import`/`webhook`):
    a diferencia de `HttpCrmAdapter` (que solo recibe digests ya calculados
    rio arriba, `parse_conversion_signal` -> `HashedIdentity.from_digest`),
    el CSV/webhook del propietario trae `email`/`phone`/`external_ref` en
    crudo -- alguien tiene que hashearlos antes de que toquen `crm.domain`.
    Determinista por negocio (HKDF sobre `ADS_SESSION_SECRET`, nunca un
    secreto propio -- `shared/crypto/hkdf.py`)."""

    def for_business(self, business_id: BusinessId) -> str: ...


class WebhookTokenRepository(Protocol):
    """`conversion_webhook_tokens` (0029_economics_inputs, T220): un token
    activo por negocio, solo el hash se persiste."""

    async def upsert(self, *, business_id: BusinessId, token_hash: str) -> None: ...

    async def find_business_id_by_token_hash(self, token_hash: str) -> BusinessId | None: ...

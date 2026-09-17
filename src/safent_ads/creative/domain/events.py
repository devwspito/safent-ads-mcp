"""Eventos de dominio de `creative` (data-model.md §"Domain events").
`creative` nunca importa `proposals`: `CreativeAssetReady` es lo unico que
cruza la frontera, publicado en `DomainEventBus` y suscrito en
`composition` (plan.md §4, T108 — fuera del alcance de este carril)."""

from __future__ import annotations

from dataclasses import dataclass

from safent_ads.creative.domain.identifiers import AssetId, BriefId, JobId, SignalId
from safent_ads.shared.events import DomainEvent


@dataclass(frozen=True, kw_only=True)
class CreativeAssetReady(DomainEvent):
    asset_id: AssetId
    brief_id: BriefId
    source_signal_id: SignalId | None


@dataclass(frozen=True, kw_only=True)
class CreativeJobFailed(DomainEvent):
    job_id: JobId
    brief_id: BriefId
    reason: str


@dataclass(frozen=True, kw_only=True)
class CreativePolicyRejected(DomainEvent):
    asset_id: AssetId
    finding_codes: tuple[str, ...]

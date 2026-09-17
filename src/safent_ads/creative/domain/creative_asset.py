"""`CreativeAsset` (agregado raiz, plan.md §5 `creative`; data-model.md
§CreativeAsset). Guarda la trazabilidad senal -> pieza (FR-34) y aplica la
regla invariable de `creative-port.md`: "Un activo con `PolicyVerdict.FAIL`
no puede referenciarse en una propuesta de publicacion; el agregado lo
rechaza" — aqui, en `propose()`."""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import datetime

from safent_ads.creative.domain.copy import AdCopy
from safent_ads.creative.domain.enums import (
    CreativeAssetState,
    CreativeOutcome,
    Format,
    GenerationStatus,
    MediaKind,
    PolicyVerdictResult,
    RendererName,
    ReviewState,
)
from safent_ads.creative.domain.identifiers import AssetId, BriefId, SignalId
from safent_ads.creative.domain.money import Money
from safent_ads.creative.domain.policy import PolicyVerdict
from safent_ads.creative.domain.storage import StorageUri
from safent_ads.shared.ids import BusinessId

_SHA256_HEX_PATTERN = re.compile(r"^[0-9a-f]{64}$")

_VALID_TRANSITIONS: dict[CreativeAssetState, frozenset[CreativeAssetState]] = {
    CreativeAssetState.DRAFT: frozenset({CreativeAssetState.READY, CreativeAssetState.REJECTED}),
    CreativeAssetState.READY: frozenset(
        {CreativeAssetState.PROPOSED, CreativeAssetState.REJECTED}
    ),
    CreativeAssetState.PROPOSED: frozenset(
        {CreativeAssetState.APPROVED, CreativeAssetState.REJECTED}
    ),
    CreativeAssetState.APPROVED: frozenset({CreativeAssetState.PUBLISHED}),
    CreativeAssetState.REJECTED: frozenset(),
    CreativeAssetState.PUBLISHED: frozenset(),
}

# Proyeccion de `CreativeAssetState` (6 valores, ciclo de vida completo de
# render/publicacion) a `ReviewState` (3 valores, lo que el panel necesita
# para la revision humana): ningun campo nuevo, `review_state` siempre se
# deriva del mismo estado que ya gobierna `_VALID_TRANSITIONS`, nunca una
# segunda fuente de verdad que pueda desincronizarse.
_REVIEW_STATE_BY_ASSET_STATE: dict[CreativeAssetState, ReviewState] = {
    CreativeAssetState.DRAFT: ReviewState.PENDING,
    CreativeAssetState.READY: ReviewState.PENDING,
    CreativeAssetState.PROPOSED: ReviewState.PENDING,
    CreativeAssetState.APPROVED: ReviewState.APPROVED,
    CreativeAssetState.PUBLISHED: ReviewState.APPROVED,
    CreativeAssetState.REJECTED: ReviewState.REJECTED,
}


class InvalidCreativeAssetTransitionError(ValueError):
    """La transicion de estado pedida no esta permitida desde el estado actual."""


class UnpublishableCreativeAssetError(ValueError):
    """Se intento proponer un activo cuyo `PolicyVerdict` es `FAIL`."""


class CreativeAssetError(ValueError):
    """Violacion de un invariante de construccion de `CreativeAsset`."""


@dataclass(frozen=True, slots=True)
class Provenance:
    """FR-34: de donde sale cada pieza, para poder reconstruir senal ->
    pieza -> resultado sin volver a preguntar al modelo.

    `generation_status` es obligatorio y sin valor por defecto a proposito
    (correccion del propietario 2026-09-09: "no silent degradation, no
    pretending an asset was produced") — cada caso de uso que construye un
    `Provenance` debe decidir explicitamente si la pieza lleva un visual
    generado por modelo o es compositor-solo sobre la plantilla de marca."""

    renderer_used: RendererName
    model_name: str
    seed: int | None
    brief_id: BriefId
    source_signal_id: SignalId | None
    generation_status: GenerationStatus
    generated_at: datetime


@dataclass(frozen=True, slots=True)
class Traceability:
    """Vista de lectura de FR-34 sobre un `CreativeAsset` concreto."""

    source_signal_id: SignalId | None
    brief_id: BriefId
    asset_id: AssetId
    outcome: CreativeOutcome


class CreativeAsset:
    def __init__(
        self,
        *,
        asset_id: AssetId,
        business_id: BusinessId,
        media_kind: MediaKind,
        format: Format | None,  # noqa: A002 - nombre del contrato (creative-port.md)
        duration_seconds: float | None,
        storage_uri: StorageUri,
        checksum: str,
        cost_estimate: Money,
        provenance: Provenance,
        ad_copy: AdCopy | None = None,
        destination_url: str | None = None,
    ) -> None:
        self._require_sha256(checksum)
        self._asset_id = asset_id
        self._business_id = business_id
        self._media_kind = media_kind
        self._format = format
        self._duration_seconds = duration_seconds
        self._storage_uri = storage_uri
        self._checksum = checksum
        self._cost_estimate = cost_estimate
        self._provenance = provenance
        self._ad_copy = ad_copy
        self._destination_url = destination_url
        self._state = CreativeAssetState.DRAFT
        self._policy_verdict: PolicyVerdict | None = None
        self._outcome = CreativeOutcome.PENDING

    @classmethod
    def _reconstitute(
        cls,
        *,
        asset_id: AssetId,
        business_id: BusinessId,
        media_kind: MediaKind,
        format: Format | None,  # noqa: A002 - nombre del contrato (creative-port.md)
        duration_seconds: float | None,
        storage_uri: StorageUri,
        checksum: str,
        cost_estimate: Money,
        provenance: Provenance,
        ad_copy: AdCopy | None,
        destination_url: str | None,
        state: CreativeAssetState,
        policy_verdict: PolicyVerdict | None,
        outcome: CreativeOutcome,
    ) -> CreativeAsset:
        """Reconstruye un `CreativeAsset` ya persistido (SOLO para un
        repositorio, nunca un caso de uso: `state`/`policy_verdict`/
        `outcome` se restauran directamente en vez de recorrer las
        transiciones que los produjeron la primera vez -- el trigger CHECK
        de `creative_assets.state`/`policy_verdict`/`outcome` ya garantiza
        que la fila solo contiene una combinacion alcanzable por esas
        transiciones)."""
        asset = cls(
            asset_id=asset_id,
            business_id=business_id,
            media_kind=media_kind,
            format=format,
            duration_seconds=duration_seconds,
            storage_uri=storage_uri,
            checksum=checksum,
            cost_estimate=cost_estimate,
            provenance=provenance,
            ad_copy=ad_copy,
            destination_url=destination_url,
        )
        asset._state = state
        asset._policy_verdict = policy_verdict
        asset._outcome = outcome
        return asset

    @staticmethod
    def _require_sha256(checksum: str) -> None:
        if not _SHA256_HEX_PATTERN.match(checksum):
            raise CreativeAssetError(f"checksum debe ser sha256 hex: {checksum!r}")

    @property
    def asset_id(self) -> AssetId:
        return self._asset_id

    @property
    def business_id(self) -> BusinessId:
        return self._business_id

    @property
    def media_kind(self) -> MediaKind:
        return self._media_kind

    @property
    def format(self) -> Format | None:
        return self._format

    @property
    def duration_seconds(self) -> float | None:
        return self._duration_seconds

    @property
    def storage_uri(self) -> StorageUri:
        return self._storage_uri

    @property
    def checksum(self) -> str:
        return self._checksum

    @property
    def cost_estimate(self) -> Money:
        return self._cost_estimate

    @property
    def provenance(self) -> Provenance:
        return self._provenance

    @property
    def ad_copy(self) -> AdCopy | None:
        return self._ad_copy

    @property
    def destination_url(self) -> str | None:
        return self._destination_url

    @property
    def state(self) -> CreativeAssetState:
        return self._state

    @property
    def policy_verdict(self) -> PolicyVerdict | None:
        return self._policy_verdict

    @property
    def outcome(self) -> CreativeOutcome:
        return self._outcome

    @property
    def review_state(self) -> ReviewState:
        return _REVIEW_STATE_BY_ASSET_STATE[self._state]

    @property
    def traceability(self) -> Traceability:
        return Traceability(
            source_signal_id=self._provenance.source_signal_id,
            brief_id=self._provenance.brief_id,
            asset_id=self._asset_id,
            outcome=self._outcome,
        )

    def _transition_to(self, target: CreativeAssetState) -> None:
        allowed = _VALID_TRANSITIONS[self._state]
        if target not in allowed:
            raise InvalidCreativeAssetTransitionError(
                f"{self._state} -> {target} no permitido"
            )
        self._state = target

    def mark_ready(self, policy_verdict: PolicyVerdict) -> None:
        if policy_verdict.verdict == PolicyVerdictResult.FAIL:
            self._policy_verdict = policy_verdict
            self._transition_to(CreativeAssetState.REJECTED)
            return
        self._policy_verdict = policy_verdict
        self._transition_to(CreativeAssetState.READY)

    def propose(self) -> None:
        if self._policy_verdict is None or not self._policy_verdict.is_publishable:
            raise UnpublishableCreativeAssetError(
                f"{self._asset_id} no tiene un PolicyVerdict publicable"
            )
        self._transition_to(CreativeAssetState.PROPOSED)

    def approve(self) -> None:
        self._transition_to(CreativeAssetState.APPROVED)

    def reject(self) -> None:
        self._transition_to(CreativeAssetState.REJECTED)

    def mark_published(self) -> None:
        self._transition_to(CreativeAssetState.PUBLISHED)

    def record_outcome(self, outcome: CreativeOutcome) -> None:
        if self._state != CreativeAssetState.PUBLISHED:
            raise InvalidCreativeAssetTransitionError(
                f"solo se registra outcome sobre un activo PUBLISHED, estado actual={self._state}"
            )
        self._outcome = outcome

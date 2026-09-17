"""`CreativeJob` (agregado de trabajo GPU, plan.md §5 `creative`;
data-model.md §CreativeAsset). Idempotente por `brief_hash + variant_index`
(UNIQUE en `0011_creative`, T098): un reintento reutiliza los activos ya
renderizados de ese trabajo — la idempotencia la aplica el caso de uso al
buscar por `idempotency_key` antes de crear un `CreativeJob` nuevo, no una
transicion interna de este agregado."""

from __future__ import annotations

from dataclasses import dataclass

from safent_ads.creative.domain.enums import CreativeJobState
from safent_ads.creative.domain.identifiers import AssetId, BriefId, JobId
from safent_ads.shared.ids import BusinessId

_TERMINAL_STATES = frozenset(
    {CreativeJobState.READY, CreativeJobState.FAILED, CreativeJobState.FALLBACK_CLOUD}
)

_VALID_TRANSITIONS: dict[CreativeJobState, frozenset[CreativeJobState]] = {
    CreativeJobState.QUEUED: frozenset({CreativeJobState.RENDERING, CreativeJobState.FAILED}),
    CreativeJobState.RENDERING: frozenset(
        {CreativeJobState.COMPOSING, CreativeJobState.FAILED}
    ),
    CreativeJobState.COMPOSING: frozenset({CreativeJobState.CHECKING, CreativeJobState.FAILED}),
    CreativeJobState.CHECKING: frozenset(
        {CreativeJobState.READY, CreativeJobState.FALLBACK_CLOUD, CreativeJobState.FAILED}
    ),
    CreativeJobState.READY: frozenset(),
    CreativeJobState.FAILED: frozenset(),
    CreativeJobState.FALLBACK_CLOUD: frozenset(),
}

_MIN_PROGRESS = 0.0
_MAX_PROGRESS = 1.0


class InvalidCreativeJobTransitionError(ValueError):
    """La transicion de estado pedida no esta permitida desde el estado actual."""


class CreativeJobError(ValueError):
    """Violacion de un invariante de `CreativeJob`."""


@dataclass(frozen=True, slots=True)
class CreativeJobIdempotencyKey:
    """`brief_hash + variant_index` (data-model.md): identifica de forma
    estable un trabajo, aunque se reintente con un `JobId` nuevo."""

    brief_hash: str
    variant_index: int

    def __post_init__(self) -> None:
        if not self.brief_hash:
            raise CreativeJobError("brief_hash vacio")
        if self.variant_index < 0:
            raise CreativeJobError(f"variant_index debe ser >= 0: {self.variant_index!r}")

    def __str__(self) -> str:
        return f"{self.brief_hash}:{self.variant_index}"


class CreativeJob:
    def __init__(
        self,
        *,
        job_id: JobId,
        business_id: BusinessId,
        brief_id: BriefId,
        idempotency_key: CreativeJobIdempotencyKey,
    ) -> None:
        self._job_id = job_id
        self._business_id = business_id
        self._brief_id = brief_id
        self._idempotency_key = idempotency_key
        self._state = CreativeJobState.QUEUED
        self._progress = _MIN_PROGRESS
        self._asset_ids: tuple[AssetId, ...] = ()
        self._failure_reason: str | None = None

    @classmethod
    def _reconstitute(
        cls,
        *,
        job_id: JobId,
        business_id: BusinessId,
        brief_id: BriefId,
        idempotency_key: CreativeJobIdempotencyKey,
        state: CreativeJobState,
        progress: float,
        asset_ids: tuple[AssetId, ...],
        failure_reason: str | None,
    ) -> CreativeJob:
        """Reconstruye un `CreativeJob` ya persistido (SOLO para un
        repositorio, mismo criterio que `CreativeAsset._reconstitute`)."""
        job = cls(
            job_id=job_id,
            business_id=business_id,
            brief_id=brief_id,
            idempotency_key=idempotency_key,
        )
        job._state = state
        job._progress = progress
        job._asset_ids = asset_ids
        job._failure_reason = failure_reason
        return job

    @property
    def job_id(self) -> JobId:
        return self._job_id

    @property
    def business_id(self) -> BusinessId:
        return self._business_id

    @property
    def brief_id(self) -> BriefId:
        return self._brief_id

    @property
    def idempotency_key(self) -> CreativeJobIdempotencyKey:
        return self._idempotency_key

    @property
    def state(self) -> CreativeJobState:
        return self._state

    @property
    def progress(self) -> float:
        return self._progress

    @property
    def asset_ids(self) -> tuple[AssetId, ...]:
        return self._asset_ids

    @property
    def failure_reason(self) -> str | None:
        return self._failure_reason

    @property
    def is_terminal(self) -> bool:
        return self._state in _TERMINAL_STATES

    def _transition_to(self, target: CreativeJobState) -> None:
        allowed = _VALID_TRANSITIONS[self._state]
        if target not in allowed:
            raise InvalidCreativeJobTransitionError(f"{self._state} -> {target} no permitido")
        self._state = target

    def update_progress(self, progress: float) -> None:
        if self.is_terminal:
            raise InvalidCreativeJobTransitionError("no se actualiza progreso de un job terminal")
        if not _MIN_PROGRESS <= progress <= _MAX_PROGRESS:
            raise CreativeJobError(f"progress fuera de [0,1]: {progress!r}")
        self._progress = progress

    def start_rendering(self) -> None:
        self._transition_to(CreativeJobState.RENDERING)

    def start_composing(self) -> None:
        self._transition_to(CreativeJobState.COMPOSING)

    def start_checking(self) -> None:
        self._transition_to(CreativeJobState.CHECKING)

    def mark_ready(self, asset_ids: tuple[AssetId, ...]) -> None:
        if not asset_ids:
            raise CreativeJobError("mark_ready requiere al menos un asset_id")
        self._transition_to(CreativeJobState.READY)
        self._asset_ids = asset_ids
        self._progress = _MAX_PROGRESS

    def mark_fallback_cloud(self, asset_ids: tuple[AssetId, ...]) -> None:
        if not asset_ids:
            raise CreativeJobError("mark_fallback_cloud requiere al menos un asset_id")
        self._transition_to(CreativeJobState.FALLBACK_CLOUD)
        self._asset_ids = asset_ids
        self._progress = _MAX_PROGRESS

    def mark_failed(self, reason: str) -> None:
        if not reason.strip():
            raise CreativeJobError("reason vacio")
        self._transition_to(CreativeJobState.FAILED)
        self._failure_reason = reason

    def cancel(self) -> None:
        self.mark_failed("cancelado")

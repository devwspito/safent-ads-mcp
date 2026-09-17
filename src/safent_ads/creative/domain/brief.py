"""`CreativeBrief` (creative-port.md §"Tipos del dominio"; agregado segun
plan.md §5 `creative`). Campos identicos al contrato; se reordena solo
`language` al final para que el dataclass sea valido en Python (un campo
con valor por defecto no puede preceder a uno sin defecto) — no cambia el
conjunto de campos ni su opcionalidad."""

from __future__ import annotations

import hashlib
from collections.abc import Sequence
from dataclasses import dataclass, field

from safent_ads.creative.domain.brand_kit import BrandKit
from safent_ads.creative.domain.enums import CampaignObjective, Language
from safent_ads.creative.domain.identifiers import CalendarEventId, SignalId
from safent_ads.creative.domain.shot import ShotDescription
from safent_ads.shared.ids import BusinessId

_MIN_SHOTS = 3
_MAX_SHOTS = 5
_MAX_VARIANT_COUNT = 8
_MAX_VOICEOVER_LINES = 20
_MAX_HOOK_LEN = 200
_MAX_ON_SCREEN_TEXT_LEN = 60


class CreativeBriefError(ValueError):
    """Violacion de un invariante de `CreativeBrief`."""


@dataclass(frozen=True, slots=True, kw_only=True)
class CreativeBrief:
    business_id: BusinessId
    calendar_event_id: CalendarEventId | None
    objective: CampaignObjective
    audience_summary: str
    hook: str
    shots: Sequence[ShotDescription]
    on_screen_text: Sequence[str]
    cta: str
    voiceover_lines: Sequence[str]
    brand_kit: BrandKit
    source_signal_id: SignalId | None
    variant_count: int
    language: Language = field(default=Language.ES_ES)

    def __post_init__(self) -> None:
        self._require_non_empty("audience_summary", self.audience_summary)
        self._require_bounded("hook", self.hook, _MAX_HOOK_LEN)
        self._require_non_empty("cta", self.cta)
        self._require_shots_in_range()
        self._require_on_screen_text_bounded()
        self._require_voiceover_lines_bounded()
        self._require_variant_count_bounded()

    def _require_non_empty(self, field_name: str, value: str) -> None:
        if not value.strip():
            raise CreativeBriefError(f"{field_name} vacio")

    def _require_bounded(self, field_name: str, value: str, max_len: int) -> None:
        self._require_non_empty(field_name, value)
        if len(value) > max_len:
            raise CreativeBriefError(f"{field_name} supera {max_len} caracteres")

    def _require_shots_in_range(self) -> None:
        count = len(self.shots)
        if not _MIN_SHOTS <= count <= _MAX_SHOTS:
            raise CreativeBriefError(
                f"shots debe tener entre {_MIN_SHOTS} y {_MAX_SHOTS} planos, llegaron {count}"
            )

    def _require_on_screen_text_bounded(self) -> None:
        for text in self.on_screen_text:
            self._require_bounded("on_screen_text", text, _MAX_ON_SCREEN_TEXT_LEN)

    def _require_voiceover_lines_bounded(self) -> None:
        if len(self.voiceover_lines) > _MAX_VOICEOVER_LINES:
            raise CreativeBriefError(f"voiceover_lines supera {_MAX_VOICEOVER_LINES} lineas")
        for line in self.voiceover_lines:
            self._require_non_empty("voiceover_lines", line)

    def _require_variant_count_bounded(self) -> None:
        if not 1 <= self.variant_count <= _MAX_VARIANT_COUNT:
            raise CreativeBriefError(
                f"variant_count debe estar entre 1 y {_MAX_VARIANT_COUNT}: {self.variant_count!r}"
            )

    def content_hash(self) -> str:
        """`brief_hash` de `data-model.md §CreativeAsset` (UNIQUE
        `(brief_hash, variant_index)`): identidad estable del contenido del
        brief, no de su identificador. Dos briefs con el mismo contenido
        reutilizan los activos ya renderizados de ese trabajo
        (creative-port.md §"Cola GPU")."""
        canonical = "|".join(
            (
                str(self.business_id),
                str(self.calendar_event_id),
                self.objective.value,
                self.audience_summary,
                self.hook,
                ",".join(f"{s.order}:{s.description}:{s.duration_s}" for s in self.shots),
                ",".join(self.on_screen_text),
                self.cta,
                ",".join(self.voiceover_lines),
                self.language.value,
                str(self.source_signal_id),
                str(self.variant_count),
            )
        )
        return hashlib.sha256(canonical.encode("utf-8")).hexdigest()

"""Tipos de `creative-port.md §"Tipos del dominio"`: `ImageSpec`, `VideoSpec`,
`Timeline` y sus VOs de apoyo, mas `RenderedAsset`. `Clip`, `Subtitle` y
`AudioAsset` no estan detallados en el contrato (solo se citan como
componentes de `Timeline`); se infieren con el minimo necesario para que
`VideoComposerPort.assemble` sea implementable — ver Assumptions del
informe de entrega. `BannerSpec` tampoco esta detallado (el contrato solo
dice "Playwright + plantilla HTML"); se define aqui con el mismo criterio."""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime

from safent_ads.creative.domain.brand_kit import BrandKit, SafeArea
from safent_ads.creative.domain.copy import AdCopy
from safent_ads.creative.domain.enums import Format, Language, MediaKind, RendererName
from safent_ads.creative.domain.identifiers import AssetRef
from safent_ads.creative.domain.money import Money
from safent_ads.creative.domain.storage import StorageUri

_MAX_PROMPT_LEN = 2000
_MAX_MOTION_PROMPT_LEN = 800
_MAX_VOICE_TEXT_LEN = 2000
_MAX_MUSIC_MOOD_PROMPT_LEN = 400
_MIN_SPEECH_SPEED = 0.5
_MAX_SPEECH_SPEED = 2.0


class RenderSpecError(ValueError):
    """Violacion de un invariante de una especificacion de render."""


def _require_non_empty(field_name: str, value: str, max_len: int) -> None:
    if not value.strip():
        raise RenderSpecError(f"{field_name} vacio")
    if len(value) > max_len:
        raise RenderSpecError(f"{field_name} supera {max_len} caracteres")


def _require_renderer_eligible_format(format_: Format) -> None:
    if not format_.is_renderer_eligible:
        raise RenderSpecError(
            f"{format_.value} supera la relacion de aspecto 1:3-3:1 de toda API de "
            "generacion de imagen/video: solo el compositor determinista "
            "(BannerComposerPort) puede producirlo, nunca un ImageRendererPort/"
            "VideoRendererPort"
        )


@dataclass(frozen=True, slots=True, kw_only=True)
class ImageSpec:
    prompt: str
    reference_assets: Sequence[AssetRef]
    format: Format
    seed: int | None
    brand_kit: BrandKit

    def __post_init__(self) -> None:
        _require_non_empty("prompt", self.prompt, _MAX_PROMPT_LEN)
        _require_renderer_eligible_format(self.format)


@dataclass(frozen=True, slots=True, kw_only=True)
class VideoSpec:
    key_frames: Sequence[AssetRef]
    motion_prompt: str
    duration_s: int
    format: Format
    seed: int | None

    def __post_init__(self) -> None:
        _require_non_empty("motion_prompt", self.motion_prompt, _MAX_MOTION_PROMPT_LEN)
        if self.duration_s <= 0:
            raise RenderSpecError(f"duration_s debe ser positivo: {self.duration_s!r}")
        _require_renderer_eligible_format(self.format)


@dataclass(frozen=True, slots=True, kw_only=True)
class VoiceSpec:
    """Entrada de `VoiceRendererPort.synthesize`. No detallada en
    `creative-port.md §"Tipos del dominio"` (solo se cita el metodo); se
    infiere con el minimo necesario — texto, idioma y voz de marca opcional."""

    text: str
    language: Language
    voice_id: str | None = None
    speed: float = 1.0

    def __post_init__(self) -> None:
        _require_non_empty("text", self.text, _MAX_VOICE_TEXT_LEN)
        if not _MIN_SPEECH_SPEED <= self.speed <= _MAX_SPEECH_SPEED:
            raise RenderSpecError(f"speed fuera de [{_MIN_SPEECH_SPEED}, {_MAX_SPEECH_SPEED}]")


@dataclass(frozen=True, slots=True, kw_only=True)
class MusicSpec:
    """Entrada de `MusicRendererPort.compose`, mismo criterio que `VoiceSpec`."""

    mood_prompt: str
    duration_s: int
    genre_hint: str | None = None

    def __post_init__(self) -> None:
        _require_non_empty("mood_prompt", self.mood_prompt, _MAX_MUSIC_MOOD_PROMPT_LEN)
        if self.duration_s <= 0:
            raise RenderSpecError(f"duration_s debe ser positivo: {self.duration_s!r}")


@dataclass(frozen=True, slots=True, kw_only=True)
class Subtitle:
    """Linea de subtitulo con ventana temporal (segundos desde el inicio)."""

    text: str
    start_s: float
    end_s: float

    def __post_init__(self) -> None:
        if not self.text.strip():
            raise RenderSpecError("subtitle text vacio")
        if self.end_s <= self.start_s:
            raise RenderSpecError("end_s debe ser mayor que start_s")


@dataclass(frozen=True, slots=True, kw_only=True)
class AudioAsset:
    """Pista de audio (voz o musica) ya renderizada, referenciable en un
    `Timeline`. Analogo de audio a `RenderedAsset` con menos metadatos."""

    storage_uri: StorageUri
    duration_s: float
    renderer_used: RendererName

    def __post_init__(self) -> None:
        if self.duration_s <= 0:
            raise RenderSpecError(f"duration_s debe ser positivo: {self.duration_s!r}")


@dataclass(frozen=True, slots=True, kw_only=True)
class Clip:
    """Un tramo de video en la linea de tiempo: un activo fuente mas su
    ventana de reproduccion dentro del export final."""

    source: AssetRef
    start_s: float
    end_s: float
    on_screen_text: str | None = None

    def __post_init__(self) -> None:
        if self.end_s <= self.start_s:
            raise RenderSpecError("end_s debe ser mayor que start_s")


@dataclass(frozen=True, slots=True, kw_only=True)
class Timeline:
    clips: Sequence[Clip]
    voiceover: AudioAsset | None
    music: AudioAsset | None
    subtitles: Sequence[Subtitle]
    safe_area: SafeArea
    exports: Sequence[Format]

    def __post_init__(self) -> None:
        if not self.clips:
            raise RenderSpecError("clips vacio")
        if not self.exports:
            raise RenderSpecError("exports vacio")


@dataclass(frozen=True, slots=True, kw_only=True)
class BannerSpec:
    """Entrada de `BannerComposerPort.compose`: plantilla HTML en
    `assets/creative-templates/` mas las variables que Jinja2 sustituye."""

    template_name: str
    ad_copy: AdCopy
    brand_kit: BrandKit
    background_image: AssetRef | None
    formats: Sequence[Format]

    def __post_init__(self) -> None:
        if not self.template_name.strip():
            raise RenderSpecError("template_name vacio")
        if not self.formats:
            raise RenderSpecError("formats vacio")


@dataclass(frozen=True, slots=True, kw_only=True)
class RenderedAsset:
    storage_uri: StorageUri
    media_kind: MediaKind
    format: Format
    checksum: str
    renderer_used: RendererName
    cost_estimate: Money
    duration_s: float | None
    generated_at: datetime

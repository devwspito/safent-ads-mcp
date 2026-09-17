"""`MoviePyVideoComposer` implementa `VideoComposerPort` (MoviePy 2 +
ffmpeg): concatena `Clip`s, superpone texto/subtitulos con la tipografia de
marca (reserva del sistema si el fichero de marca no existe), mezcla voz +
musica atenuada (`afx.MultiplyVolume`) y exporta al primer formato de
`Timeline.exports` — para varios formatos, el llamador invoca `assemble`
una vez por `Timeline` con `exports` de un solo elemento cada vez (T105).

`ComposedVideoRenderer` (mismo fichero, misma pila MoviePy/ffmpeg)
implementa `VideoRendererPort`: zoom/pan lento tipo Ken Burns sobre un
unico keyvisual. Correccion final del propietario 2026-09-09: "a genuine,
always-available video capability, not a fallback for a missing key" —
no depende de ninguna clave de proveedor ni de que el agente delegue nada,
por eso se registra en `RendererTier.DIRECT_PROVIDER` con prioridad sobre
los generativos (`renderer_selector.py`). `VideoSpec` (creative-port.md)
no lleva locucion ni subtitulos — esos siguen siendo trabajo de
`VideoComposerPort`/`Timeline` (montaje autorado a mano); esta clase no lo
duplica, solo anade una fuente de video mas alli donde antes solo habia
generativos o nada."""

from __future__ import annotations

import asyncio
import hashlib
import io
from datetime import UTC, datetime
from pathlib import Path
from tempfile import TemporaryDirectory

from moviepy import (
    AudioFileClip,
    CompositeAudioClip,
    CompositeVideoClip,
    ImageClip,
    TextClip,
    VideoFileClip,
    afx,
    concatenate_videoclips,
)
from moviepy.audio.AudioClip import AudioClip as MoviePyAudioClip
from moviepy.Clip import Clip as MoviePyClip
from PIL import Image

from safent_ads.creative.application.errors import CreativeAssetNotFoundError
from safent_ads.creative.application.ports import (
    AssetRetrievalPort,
    AssetStorePort,
    CreativeAssetRepository,
)
from safent_ads.creative.domain.creative_asset import CreativeAsset
from safent_ads.creative.domain.enums import Format, MediaKind, RendererName
from safent_ads.creative.domain.identifiers import AssetRef
from safent_ads.creative.domain.image_crop import crop_to_format
from safent_ads.creative.domain.money import Money
from safent_ads.creative.domain.render_specs import (
    AudioAsset,
    Clip,
    RenderedAsset,
    Timeline,
    VideoSpec,
)
from safent_ads.shared.errors import InfrastructureError

_MUSIC_DUCK_VOLUME = 0.22
_EXPORT_FPS = 24
_HEADLINE_FONT_SIZE = 48
_SUBTITLE_FONT_SIZE = 36
_STROKE_WIDTH = 2
_LOCAL_RENDER_COST = Money.zero("USD")
_KEN_BURNS_TOTAL_ZOOM = 0.06  # crecimiento total sobre la duracion del clip: lento, no llamativo


class VideoComposeError(InfrastructureError):
    """Fallo componiendo el timeline: activo ausente, ffmpeg, etc."""


class MoviePyVideoComposer:
    def __init__(
        self,
        assets: CreativeAssetRepository,
        asset_retrieval: AssetRetrievalPort,
        asset_store: AssetStorePort,
        brand_font_path: Path,
        fallback_font_path: Path,
    ) -> None:
        self._assets = assets
        self._asset_retrieval = asset_retrieval
        self._asset_store = asset_store
        self._brand_font_path = brand_font_path
        self._fallback_font_path = fallback_font_path

    async def assemble(self, timeline: Timeline) -> RenderedAsset:
        if not timeline.exports:
            raise VideoComposeError("Timeline.exports vacio")
        export_format = timeline.exports[0]
        with TemporaryDirectory(prefix="safent-ads-video-") as raw_tmp_dir:
            tmp_dir = Path(raw_tmp_dir)
            video_clip = await self._build_video_track(timeline, export_format, tmp_dir)
            audio_clip = await self._build_audio_track(timeline, tmp_dir, video_clip.duration)
            if audio_clip is not None:
                video_clip = video_clip.with_audio(audio_clip)
            payload = await self._export_to_bytes(video_clip, tmp_dir, audio_clip is not None)
        return await self._to_rendered_asset(payload, export_format, video_clip.duration)

    async def _export_to_bytes(
        self, video_clip: MoviePyClip, tmp_dir: Path, has_audio: bool
    ) -> bytes:
        output_path = tmp_dir / "output.mp4"
        await asyncio.to_thread(
            video_clip.write_videofile,
            str(output_path),
            fps=_EXPORT_FPS,
            codec="libx264",
            audio_codec="aac" if has_audio else None,
            logger=None,
        )
        return await asyncio.to_thread(output_path.read_bytes)

    async def _to_rendered_asset(
        self, payload: bytes, export_format: Format, duration_s: float
    ) -> RenderedAsset:
        checksum = hashlib.sha256(payload).hexdigest()
        storage_uri = await self._asset_store.put(payload, MediaKind.VIDEO)
        return RenderedAsset(
            storage_uri=storage_uri,
            media_kind=MediaKind.VIDEO,
            format=export_format,
            checksum=checksum,
            renderer_used=RendererName.MOVIEPY_VIDEO_COMPOSER,
            cost_estimate=_LOCAL_RENDER_COST,
            duration_s=duration_s,
            generated_at=datetime.now(UTC),
        )

    async def _build_video_track(
        self, timeline: Timeline, export_format: Format, tmp_dir: Path
    ) -> MoviePyClip:
        segments = [
            await self._build_segment(clip, index, tmp_dir)
            for index, clip in enumerate(timeline.clips)
        ]
        composite = concatenate_videoclips(segments, method="compose")
        composite = composite.resized(new_size=(export_format.width, export_format.height))
        return self._overlay_subtitles(composite, timeline)

    async def _build_segment(self, clip: Clip, index: int, tmp_dir: Path) -> MoviePyClip:
        asset = await self._require_asset(clip.source)
        local_path = await self._download_to_tmp(asset, tmp_dir, index)
        duration = clip.end_s - clip.start_s
        base = self._load_clip(asset, local_path, duration)
        if clip.on_screen_text:
            base = self._overlay_text(base, clip.on_screen_text, duration)
        return base

    async def _require_asset(self, asset_ref: AssetRef) -> CreativeAsset:
        asset = await self._assets.get(asset_ref)
        if asset is None:
            raise CreativeAssetNotFoundError(str(asset_ref))
        return asset

    async def _download_to_tmp(self, asset: CreativeAsset, tmp_dir: Path, index: int) -> Path:
        payload = await self._asset_retrieval.get(asset.storage_uri)
        extension = ".mp4" if asset.media_kind == MediaKind.VIDEO else ".png"
        local_path = tmp_dir / f"segment-{index}{extension}"
        await asyncio.to_thread(local_path.write_bytes, payload)
        return local_path

    def _load_clip(self, asset: CreativeAsset, local_path: Path, duration: float) -> MoviePyClip:
        if asset.media_kind == MediaKind.VIDEO:
            return VideoFileClip(str(local_path)).subclipped(0, duration)
        return ImageClip(str(local_path)).with_duration(duration)

    def _font_path(self) -> Path:
        return self._brand_font_path if self._brand_font_path.exists() else self._fallback_font_path

    def _overlay_text(self, base: MoviePyClip, text: str, duration: float) -> MoviePyClip:
        text_clip = TextClip(
            font=str(self._font_path()),
            text=text,
            font_size=_HEADLINE_FONT_SIZE,
            color="white",
            stroke_color="black",
            stroke_width=_STROKE_WIDTH,
            duration=duration,
        ).with_position(("center", "bottom"))
        return CompositeVideoClip([base, text_clip])

    def _overlay_subtitles(self, composite: MoviePyClip, timeline: Timeline) -> MoviePyClip:
        if not timeline.subtitles:
            return composite
        subtitle_clips = [
            TextClip(
                font=str(self._font_path()),
                text=subtitle.text,
                font_size=_SUBTITLE_FONT_SIZE,
                color="white",
                duration=subtitle.end_s - subtitle.start_s,
            )
            .with_start(subtitle.start_s)
            .with_position(("center", "bottom"))
            for subtitle in timeline.subtitles
        ]
        return CompositeVideoClip([composite, *subtitle_clips])

    async def _build_audio_track(
        self, timeline: Timeline, tmp_dir: Path, video_duration: float
    ) -> MoviePyAudioClip | None:
        voice_clip = await self._load_audio(timeline.voiceover, tmp_dir, "voice")
        music_clip = await self._load_audio(timeline.music, tmp_dir, "music")
        if music_clip is not None:
            music_clip = music_clip.with_effects([afx.MultiplyVolume(_MUSIC_DUCK_VOLUME)])
        tracks = [track for track in (voice_clip, music_clip) if track is not None]
        if not tracks:
            return None
        return CompositeAudioClip(tracks).subclipped(0, video_duration)

    async def _load_audio(
        self, audio: AudioAsset | None, tmp_dir: Path, name: str
    ) -> MoviePyAudioClip | None:
        if audio is None:
            return None
        payload = await self._asset_retrieval.get(audio.storage_uri)
        local_path = tmp_dir / f"{name}.mp3"
        await asyncio.to_thread(local_path.write_bytes, payload)
        return AudioFileClip(str(local_path))


class ComposedVideoRendererError(InfrastructureError):
    """`VideoSpec.key_frames` vacio, o el fotograma referenciado no existe."""


class ComposedVideoRenderer:
    """`VideoRendererPort` sobre un unico keyvisual con zoom lento tipo Ken
    Burns (ver docstring del modulo). CPU-only, sin credencial: siempre
    puede completar."""

    def __init__(
        self,
        assets: CreativeAssetRepository,
        asset_retrieval: AssetRetrievalPort,
        asset_store: AssetStorePort,
    ) -> None:
        self.name = RendererName.KEN_BURNS_VIDEO_COMPOSER
        self._assets = assets
        self._asset_retrieval = asset_retrieval
        self._asset_store = asset_store

    async def render(self, spec: VideoSpec) -> RenderedAsset:
        if not spec.key_frames:
            raise ComposedVideoRendererError("VideoSpec.key_frames vacio: Ken Burns exige uno")
        with TemporaryDirectory(prefix="safent-ads-ken-burns-") as raw_tmp_dir:
            tmp_dir = Path(raw_tmp_dir)
            local_path = await self._prepare_key_frame(spec, tmp_dir)
            clip = self._build_zoomed_clip(local_path, spec)
            payload = await self._export_to_bytes(clip, tmp_dir)
        return await self._to_rendered_asset(payload, spec)

    async def _prepare_key_frame(self, spec: VideoSpec, tmp_dir: Path) -> Path:
        """Descarga el fotograma clave y lo recorta/reescala (misma
        `domain/image_crop.py` que `OpenAiImageRenderer`) a la relacion de
        aspecto EXACTA de `spec.format`, para que el zoom Ken Burns nunca
        deforme la imagen ni deje bordes en blanco."""
        asset = await self._assets.get(spec.key_frames[0])
        if asset is None:
            raise CreativeAssetNotFoundError(str(spec.key_frames[0]))
        raw_payload = await self._asset_retrieval.get(asset.storage_uri)
        local_path = tmp_dir / "keyframe.png"
        await asyncio.to_thread(self._crop_and_write, raw_payload, spec.format, local_path)
        return local_path

    def _crop_and_write(self, raw_payload: bytes, format_: Format, local_path: Path) -> None:
        with Image.open(io.BytesIO(raw_payload)) as opened:
            rgb_image = opened.convert("RGB")
        box = crop_to_format(rgb_image.width, rgb_image.height, format_)
        cropped = rgb_image.crop((box.left, box.top, box.left + box.width, box.top + box.height))
        resized = cropped.resize((format_.width, format_.height), Image.Resampling.LANCZOS)
        resized.save(local_path, format="PNG")

    def _build_zoomed_clip(self, local_path: Path, spec: VideoSpec) -> MoviePyClip:
        duration = float(spec.duration_s)
        base = ImageClip(str(local_path)).with_duration(duration)
        zoomed = base.resized(lambda t: 1.0 + _KEN_BURNS_TOTAL_ZOOM * (t / duration))
        zoomed = zoomed.with_position("center")
        return CompositeVideoClip(
            [zoomed], size=(spec.format.width, spec.format.height)
        ).with_duration(duration)

    async def _export_to_bytes(self, clip: MoviePyClip, tmp_dir: Path) -> bytes:
        output_path = tmp_dir / "output.mp4"
        await asyncio.to_thread(
            clip.write_videofile,
            str(output_path),
            fps=_EXPORT_FPS,
            codec="libx264",
            audio=False,
            logger=None,
        )
        return await asyncio.to_thread(output_path.read_bytes)

    async def _to_rendered_asset(self, payload: bytes, spec: VideoSpec) -> RenderedAsset:
        checksum = hashlib.sha256(payload).hexdigest()
        storage_uri = await self._asset_store.put(payload, MediaKind.VIDEO)
        return RenderedAsset(
            storage_uri=storage_uri,
            media_kind=MediaKind.VIDEO,
            format=spec.format,
            checksum=checksum,
            renderer_used=self.name,
            cost_estimate=_LOCAL_RENDER_COST,
            duration_s=float(spec.duration_s),
            generated_at=datetime.now(UTC),
        )

"""`HermesToolRenderer` implementa `ImageRendererPort`, `VideoRendererPort`
y `VoiceRendererPort` (la "TtsPort" del encargo) delegando en las
herramientas nativas del agente Hermes (`image_generate`, `video_generate`,
`text_to_speech` — tool-surface.md §2.2, §5.2) en vez de en un modelo que
Safent aloje o pague.

## Por que esta forma de delegacion y no otras dos mas directas

1. **"El servicio invoca la herramienta nativa directamente"** — imposible
   hoy: `ads-api`/`ads-worker` son procesos separados del agente que hablan
   por MCP (`npx mcp-remote`); no hay ruta para que este backend llame a
   una herramienta in-process de Hermes de forma sincrona
   (research/creative-via-codex.md §(b): "nuestro servicio no puede llamar
   las herramientas in-process de Hermes").

2. **"Reenviar la llamada del agente a `image_generate` dentro de
   `generate_creative_assets`"** (forma sugerida originalmente en el
   encargo) — rechazada por su propio informe de investigacion: rompe la
   serializacion de `GpuLeasePort` y el resultado no es determinista,
   porque el backend de Codex ignora `tool_choice` y el modelo puede
   declinar sin que el llamador se entere
   (research/creative-via-codex.md §(a), fila "Forzar que se genere la
   imagen").

3. **Lo que se implementa aqui**: `render()`/`synthesize()` nunca
   completan un render por si mismos. Construyen una
   `DelegationInstruction` — herramienta nativa + argumentos deterministas
   y sin datos personales — y lanzan `RendererDelegationRequiredError`
   llevandola. `GenerateCreativeAssets._render_with_fallback` la atrapa,
   registra la instruccion para trazabilidad (FR-34) y pasa al siguiente
   candidato de `RendererSelector.candidates_for` (proveedor directo si
   hay clave configurada; si no, `ImageGenerationUnavailableError` con un
   mensaje accionable, nunca una degradacion silenciosa).

   El activo que produzca de verdad la herramienta nativa vuelve por un
   camino EXPLICITO y distinto: `import_creative_asset`
   (`application/import_creative_asset.py`), que el agente invoca con la
   URL que le devolvio su propia herramienta. Dos tools MCP nuestras
   (`generate_creative_assets` que devuelve la instruccion,
   `import_creative_asset` que cierra el circulo) en vez de una caja negra
   donde el agente genera por su cuenta sin que `creative` se entere.

## Generico, no atado a un proveedor

Correccion del propietario 2026-09-09: "whether the LLM chosen as
Safent's brain can generate images is the user's responsibility... ask the
configured backend what it supports and adapt". Los 8 backends de
`image_gen` de Hermes (`openai`, `openai-codex`, `xai`, `meta-ai`, `krea`,
`deepinfra`, `openrouter`, `fal`) comparten una capa comun de aspecto
(`agent/image_gen_provider.py` `VALID_ASPECT_RATIOS = ("landscape",
"square", "portrait")`): es la unica entrada de forma que no depende de
cual backend eligio el propietario. Por eso `DelegationInstruction` pide
un `aspect_ratio` semantico, nunca un tamano en pixeles — el tamano exacto
de anuncio (recorte, reescalado) es responsabilidad de
`domain/image_crop.py` sobre las dimensiones REALES del activo que vuelva,
no de una tabla fija. `video_generate` no tiene esa capa comun (Hermes
solo trae `fal`/`xai`/`deepinfra` en video, sin Sora/Veo directos): se
pide igual por aspecto/duracion; si el backend configurado no genera
video, la instruccion vuelve igual y es el propio agente quien reporta la
ausencia al propietario — este adaptador nunca finge un resultado."""

from __future__ import annotations

from safent_ads.creative.application.delegation import (
    DelegationInstruction,
    RendererDelegationRequiredError,
)
from safent_ads.creative.domain.enums import RendererName
from safent_ads.creative.domain.render_specs import (
    AudioAsset,
    ImageSpec,
    RenderedAsset,
    VideoSpec,
    VoiceSpec,
)

__all__ = ["HermesToolRenderer", "RendererDelegationRequiredError"]

_ASPECT_RATIO_BANDS: tuple[tuple[float, float, str], ...] = (
    (0.0, 0.85, "portrait"),
    (0.85, 1.2, "square"),
    (1.2, float("inf"), "landscape"),
)

_IMAGE_NATIVE_TOOL = "image_generate"
_VIDEO_NATIVE_TOOL = "video_generate"
_VOICE_NATIVE_TOOL = "text_to_speech"


def _aspect_ratio_for(width: int, height: int) -> str:
    ratio = width / height
    for low, high, aspect in _ASPECT_RATIO_BANDS:
        if low <= ratio < high:
            return aspect
    raise AssertionError(f"banda de aspecto no cubierta para ratio={ratio!r}")


class HermesToolRenderer:
    """Un unico adaptador para los tres puertos generativos: la forma de
    delegacion (construir instruccion, lanzar
    `RendererDelegationRequiredError`) es identica para imagen, video y
    voz — solo cambian la herramienta nativa y los argumentos."""

    def __init__(self) -> None:
        self.name = RendererName.HERMES_NATIVE_DELEGATED

    async def render(self, spec: ImageSpec | VideoSpec) -> RenderedAsset:
        if isinstance(spec, VideoSpec):
            raise RendererDelegationRequiredError(self._video_instruction(spec))
        raise RendererDelegationRequiredError(self._image_instruction(spec))

    async def synthesize(self, spec: VoiceSpec) -> AudioAsset:
        raise RendererDelegationRequiredError(self._voice_instruction(spec))

    def _image_instruction(self, spec: ImageSpec) -> DelegationInstruction:
        aspect = _aspect_ratio_for(spec.format.width, spec.format.height)
        return DelegationInstruction(
            native_tool=_IMAGE_NATIVE_TOOL,
            arguments={"prompt": spec.prompt, "aspect_ratio": aspect},
        )

    def _video_instruction(self, spec: VideoSpec) -> DelegationInstruction:
        aspect = _aspect_ratio_for(spec.format.width, spec.format.height)
        return DelegationInstruction(
            native_tool=_VIDEO_NATIVE_TOOL,
            arguments={
                "prompt": spec.motion_prompt,
                "aspect_ratio": aspect,
                "duration_s": spec.duration_s,
            },
        )

    def _voice_instruction(self, spec: VoiceSpec) -> DelegationInstruction:
        return DelegationInstruction(
            native_tool=_VOICE_NATIVE_TOOL,
            arguments={"text": spec.text, "language": spec.language.value},
        )

"""`RendererSelector` (plan.md §5 `creative`): estrategia de eleccion de
renderizador declarada como datos — una tabla de `RendererDescriptor`, no
una cadena de `if` anidados (creative-port.md §"Seleccion de renderizador").

**Orden invertido 2026-09-09** (research/creative-via-codex.md §(b)-(c),
`tool-surface.md` §0, y la correccion final del propietario): delegado
primero (herramienta nativa del agente, ya cubierta por la suscripcion),
proveedor directo despues (BYOK, solo si esta configurado), local al final
y deshabilitado por defecto (`CREATIVE_LOCAL_ENABLED=false` —
`build_registry` omite estructuralmente los descriptores `LOCAL_GPU` salvo
que se pida explicitamente, mismo patron que el filtro de licencia: la
ausencia es el control, no una comprobacion en caliente).

Filtro duro de licencia: solo se registran renderizadores con
`commercial_use = True`. Los descartados (NarratoAI, F5-TTS, Fish, XTTS,
MusicGen, FLUX.2-dev, SD3.5) no aparecen aqui — la ausencia es el control,
no una comprobacion en tiempo de ejecucion."""

from __future__ import annotations

from dataclasses import dataclass

from safent_ads.creative.domain.enums import RendererIntent, RendererName, RendererTier

_TIER_PRIORITY: dict[RendererTier, int] = {
    RendererTier.DELEGATED: 0,
    RendererTier.DIRECT_PROVIDER: 1,
    RendererTier.LOCAL_GPU: 2,
}


class NoRendererAvailableError(LookupError):
    """Ningun renderizador registrado cubre la intencion pedida."""


class LocalRenderingDisabledError(LookupError):
    """Se pidio construir el registro con renderizadores `LOCAL_GPU` sin
    habilitar `local_enabled` explicitamente. `infrastructure/comfyui_adapter.py`
    aplica el mismo cierre en falso al construir el adaptador en si — esta
    comprobacion cubre el registro de seleccion, esa cubre la instanciacion."""


@dataclass(frozen=True, slots=True)
class RendererDescriptor:
    name: RendererName
    commercial_use: bool
    tier: RendererTier
    intents: frozenset[RendererIntent]

    def __post_init__(self) -> None:
        if not self.commercial_use:
            raise ValueError(
                f"{self.name}: solo se registran renderizadores de uso comercial"
            )


# research/creative-via-codex.md §(c) "No implementar": no existe un
# renderizador que llame a la API de OpenAI/fal.ai *como si fuera* la
# suscripcion Codex — DELEGATED es honesto: nunca completa el render el
# mismo (RendererDelegationRequiredError), solo abre la puerta a que el
# agente use su herramienta nativa y el resultado vuelva por
# `import_creative_asset`.
_DELEGATED_DESCRIPTORS: tuple[RendererDescriptor, ...] = (
    RendererDescriptor(
        name=RendererName.HERMES_NATIVE_DELEGATED,
        commercial_use=True,
        tier=RendererTier.DELEGATED,
        intents=frozenset(
            {RendererIntent.IMAGE_WITH_TEXT, RendererIntent.IMAGE_PRODUCT_BACKGROUND}
        ),
    ),
)

# Sin clave de proveedor provisionada (correccion del propietario
# 2026-09-09), `GPT_IMAGE_1_5`/`VEO_3_1_FAST`/`WAN_2_2` quedan declarados
# pero normalmente sin adaptador inyectado en `composition` —
# `RendererSelector` los ordena de todos modos porque el dia que el
# propietario tenga una clave, activarlos es cablear el adaptador, no tocar
# este modulo. `KEN_BURNS_VIDEO_COMPOSER` es distinto: CPU-only, siempre
# disponible, sin credencial ("a genuine, always-available video
# capability, not a fallback for a missing key") — se lista antes de los
# generativos por ser gratis y determinista.
#
# `FLUX2_KLEIN_9B` precede a `GPT_IMAGE_1_5` a proposito (encargo del
# propietario 2026-09-15, "total parity"): es el MISMO proveedor+modelo que
# Hermes ya usa por defecto para generar imagenes, asi que es la primera
# preferencia de proveedor directo; `GPT_IMAGE_1_5` sigue disponible como
# alternativa cuando el propietario configura `OPENAI_API_KEY` en su lugar
# o ademas.
_DIRECT_PROVIDER_DESCRIPTORS: tuple[RendererDescriptor, ...] = (
    RendererDescriptor(
        name=RendererName.KEN_BURNS_VIDEO_COMPOSER,
        commercial_use=True,
        tier=RendererTier.DIRECT_PROVIDER,
        intents=frozenset({RendererIntent.VIDEO_VERTICAL_FAST}),
    ),
    RendererDescriptor(
        name=RendererName.FLUX2_KLEIN_9B,
        commercial_use=True,
        tier=RendererTier.DIRECT_PROVIDER,
        intents=frozenset(
            {RendererIntent.IMAGE_WITH_TEXT, RendererIntent.IMAGE_PRODUCT_BACKGROUND}
        ),
    ),
    RendererDescriptor(
        name=RendererName.GPT_IMAGE_1_5,
        commercial_use=True,
        tier=RendererTier.DIRECT_PROVIDER,
        intents=frozenset(
            {RendererIntent.IMAGE_WITH_TEXT, RendererIntent.IMAGE_PRODUCT_BACKGROUND}
        ),
    ),
    RendererDescriptor(
        name=RendererName.VEO_3_1_FAST,
        commercial_use=True,
        tier=RendererTier.DIRECT_PROVIDER,
        intents=frozenset(
            {RendererIntent.VIDEO_VERTICAL_FAST, RendererIntent.VIDEO_MOTION_FIDELITY}
        ),
    ),
    RendererDescriptor(
        name=RendererName.WAN_2_2,
        commercial_use=True,
        tier=RendererTier.DIRECT_PROVIDER,
        intents=frozenset(
            {RendererIntent.VIDEO_VERTICAL_FAST, RendererIntent.VIDEO_MOTION_FIDELITY}
        ),
    ),
    RendererDescriptor(
        name=RendererName.CHATTERBOX_ES_ES,
        commercial_use=True,
        tier=RendererTier.DIRECT_PROVIDER,
        intents=frozenset({RendererIntent.VOICE_ES}),
    ),
    RendererDescriptor(
        name=RendererName.ACE_STEP_1_5,
        commercial_use=True,
        tier=RendererTier.DIRECT_PROVIDER,
        intents=frozenset({RendererIntent.MUSIC}),
    ),
)

# `infra/creative/` dormido a proposito (D7, threat-model.md C-29): 4
# caidas termicas el 9-sep-2026 (0%->96% de uso, 70->94 C en 1-2 minutos).
# `build_registry(local_enabled=True)` es la unica puerta de entrada.
_LOCAL_GPU_DESCRIPTORS: tuple[RendererDescriptor, ...] = (
    RendererDescriptor(
        name=RendererName.QWEN_IMAGE_2512,
        commercial_use=True,
        tier=RendererTier.LOCAL_GPU,
        intents=frozenset({RendererIntent.IMAGE_WITH_TEXT}),
    ),
    RendererDescriptor(
        name=RendererName.FLUX2_KLEIN,
        commercial_use=True,
        tier=RendererTier.LOCAL_GPU,
        intents=frozenset({RendererIntent.IMAGE_PRODUCT_BACKGROUND}),
    ),
    RendererDescriptor(
        name=RendererName.LTX_2_5,
        commercial_use=True,
        tier=RendererTier.LOCAL_GPU,
        intents=frozenset({RendererIntent.VIDEO_VERTICAL_FAST}),
    ),
)


def build_registry(*, local_enabled: bool = False) -> tuple[RendererDescriptor, ...]:
    """Unico punto de construccion del registro por defecto. `local_enabled`
    debe llegar de `CreativeSettings.creative_local_enabled`
    (`ADS_CREATIVE_LOCAL_ENABLED`, por defecto `false`); sin el, los
    descriptores `LOCAL_GPU` estan estructuralmente ausentes — no hay nada
    que comprobar en caliente porque no hay nada que elegir."""
    if not local_enabled:
        return _DELEGATED_DESCRIPTORS + _DIRECT_PROVIDER_DESCRIPTORS
    return _DELEGATED_DESCRIPTORS + _DIRECT_PROVIDER_DESCRIPTORS + _LOCAL_GPU_DESCRIPTORS


_DEFAULT_REGISTRY: tuple[RendererDescriptor, ...] = build_registry(local_enabled=False)


# Nombre de modelo/checkpoint para provenance (FR-34), distinto del
# `RendererName` categorico. `infra/creative/workflows/README.md` (postmortem
# 2026-09-09): el checkpoint real de Qwen es el merge con LoRA horneada, no
# el `_scaled` plano con `LoraLoaderModelOnly` separado.
MODEL_NAME_BY_RENDERER: dict[RendererName, str] = {
    RendererName.QWEN_IMAGE_2512: "qwen-image-2512-lightning-4step",
    RendererName.FLUX2_KLEIN: "flux2-klein-4b",
    RendererName.FLUX2_KLEIN_9B: "flux-2-klein-9b",
    RendererName.GPT_IMAGE_1_5: "gpt-image-1.5",
    RendererName.LTX_2_5: "ltx-2.5-distilled",
    RendererName.WAN_2_2: "wan-2.2-a14b-lightning",
    RendererName.VEO_3_1_FAST: "veo-3.1-fast",
    RendererName.CHATTERBOX_ES_ES: "chatterbox-multilingual-v3-es-es",
    RendererName.ACE_STEP_1_5: "ace-step-1.5",
    RendererName.HTML_BANNER_COMPOSER: "jinja2-playwright-banner-composer",
    RendererName.MOVIEPY_VIDEO_COMPOSER: "moviepy2-ffmpeg-video-composer",
    RendererName.HERMES_NATIVE_DELEGATED: "hermes-native-tool (delegado, sin modelo fijo)",
    RendererName.KEN_BURNS_VIDEO_COMPOSER: "moviepy2-ffmpeg-ken-burns",
}


class RendererSelector:
    """Servicio de dominio: ordena los `RendererName` candidatos para una
    intencion por `RendererTier` (delegado -> proveedor directo -> local).
    Sin estado mutable; el registro es inmutable y se inyecta en el
    constructor para poder probar variantes en tests."""

    def __init__(self, registry: tuple[RendererDescriptor, ...] = _DEFAULT_REGISTRY) -> None:
        self._registry = registry

    def candidates_for(self, intent: RendererIntent) -> tuple[RendererName, ...]:
        """Todos los `RendererName` registrados para `intent`, ordenados de
        mayor a menor prioridad. El llamante (`GenerateCreativeAssets`)
        recorre esta lista probando cada uno hasta que alguno complete o se
        agoten — nunca un `if`/`else` de dos saltos fijo."""
        candidates = [d for d in self._registry if intent in d.intents]
        if not candidates:
            raise NoRendererAvailableError(f"sin renderizador registrado para {intent}")
        ordered = sorted(candidates, key=lambda d: _TIER_PRIORITY[d.tier])
        return tuple(d.name for d in ordered)

    def select(self, intent: RendererIntent) -> RendererName:
        """Atajo sobre `candidates_for` para el llamante que solo quiere la
        primera preferencia (p.ej. estimar coste antes de encolar)."""
        return self.candidates_for(intent)[0]

    def is_registered(self, name: RendererName) -> bool:
        return any(d.name == name for d in self._registry)

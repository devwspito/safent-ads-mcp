"""Enumeraciones cerradas del contexto `creative`. Declaradas como datos, no
como ramas `if` (creative-port.md §"Seleccion de renderizador")."""

from __future__ import annotations

from enum import StrEnum

_MAX_GENERATIVE_ASPECT_RATIO = 3.0


class Language(StrEnum):
    """NFR-12: las creatividades se generan en espanol natural. Alcance v1
    = el negocio del propietario (spec.md Assumptions §6): un unico idioma
    soportado."""

    ES_ES = "es-ES"


class CampaignObjective(StrEnum):
    """Objetivo comercial del brief, alineado con `ConversionKind` de
    `data-model.md §LeadAttribution` mas `AWARENESS` para tope de embudo."""

    LEAD = "lead"
    WHATSAPP_CLICK = "whatsapp_click"
    CALL = "call"
    BUSINESS_CONVERSION = "business_conversion"
    AWARENESS = "awareness"


class MediaKind(StrEnum):
    """`data-model.md §CreativeAsset`: `image|video|banner|audio`."""

    IMAGE = "image"
    VIDEO = "video"
    BANNER = "banner"
    AUDIO = "audio"


class AssetKind(StrEnum):
    """Subconjunto de `MediaKind` que admite un tamano/preset visual
    (excluye `AUDIO`, que no tiene ancho/alto)."""

    IMAGE = "image"
    VIDEO = "video"
    BANNER = "banner"


class Format(StrEnum):
    """Tamanos soportados. `creative-port.md` declara 1080x1920, 1080x1080,
    1200x628, 300x250, 728x90 como "formatos soportados"; se anaden
    1080x1350 (formato feed 4:5 que ya emite `image_text_qwen.json`,
    infra/creative/workflows/README.md) y 320x50 (banner movil estandar
    IAB, misma familia que 300x250/728x90) para cubrir los 7 presets que
    pide `AssetSpec`."""

    SQUARE_1080 = "1080x1080"
    PORTRAIT_FEED_1080X1350 = "1080x1350"
    STORY_1080X1920 = "1080x1920"
    LINK_1200X628 = "1200x628"
    MEDIUM_RECTANGLE_300X250 = "300x250"
    LEADERBOARD_728X90 = "728x90"
    MOBILE_LEADERBOARD_320X50 = "320x50"

    @property
    def is_renderer_eligible(self) -> bool:
        """Toda API de generacion de imagen consultada
        (research/creative-via-codex.md) exige una relacion de aspecto
        entre 1:3 y 3:1: `728x90` (8.09:1) y `320x50` (6.4:1) la superan y
        son estructuralmente imposibles de generar, con cualquier
        proveedor. Calculado, no una lista fija, para que valga con
        cualquier `Format` que se añada despues."""
        ratio = max(self.width, self.height) / min(self.width, self.height)
        return ratio <= _MAX_GENERATIVE_ASPECT_RATIO

    @property
    def width(self) -> int:
        return int(self.value.split("x")[0])

    @property
    def height(self) -> int:
        return int(self.value.split("x")[1])


class VideoDurationSeconds(StrEnum):
    """Duraciones cerradas para video corto (AssetSpec)."""

    SIX = "6"
    TEN = "10"
    FIFTEEN = "15"

    @property
    def seconds(self) -> int:
        return int(self.value)


class CallToAction(StrEnum):
    """Identificadores en ingles (plan.md §13 Assumption 3); la etiqueta
    visible se resuelve via `label_es()` en `copy.py`."""

    LEARN_MORE = "learn_more"
    SIGN_UP = "sign_up"
    APPLY_NOW = "apply_now"
    CALL_NOW = "call_now"
    MESSAGE_WHATSAPP = "message_whatsapp"
    GET_INFO = "get_info"


class RendererName(StrEnum):
    """`creative-port.md`: unicamente renderizadores con `commercial_use =
    true`. Los descartados por licencia (NarratoAI, F5-TTS, Fish, XTTS,
    MusicGen, FLUX.2-dev, SD3.5) no tienen miembro aqui: la ausencia es la
    comprobacion, no un chequeo en caliente."""

    QWEN_IMAGE_2512 = "qwen_image_2512"
    FLUX2_KLEIN = "flux2_klein"
    # Encargo del propietario 2026-09-15 (paridad Safent/MCP alojado):
    # `fal-ai/flux-2/klein/9b` sobre la API de cola de fal.ai -- el MISMO
    # proveedor y modelo que usa Hermes (motor de Safent) para generar
    # imagenes. Miembro propio, distinto de `FLUX2_KLEIN` (checkpoint 4B
    # de `ComfyUiImageRenderer`, LOCAL_GPU): mismo modelo base, tier y
    # credencial distintos.
    FLUX2_KLEIN_9B = "flux2_klein_9b"
    GPT_IMAGE_1_5 = "gpt_image_1_5"
    LTX_2_5 = "ltx_2_5"
    WAN_2_2 = "wan_2_2"
    VEO_3_1_FAST = "veo_3_1_fast"
    CHATTERBOX_ES_ES = "chatterbox_es_es"
    ACE_STEP_1_5 = "ace_step_1_5"
    # `hermes_tool_renderer.py`: nivel RendererTier.DELEGATED. Marca tanto el
    # intento de delegacion (que hoy siempre falla con
    # RendererDelegationRequiredError, research/creative-via-codex.md §(c))
    # como, via `import_creative_asset`, el activo que el agente trajo de
    # verdad usando una herramienta nativa de Hermes (image_generate,
    # video_generate, text_to_speech) que el propietario ya tiene habilitada
    # por su suscripcion — sin coste de API adicional para Safent.
    HERMES_NATIVE_DELEGATED = "hermes_native_delegated"
    # Composicion deterministica (Jinja2+Playwright, MoviePy+ffmpeg): no son
    # modelos generativos, pero `RenderedAsset.renderer_used` exige un
    # `RendererName` para toda pieza, generativa o compuesta (FR-34).
    HTML_BANNER_COMPOSER = "html_banner_composer"
    MOVIEPY_VIDEO_COMPOSER = "moviepy_video_composer"
    # `ComposedVideoRenderer` (VideoRendererPort): ken-burns de un solo
    # keyvisual, siempre disponible, CPU-only. Distinto de
    # MOVIEPY_VIDEO_COMPOSER (VideoComposerPort, Timeline autorada a mano)
    # para que FR-34 distinga que camino de codigo produjo cada pieza.
    KEN_BURNS_VIDEO_COMPOSER = "ken_burns_video_composer"


class RendererTier(StrEnum):
    """Prioridad de seleccion de `RendererSelector` (creative-port.md
    §"Seleccion de renderizador", invertido research/creative-via-codex.md
    §(b)-(c) y la correccion del propietario 2026-09-09: sin clave de
    proveedor provisionada, el unico credito real es la suscripcion
    Codex/ChatGPT que ya trae herramientas nativas de generacion. Orden:

    1. DELEGATED — herramienta nativa del agente (`image_generate`,
       `video_generate`, `text_to_speech`); coste ya cubierto por la
       suscripcion, pero el resultado no es determinista ni sincrono
       (research/creative-via-codex.md §(a): sin `tool_choice` forzado).
    2. DIRECT_PROVIDER — adaptador HTTP propio contra la API de pago de un
       proveedor (BYOK); solo se selecciona si esta configurado.
    3. LOCAL_GPU — ComfyUI en la DGX; deshabilitado por defecto
       (`CREATIVE_LOCAL_ENABLED=false`), tumbo la maquina 4 veces el
       9-sep-2026 por temperatura."""

    DELEGATED = "delegated"
    DIRECT_PROVIDER = "direct_provider"
    LOCAL_GPU = "local_gpu"


class GenerationStatus(StrEnum):
    """Provenencia honesta de la capa visual de un `CreativeAsset`
    (correccion del propietario 2026-09-09: "no silent degradation, no
    pretending an asset was produced"). `MODEL_GENERATED` cubre tanto un
    render directo como uno importado de una herramienta nativa del agente;
    `COMPOSER_ONLY_FALLBACK` marca una pieza compuesta sobre la plantilla de
    marca sin ningun visual generado por modelo (porque no se pidio uno, o
    porque la generacion no estaba disponible/fue rechazada) — nunca se
    presenta como si llevara un fondo generado."""

    MODEL_GENERATED = "model_generated"
    COMPOSER_ONLY_FALLBACK = "composer_only_fallback"


class RendererIntent(StrEnum):
    """Que necesita generarse; entrada del `RendererSelector`."""

    IMAGE_WITH_TEXT = "image_with_text"
    IMAGE_PRODUCT_BACKGROUND = "image_product_background"
    VIDEO_VERTICAL_FAST = "video_vertical_fast"
    VIDEO_MOTION_FIDELITY = "video_motion_fidelity"
    VOICE_ES = "voice_es"
    MUSIC = "music"


class ReviewState(StrEnum):
    """Vista de revision humana de un `CreativeAsset` (rest-api.md
    §Creatividades, panel `creativeReviewStateSchema`): "estado NUESTRO
    (no de plataforma)", proyectada desde `CreativeAssetState` — no un
    campo de base de datos propio. `reject`/`regenerate` no tocan
    plataforma (FR-33); `PENDING` cubre todo lo que un humano todavia no
    ha aprobado ni rechazado (`DRAFT`/`READY`/`PROPOSED`)."""

    PENDING = "pending"
    APPROVED = "approved"
    REJECTED = "rejected"


class CreativeAssetState(StrEnum):
    """Ciclo de vida de un `CreativeAsset` (data-model.md + plan.md §5).
    `PUBLISHED` solo lo alcanza un manejador externo tras `ExecutionSucceeded`
    (creative nunca publica, FR-33)."""

    DRAFT = "draft"
    READY = "ready"
    PROPOSED = "proposed"
    APPROVED = "approved"
    REJECTED = "rejected"
    PUBLISHED = "published"


class CreativeJobState(StrEnum):
    """`data-model.md §CreativeAsset`: `QUEUED -> RENDERING -> COMPOSING ->
    CHECKING -> READY | FAILED | FALLBACK_CLOUD`."""

    QUEUED = "queued"
    RENDERING = "rendering"
    COMPOSING = "composing"
    CHECKING = "checking"
    READY = "ready"
    FAILED = "failed"
    FALLBACK_CLOUD = "fallback_cloud"


class PolicySeverity(StrEnum):
    WARN = "WARN"
    FAIL = "FAIL"


class PolicyVerdictResult(StrEnum):
    PASS_ = "PASS"  # noqa: S105 - veredicto de politica, no una contrasena
    WARN = "WARN"
    FAIL = "FAIL"


class CreativeOutcome(StrEnum):
    """FR-34: trazabilidad senal -> pieza -> resultado. Espeja
    `CreativeSignalKind` (plan.md §5 `signals`) sin importar ese contexto
    (N3, por encima de `creative` N2 en el grafo de dependencias)."""

    PENDING = "pending"
    WINNER = "winner"
    LOSER = "loser"
    FATIGUE = "fatigue"


class Placement(StrEnum):
    """Emplazamiento dentro de una plataforma; entrada de `PolicyCheckPort`
    junto a `PlatformCode` (creative-port.md)."""

    FEED = "feed"
    STORY = "story"
    REEL = "reel"
    SEARCH = "search"
    DISPLAY = "display"
    IN_STREAM = "in_stream"


class JobWeight(StrEnum):
    """Peso de un trabajo frente a `GpuLeasePort`: solo `HEAVY` compite por
    la concurrencia 1 de la DGX (creative-port.md §"Cola GPU")."""

    LIGHT = "light"
    HEAVY = "heavy"

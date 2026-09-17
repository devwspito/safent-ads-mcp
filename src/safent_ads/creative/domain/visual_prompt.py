"""Separacion capa-visual / capa-copy (medido sobre renders reales el
2026-09-09 con Qwen-Image-2512-Lightning 4-step): el modelo de difusion
dibuja de forma fiable un titular grande de 1-3 palabras, pero desfigura
texto pequeno ("Preara", "Infórmante
gradies") y, si el prompt menciona la plataforma o el dispositivo
("anuncio para Instagram"), dibuja un mockup de telefono en vez de la
escena pedida (`infra/creative/workflows/README.md`).

Regla: el modelo solo recibe la *capa visual* (escena + titular opcional
<=3 palabras, nunca la plataforma/UI/dispositivo). El copy exacto —
titular completo, texto secundario, CTA, legal — se compone siempre encima
con `BannerComposerPort` (HTML/Playwright, tipografia de marca). Esta capa
es pura: sin I/O, sin llamada al renderizador."""

from __future__ import annotations

import re
from dataclasses import dataclass

_MAX_ON_SCREEN_HEADLINE_WORDS = 3
_MAX_SCENE_DESCRIPTION_LEN = 2000

# De mas larga a mas corta: una alternancia regex hace *first match wins*
# por posicion, no por longitud, asi que una frase corta subsumida en una
# larga debe listarse despues para no partir la frase larga a medias.
_FORBIDDEN_PLATFORM_UI_TERMS: tuple[str, ...] = (
    "anuncio para instagram",
    "anuncio para facebook",
    "anuncio para tiktok",
    "captura de pantalla",
    "google ads",
    "meta ads",
    "instagram",
    "facebook",
    "tiktok",
    "anuncio",
    "publicidad",
    "reels",
    "reel",
    "stories",
    "story",
    "feed",
    "banner",
    "post",
    "smartphone",
    "móvil",
    "movil",
    "teléfono",
    "telefono",
    "pantalla",
    "mockup",
    "interfaz",
    "aplicación",
    "aplicacion",
    "app",
)

_NEGATIVE_GUIDANCE_SUFFIX = (
    "No dibujes telefonos, mockups, capturas de pantalla, interfaces de "
    "aplicacion ni logotipos de redes sociales. Si hay texto en la imagen, "
    "que sea grande, minimo y legible; nunca parrafos ni texto pequeno."
)

_WHITESPACE_PATTERN = re.compile(r"\s{2,}")


class VisualPromptError(ValueError):
    """Violacion de un invariante de la capa visual."""


def _forbidden_terms_pattern() -> re.Pattern[str]:
    ordered = sorted(_FORBIDDEN_PLATFORM_UI_TERMS, key=len, reverse=True)
    alternation = "|".join(re.escape(term) for term in ordered)
    return re.compile(rf"\b(?:{alternation})\b", re.IGNORECASE)


_FORBIDDEN_TERMS_PATTERN = _forbidden_terms_pattern()


def sanitize_scene_description(text: str) -> str:
    """Retira menciones a plataforma/UI/dispositivo de una descripcion de
    escena; el modelo interpreta esas palabras como instruccion de dibujar
    un mockup en vez de la escena real."""
    if not text.strip():
        raise VisualPromptError("scene_description vacia")
    if len(text) > _MAX_SCENE_DESCRIPTION_LEN:
        raise VisualPromptError(f"scene_description supera {_MAX_SCENE_DESCRIPTION_LEN} caracteres")
    stripped = _FORBIDDEN_TERMS_PATTERN.sub("", text)
    return _WHITESPACE_PATTERN.sub(" ", stripped).strip()


def _require_short_headline(headline: str) -> str:
    word_count = len(headline.split())
    if word_count > _MAX_ON_SCREEN_HEADLINE_WORDS:
        raise VisualPromptError(
            f"on_screen_headline debe tener <= {_MAX_ON_SCREEN_HEADLINE_WORDS} "
            f"palabras, llegaron {word_count}: {headline!r}"
        )
    return sanitize_scene_description(headline)


@dataclass(frozen=True, slots=True, kw_only=True)
class VisualPrompt:
    """Prompt final que se envia al renderizador de imagen/video. Nunca
    contiene el copy exacto del anuncio (headline completo, primary_text,
    CTA): eso lo compone `BannerComposerPort` despues."""

    text: str
    on_screen_headline: str | None

    def __post_init__(self) -> None:
        if not self.text.strip():
            raise VisualPromptError("text vacio")


def build_visual_prompt(
    scene_description: str, on_screen_headline: str | None = None
) -> VisualPrompt:
    """Unico punto de construccion del prompt de imagen/video: sanitiza la
    escena, valida y sanitiza el titular corto opcional, y anade la
    instruccion negativa contra mockups/UI/texto denso."""
    sanitized_scene = sanitize_scene_description(scene_description)
    sanitized_headline = (
        _require_short_headline(on_screen_headline) if on_screen_headline else None
    )
    parts = [sanitized_scene]
    if sanitized_headline:
        parts.append(f'Texto en pantalla, grande y centrado: "{sanitized_headline}".')
    parts.append(_NEGATIVE_GUIDANCE_SUFFIX)
    return VisualPrompt(text=" ".join(parts), on_screen_headline=sanitized_headline)

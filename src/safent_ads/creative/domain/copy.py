"""`AdCopy`: variante de texto de un anuncio (spec.md FR-31, NFR-12: espanol
natural). No esta en `creative-port.md` como dataclase propia; es el
elemento de la matriz "copy x visual" que `GenerateCreativeAssets` combina
(creative-port.md §"Seleccion de renderizador" + `CreativeBrief.variant_count`)."""

from __future__ import annotations

from dataclasses import dataclass

from safent_ads.creative.domain.enums import CallToAction, Language
from safent_ads.shared.ids import PlatformCode

_MAX_HEADLINE_LEN = 40
_MAX_PRIMARY_TEXT_LEN = 600

_CTA_LABELS_ES: dict[CallToAction, str] = {
    CallToAction.LEARN_MORE: "Más información",
    CallToAction.SIGN_UP: "Apúntate",
    CallToAction.APPLY_NOW: "Solicita plaza",
    CallToAction.CALL_NOW: "Llama ahora",
    CallToAction.MESSAGE_WHATSAPP: "Escríbenos por WhatsApp",
    CallToAction.GET_INFO: "Infórmate",
}

# Cierre duro por plataforma (creative-port.md §"Reglas invariables": los
# limites de cada emplazamiento se validan en la composicion, no despues).
_ALLOWED_CTA_BY_PLATFORM: dict[PlatformCode, frozenset[CallToAction]] = {
    PlatformCode.META: frozenset(
        {
            CallToAction.LEARN_MORE,
            CallToAction.SIGN_UP,
            CallToAction.APPLY_NOW,
            CallToAction.MESSAGE_WHATSAPP,
            CallToAction.GET_INFO,
        }
    ),
    PlatformCode.GOOGLE: frozenset(
        {
            CallToAction.LEARN_MORE,
            CallToAction.SIGN_UP,
            CallToAction.APPLY_NOW,
            CallToAction.CALL_NOW,
            CallToAction.GET_INFO,
        }
    ),
}


class AdCopyError(ValueError):
    """Copy invalido: longitud fuera de limite o campo vacio."""


class CtaNotAllowedForPlatformError(ValueError):
    """El CTA elegido no pertenece al conjunto cerrado de esa plataforma."""


def cta_label_es(cta: CallToAction) -> str:
    return _CTA_LABELS_ES[cta]


def is_cta_allowed(cta: CallToAction, platform: PlatformCode) -> bool:
    return cta in _ALLOWED_CTA_BY_PLATFORM[platform]


@dataclass(frozen=True, slots=True)
class AdCopy:
    """Una variante de texto: titular, texto principal y llamada a la
    accion. `headline` <= 40 caracteres es invariante de constructor, no un
    hallazgo de `PolicyCheck` en tiempo de ejecucion."""

    headline: str
    primary_text: str
    cta: CallToAction
    language: Language = Language.ES_ES

    def __post_init__(self) -> None:
        if not self.headline.strip():
            raise AdCopyError("headline vacio")
        if len(self.headline) > _MAX_HEADLINE_LEN:
            raise AdCopyError(f"headline supera {_MAX_HEADLINE_LEN} caracteres")
        if not self.primary_text.strip():
            raise AdCopyError("primary_text vacio")
        if len(self.primary_text) > _MAX_PRIMARY_TEXT_LEN:
            raise AdCopyError(f"primary_text supera {_MAX_PRIMARY_TEXT_LEN} caracteres")

    def require_cta_allowed_for(self, platform: PlatformCode) -> None:
        if not is_cta_allowed(self.cta, platform):
            raise CtaNotAllowedForPlatformError(f"{self.cta} no permitido en {platform}")

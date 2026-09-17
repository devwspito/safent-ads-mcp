"""`BrandDiscoveryDraft`: agregado de un rastreo de identidad de marca
(owner request: "El MCP debe pedir el sitio web del cliente (OPCIONAL)
para sacar la identidad de marca -- logos, colores, tipografias, tono. El
usuario puede subir manual o poner el enlace y que se rastree desde la
web"). Candidatos con confianza y procedencia para cada categoria; nunca
se usa como `BrandKit` de verdad hasta que `ConfirmBrandDraft` lo confirma
(`merge_into_kit` siempre produce `is_confirmed=False`, brand_kit.py).

Puro: sin HTTP, sin parseo de HTML/CSS (eso es
`infrastructure/html_brand_parser.py` -- mismo reparto que
`infrastructure/yaml_brand_kit_loader.py` con `config/brand/*.yaml`, la
traduccion de un formato externo es un adaptador, no dominio)."""

from __future__ import annotations

import re
from dataclasses import dataclass, replace
from datetime import datetime
from enum import StrEnum

from safent_ads.brand.domain.brand_asset import AssetKind, BrandAsset
from safent_ads.brand.domain.brand_kit import PLACEHOLDER_MARKER, BrandKit
from safent_ads.brand.domain.claims_policy import normalize_forbidden_claims
from safent_ads.brand.domain.color_palette import ColorPalette, ColorRole, ColorSwatch
from safent_ads.brand.domain.errors import (
    BlankFieldError,
    InvalidConfidenceError,
    InvalidDiscoveryUrlError,
    InvalidHexColorError,
    PiiDetectedInCopySampleError,
)
from safent_ads.brand.domain.identifiers import BrandKitId
from safent_ads.brand.domain.tone_of_voice import ToneOfVoice
from safent_ads.brand.domain.typography import Typography
from safent_ads.shared.ids import BusinessId
from safent_ads.shared.net.ip_guard import UnsafeEgressUrlError, validate_egress_url

_MIN_CONFIDENCE = 0.0
_MAX_CONFIDENCE = 1.0
_HEX_COLOR_PATTERN = re.compile(r"^#[0-9A-Fa-f]{6}$")
_MAX_COPY_SAMPLE_LENGTH = 280
_TYPOGRAPHY_PLACEHOLDER_NOTE = f"{PLACEHOLDER_MARKER}: licencia por confirmar por el propietario"
_TONE_PLACEHOLDER = (
    f"{PLACEHOLDER_MARKER}: completar tras revisar los ejemplos de copy del borrador"
)
_DRAFT_USAGE_RULE_TEMPLATE = (
    "Importado automaticamente desde {source_url}; revisar antes de produccion."
)

# Un email tiene "@dominio.tld"; un telefono espanol tiene 9 digitos --
# ambos son la base del saneado de C-copy y de la defensa en profundidad de
# `CopySample` (nunca confiamos ciegamente en que la capa de arriba ya
# limpio el texto).
#
# F-1 (CWE-1333/CWE-400, checklists/website-brand-extractor-review.md):
# el patron original (`[\w.+-]+@...`) es cuadratico sobre una racha larga
# de caracteres validos sin `@` -- *medido* en la revision: 120 KB sin
# arroba, 11,6 s de CPU. Los cuantificadores acotados (nunca mas de 64/63
# caracteres por lado, dominio de hasta 24) convierten cada intento de
# encaje en trabajo constante, cerrando el blowup para cualquier longitud
# de entrada. `_MAX_PII_SCAN_CHARS` es la segunda capa: nunca escanear mas
# de 64 KiB de una pagina, aunque el HTML entero pese hasta 2 MiB.
_EMAIL_PATTERN = re.compile(r"[\w.+-]{1,64}@[\w-]{1,63}\.[A-Za-z]{2,24}")
_PHONE_DIGIT_RUN_PATTERN = re.compile(r"(?:\d[\s.\-]?){9,20}")

# F-12 (C-31): DNI (8 digitos + letra de control) y NIE (X/Y/Z + 7 digitos
# + letra) espanoles, mas IBAN (2 letras de pais + 2 digitos de control +
# hasta 30 alfanumericos, con o sin espacios cada 4 -- formato habitual al
# imprimirlo). Estructural, igual que el email/telefono de arriba: no
# valida el digito de control, solo reconoce la FORMA para poder quitarla.
_DNI_NIE_PATTERN = re.compile(r"\b(?:\d{8}|[XYZxyz]\d{7})[A-Za-z]\b")
_IBAN_PATTERN = re.compile(r"\b[A-Za-z]{2}\d{2}(?:[ ]?[A-Za-z0-9]{4}){2,7}\b")
_MAX_PII_SCAN_CHARS = 64 * 1024


class DiscoverySource(StrEnum):
    """Procedencia de un candidato (tool-surface.md ampliado: "candidates
    with confidence and provenance"). Verbo-primero no aplica aqui -- son
    etiquetas de dato, no nombres de herramienta."""

    FAVICON = "favicon"
    APPLE_TOUCH_ICON = "apple_touch_icon"
    OG_IMAGE = "og_image"
    MANIFEST_ICON = "manifest_icon"
    IMG_LOGO_HINT = "img_logo_hint"
    INLINE_SVG = "inline_svg"
    MANUAL_UPLOAD = "manual_upload"
    CSS_CUSTOM_PROPERTY = "css_custom_property"
    CSS_MOST_USED_COLOR = "css_most_used_color"
    DOMINANT_COLOR_LOGO = "dominant_color_logo"
    DOMINANT_COLOR_SCREENSHOT = "dominant_color_screenshot"
    CSS_FONT_FAMILY = "css_font_family"
    WEB_FONT_LINK = "web_font_link"
    OG_SITE_NAME = "og_site_name"
    TITLE_TAG = "title_tag"
    SCHEMA_ORG_ORGANIZATION = "schema_org_organization"
    HERO_HEADLINE = "hero_headline"
    TAGLINE = "tagline"
    CTA_TEXT = "cta_text"


class SocialNetwork(StrEnum):
    INSTAGRAM = "instagram"
    FACEBOOK = "facebook"
    LINKEDIN = "linkedin"
    TIKTOK = "tiktok"
    X = "x"
    YOUTUBE = "youtube"
    OTHER = "other"


class ContactChannelKind(StrEnum):
    EMAIL = "email"
    PHONE = "phone"
    WHATSAPP = "whatsapp"
    CONTACT_FORM = "contact_form"


def _validate_confidence(value: float) -> None:
    if not _MIN_CONFIDENCE <= value <= _MAX_CONFIDENCE:
        raise InvalidConfidenceError(f"confidence fuera de [0, 1]: {value!r}")


def validate_discovery_url(url: str) -> str:
    """Puerta de entrada del unico argumento URL de `brand`
    (`ingest_brand_from_website`, threat-model.md C-11). A diferencia de
    `creative.domain.asset_import.validate_import_source_url` no hay
    allow-list de host -- el propietario puede apuntar a cualquier sitio
    publico -- pero se aplican las mismas comprobaciones que no requieren
    resolucion DNS, mas el puerto (F-11: solo 80/443, nunca `:9200`,
    `:6379`...), delegadas en `shared.net.ip_guard.validate_egress_url`
    para no repetir la logica que ya reemplazo la lista negra de
    `website_brand_extractor.py`. Esa resolucion DNS es infraestructura
    y debe llamarse DESPUES de esto, nunca antes (fail closed). Devuelve
    el host en minusculas."""
    try:
        return validate_egress_url(url)
    except UnsafeEgressUrlError as exc:
        raise InvalidDiscoveryUrlError(str(exc)) from exc


def strip_pii(text: str) -> str:
    """Quita emails, telefonos, DNI/NIE e IBAN de un texto de copy antes
    de construir un `CopySample` (F-12, C-31: los canales de contacto son
    su *presencia* + la URL de la pagina de contacto, nunca el dato en
    si). Acotado a `_MAX_PII_SCAN_CHARS` (F-1): el llamante debe recortar
    a la longitud final ANTES de llamar a esto cuando le importa el texto
    completo (`CopySample` ya exige <=280 caracteres) -- este tope es la
    red de seguridad para cualquier otro llamante que no lo haga."""
    bounded = text[:_MAX_PII_SCAN_CHARS]
    without_email = _EMAIL_PATTERN.sub(" ", bounded)
    without_phone = _PHONE_DIGIT_RUN_PATTERN.sub(" ", without_email)
    without_dni = _DNI_NIE_PATTERN.sub(" ", without_phone)
    without_iban = _IBAN_PATTERN.sub(" ", without_dni)
    return re.sub(r"\s{2,}", " ", without_iban).strip()


def _contains_pii(text: str) -> bool:
    """Chequeo espejo de `strip_pii` (F-12, C-31): defensa en profundidad
    de `CopySample.__post_init__`, misma lista de patrones."""
    return bool(
        _EMAIL_PATTERN.search(text)
        or _PHONE_DIGIT_RUN_PATTERN.search(text)
        or _DNI_NIE_PATTERN.search(text)
        or _IBAN_PATTERN.search(text)
    )


def detect_contact_channel_kinds(text: str) -> frozenset[ContactChannelKind]:
    """Presencia (no el dato) de email/telefono en un bloque de texto ya
    visible de una pagina -- llamado ANTES de `strip_pii`, sobre el texto
    crudo, para no perder la senal de que el canal existe. Acotado a
    `_MAX_PII_SCAN_CHARS` (F-1): el texto visible de una pagina puede
    pesar hasta 2 MiB, pero un canal de contacto real aparece siempre
    cerca del principio (cabecera, pie, seccion de contacto)."""
    bounded = text[:_MAX_PII_SCAN_CHARS]
    kinds: set[ContactChannelKind] = set()
    if _EMAIL_PATTERN.search(bounded):
        kinds.add(ContactChannelKind.EMAIL)
    if _PHONE_DIGIT_RUN_PATTERN.search(bounded):
        kinds.add(ContactChannelKind.PHONE)
    return frozenset(kinds)


def contrast_ratio_on_white(hex_color: str) -> float:
    """Ratio de contraste WCAG de `hex_color` sobre blanco puro
    (formula de luminancia relativa, W3C). `ColorSwatch` exige este valor
    ya medido (color_palette.py: "el contraste no se calcula aqui"); para
    un candidato auto-rastreado no hay "ojo humano" todavia, asi que el
    calculo determinista vive aqui -- sigue siendo dominio puro, es
    aritmetica, no I/O."""
    r, g, b = (int(hex_color[i : i + 2], 16) / 255 for i in (1, 3, 5))
    luminance = _relative_luminance(r, g, b)
    return round((1.0 + 0.05) / (luminance + 0.05), 2)


def _relative_luminance(r: float, g: float, b: float) -> float:
    channels = (_linearize(c) for c in (r, g, b))
    r_lin, g_lin, b_lin = channels
    return 0.2126 * r_lin + 0.7152 * g_lin + 0.0722 * b_lin


_LINEAR_THRESHOLD = 0.03928
_LINEAR_DIVISOR = 12.92
_GAMMA_OFFSET = 0.055
_GAMMA_EXPONENT = 2.4


def _linearize(channel: float) -> float:
    if channel <= _LINEAR_THRESHOLD:
        return channel / _LINEAR_DIVISOR
    return float(((channel + _GAMMA_OFFSET) / (1.0 + _GAMMA_OFFSET)) ** _GAMMA_EXPONENT)


@dataclass(frozen=True, kw_only=True, slots=True)
class LogoCandidate:
    asset_id: str
    kind: AssetKind
    storage_uri: str
    sha256: str
    source: DiscoverySource
    confidence: float

    def __post_init__(self) -> None:
        _validate_confidence(self.confidence)
        if not self.asset_id.strip():
            raise BlankFieldError("asset_id vacio")
        if not self.storage_uri.strip():
            raise BlankFieldError("storage_uri vacio")

    def to_brand_asset(self, *, usage_rule: str) -> BrandAsset:
        return BrandAsset(
            asset_id=self.asset_id,
            kind=self.kind,
            storage_uri=self.storage_uri,
            usage_rule=usage_rule,
        )


@dataclass(frozen=True, kw_only=True, slots=True)
class ColorCandidate:
    hex: str
    source: DiscoverySource
    confidence: float
    role_hint: ColorRole | None = None

    def __post_init__(self) -> None:
        _validate_confidence(self.confidence)
        if not _HEX_COLOR_PATTERN.match(self.hex):
            raise InvalidHexColorError(f"hex invalido, se esperaba #RRGGBB: {self.hex!r}")


@dataclass(frozen=True, kw_only=True, slots=True)
class TypographyCandidate:
    family: str
    source: DiscoverySource
    confidence: float

    def __post_init__(self) -> None:
        _validate_confidence(self.confidence)
        if not self.family.strip():
            raise BlankFieldError("family vacio")


@dataclass(frozen=True, kw_only=True, slots=True)
class BusinessNameCandidate:
    name: str
    source: DiscoverySource
    confidence: float

    def __post_init__(self) -> None:
        _validate_confidence(self.confidence)
        if not self.name.strip():
            raise BlankFieldError("name vacio")


@dataclass(frozen=True, kw_only=True, slots=True)
class SocialLinkCandidate:
    network: SocialNetwork
    url: str

    def __post_init__(self) -> None:
        if not self.url.strip():
            raise BlankFieldError("url vacia")


@dataclass(frozen=True, kw_only=True, slots=True)
class ContactChannelCandidate:
    kind: ContactChannelKind
    page_url: str

    def __post_init__(self) -> None:
        if not self.page_url.strip():
            raise BlankFieldError("page_url vacia")


@dataclass(frozen=True, kw_only=True, slots=True)
class CopySample:
    """`text` nunca lleva PII (owner request explicito): saneado en
    `infrastructure` via `strip_pii` ANTES de construir esto; el rechazo de
    aqui es defensa en profundidad, no el mecanismo principal."""

    source: DiscoverySource
    text: str

    def __post_init__(self) -> None:
        stripped = self.text.strip()
        if not stripped:
            raise BlankFieldError("text vacio")
        if len(stripped) > _MAX_COPY_SAMPLE_LENGTH:
            raise ValueError(f"text supera {_MAX_COPY_SAMPLE_LENGTH} caracteres")
        if _contains_pii(stripped):
            raise PiiDetectedInCopySampleError("la muestra de copy todavia contiene PII")


@dataclass(frozen=True, kw_only=True, slots=True)
class BrandDiscoveryDraft:
    business_id: BusinessId
    source_url: str | None
    discovered_at: datetime
    logo_candidates: tuple[LogoCandidate, ...] = ()
    color_candidates: tuple[ColorCandidate, ...] = ()
    typography_candidates: tuple[TypographyCandidate, ...] = ()
    business_name_candidates: tuple[BusinessNameCandidate, ...] = ()
    social_links: tuple[SocialLinkCandidate, ...] = ()
    contact_channels: tuple[ContactChannelCandidate, ...] = ()
    copy_samples: tuple[CopySample, ...] = ()

    def with_manual_logo(self, candidate: LogoCandidate) -> BrandDiscoveryDraft:
        """Ruta manual (owner request: "el usuario puede subir manual"):
        anade/reemplaza por `asset_id`, nunca duplica."""
        kept = tuple(c for c in self.logo_candidates if c.asset_id != candidate.asset_id)
        return replace(self, logo_candidates=(*kept, candidate))

    def top_logo(self, *, kind: AssetKind | None = None) -> LogoCandidate | None:
        pool = self.logo_candidates if kind is None else self._logos_of_kind(kind)
        return max(pool, key=lambda c: c.confidence, default=None)

    def _logos_of_kind(self, kind: AssetKind) -> tuple[LogoCandidate, ...]:
        return tuple(c for c in self.logo_candidates if c.kind == kind)

    def logo_by_asset_id(self, asset_id: str) -> LogoCandidate | None:
        return next((c for c in self.logo_candidates if c.asset_id == asset_id), None)

    def top_color(self) -> ColorCandidate | None:
        return max(self.color_candidates, key=lambda c: c.confidence, default=None)

    def top_typography(self) -> TypographyCandidate | None:
        return max(self.typography_candidates, key=lambda c: c.confidence, default=None)

    def merge_into_kit(
        self, *, brand_kit_id: BrandKitId, existing: BrandKit | None, now: datetime
    ) -> BrandKit:
        """Produce un `BrandKit` **borrador** (`is_confirmed=False`,
        brand_kit.py): nunca sobreescribe un valor real ya confirmado por
        el propietario, solo rellena huecos con la mejor conjetura o con
        el marcador de plantilla. `ConfirmBrandDraft` es el unico camino
        para que `is_confirmed` pase a `True`."""
        return BrandKit(
            brand_kit_id=brand_kit_id,
            business_id=self.business_id,
            typography=self._merged_typography(existing),
            palette=self._merged_palette(existing),
            tone_of_voice=self._merged_tone(existing),
            updated_at=now,
            assets=self._merged_assets(existing),
            claims_allowlist=existing.claims_allowlist if existing else frozenset(),
            forbidden_claims=normalize_forbidden_claims(
                existing.forbidden_claims if existing else ()
            ),
            legal_disclaimers=existing.legal_disclaimers if existing else (),
            platform_constraints=existing.platform_constraints if existing else (),
            is_confirmed=False,
        )

    def _merged_typography(self, existing: BrandKit | None) -> Typography:
        if existing is not None and not _is_placeholder(existing.typography.primary_family):
            return existing.typography
        top = self.top_typography()
        return Typography(
            primary_family=top.family if top else PLACEHOLDER_MARKER,
            licence_note=_TYPOGRAPHY_PLACEHOLDER_NOTE,
        )

    def _merged_palette(self, existing: BrandKit | None) -> ColorPalette:
        if existing is not None and existing.palette.swatches:
            return existing.palette
        ranked = sorted(self.color_candidates, key=lambda c: c.confidence, reverse=True)
        roles = (ColorRole.PRIMARY, ColorRole.SECONDARY, ColorRole.ACCENT)
        swatches = tuple(
            ColorSwatch(
                role=candidate.role_hint or role,
                hex=candidate.hex,
                contrast_ratio_on_white=contrast_ratio_on_white(candidate.hex),
            )
            for candidate, role in zip(ranked, roles, strict=False)
        )
        return ColorPalette(swatches=swatches)

    def _merged_tone(self, existing: BrandKit | None) -> ToneOfVoice:
        if existing is not None and not _is_placeholder(existing.tone_of_voice.description):
            return existing.tone_of_voice
        return ToneOfVoice(description=_TONE_PLACEHOLDER)

    def _merged_assets(self, existing: BrandKit | None) -> tuple[BrandAsset, ...]:
        kept = existing.assets if existing else ()
        kept_uris = {a.storage_uri for a in kept}
        origin = self.source_url or "subida manual"
        usage_rule = _DRAFT_USAGE_RULE_TEMPLATE.format(source_url=origin)
        discovered = tuple(
            candidate.to_brand_asset(usage_rule=usage_rule)
            for candidate in self._preview_logo_candidates()
            if candidate.storage_uri not in kept_uris
        )
        return (*kept, *discovered)

    def _preview_logo_candidates(self) -> tuple[LogoCandidate, ...]:
        """Como mucho un logo y un icono en la vista previa sin confirmar
        -- el resto de candidatos se revisan en `get_brand_draft`, no se
        vuelcan todos en `BrandKit.assets` (ruido)."""
        best_logo = self.top_logo(kind=AssetKind.LOGO_RASTER) or self.top_logo(
            kind=AssetKind.LOGO_VECTOR
        )
        best_icon = self.top_logo(kind=AssetKind.ICON)
        return tuple(c for c in (best_logo, best_icon) if c is not None)


def _is_placeholder(text: str) -> bool:
    return PLACEHOLDER_MARKER.casefold() in text.casefold()

"""Parsers puros de HTML/CSS para el rastreo de identidad de marca.
Infraestructura, no dominio -- mismo reparto que `yaml_brand_kit_loader.py`
con `config/brand/*.yaml`: traducir un formato externo (HTML/CSS/JSON) a
candidatos de dominio es un adaptador. Cada `extract_*` toma texto YA
DESCARGADO -- nunca abre una conexion -- para poder probarse con fixtures
guardadas (tests/unit/brand/infrastructure), sin red.

REUSE-BEFORE-WRITING (owner request, plazo de 15 min): se evaluaron
`extruct`/`trafilatura`/`tinycss2`/`colorthief`, pero ninguno esta ya en
`pyproject.toml` y cada uno trae su propia superficie de parseo sobre HTML
de terceros no confiable. Se opto por la biblioteca estandar
(`html.parser.HTMLParser`, ya en Python) mas expresiones regulares acotadas
para CSS -- cero dependencias nuevas, superficie de ataque minima sobre
contenido de un sitio arbitrario. `Pillow` (color dominante) y
`Playwright` (paso opcional JS) ya estaban en las dependencias de
`creative` y se reutilizan tal cual."""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from html.parser import HTMLParser
from urllib.parse import urljoin, urlsplit

from safent_ads.brand.domain.discovery import (
    BusinessNameCandidate,
    ColorCandidate,
    ContactChannelCandidate,
    ContactChannelKind,
    CopySample,
    DiscoverySource,
    SocialLinkCandidate,
    SocialNetwork,
    TypographyCandidate,
    detect_contact_channel_kinds,
    strip_pii,
)
from safent_ads.brand.domain.errors import BlankFieldError, PiiDetectedInCopySampleError

_LOGO_HINT_PATTERN = re.compile(r"logo", re.IGNORECASE)
_GENERIC_FONT_FAMILIES = frozenset(
    {"sans-serif", "serif", "monospace", "system-ui", "cursive", "fantasy", "inherit", "initial"}
)
_FONT_FAMILY_PATTERN = re.compile(r"font-family\s*:\s*([^;}]+)", re.IGNORECASE)
_GOOGLE_FONTS_FAMILY_PATTERN = re.compile(r"family=([^&:@]+)")
_SOCIAL_DOMAINS: dict[str, SocialNetwork] = {
    "instagram.com": SocialNetwork.INSTAGRAM,
    "facebook.com": SocialNetwork.FACEBOOK,
    "linkedin.com": SocialNetwork.LINKEDIN,
    "tiktok.com": SocialNetwork.TIKTOK,
    "twitter.com": SocialNetwork.X,
    "x.com": SocialNetwork.X,
    "youtube.com": SocialNetwork.YOUTUBE,
}
_CTA_HINT_PATTERN = re.compile(r"\b(cta|btn|button)\b", re.IGNORECASE)
_INTERESTING_PAGE_KEYWORDS = (
    "about",
    "nosotros",
    "quienes-somos",
    "sobre-nosotros",
    "contacto",
    "contact",
    "legal",
    "aviso-legal",
    "privacidad",
)
_HEX6_PATTERN = re.compile(r"#[0-9A-Fa-f]{6}\b")
_CUSTOM_PROPERTY_PATTERN = re.compile(r"--([\w-]+)\s*:\s*(#[0-9A-Fa-f]{6}|#[0-9A-Fa-f]{3})\b")
_BRAND_PROPERTY_NAME_PATTERN = re.compile(r"brand|primary|main|accent|secondary", re.IGNORECASE)
_WHATSAPP_HOSTS = frozenset({"wa.me", "api.whatsapp.com"})

_MAX_LINKED_PAGES = 4
_MAX_STYLESHEETS = 3
_MAX_COPY_SAMPLES = 10
_MAX_SAMPLE_TEXT_LENGTH = 280
_MAX_TAGLINE_LENGTH = 140
_MIN_CTA_LENGTH = 2
_MAX_CTA_LENGTH = 40
_MAX_LOGO_HINTS = 10  # F-2: 2 MiB de HTML sin tope aqui -> decenas de miles de <link>/<img>


@dataclass(frozen=True, slots=True)
class LogoHint:
    """Candidato a logo/icono antes de descargarse (sin `asset_id`,
    `storage_uri` ni `sha256`: eso solo existe tras el paso de I/O de
    `website_brand_extractor.py`)."""

    url: str
    source: DiscoverySource
    confidence: float


class _DocumentParser(HTMLParser):
    """Un unico paso sobre el HTML; cada `extract_*` publico opera sobre su
    propia instancia (re-parsear es barato frente al tope de tamano de
    pagina, y mantiene cada funcion testeable de forma independiente)."""

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.title = ""
        self.meta: dict[str, str] = {}
        self.link_tags: list[dict[str, str]] = []
        self.imgs: list[dict[str, str]] = []
        self.anchors: list[dict[str, str]] = []
        self.ld_json_blocks: list[str] = []
        self.style_blocks: list[str] = []
        self.h1: list[str] = []
        self.h2: list[str] = []
        self.form_present = False
        self.visible_text: list[str] = []
        self._capture: str | None = None
        self._buffer: list[str] = []
        self._current_anchor: dict[str, str] | None = None

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        attr_map = {k: (v or "") for k, v in attrs}
        handler = getattr(self, f"_start_{tag}", None)
        if handler is not None:
            handler(attr_map)

    def _start_title(self, _attrs: dict[str, str]) -> None:
        self._begin_capture("title")

    def _start_meta(self, attrs: dict[str, str]) -> None:
        key = attrs.get("property") or attrs.get("name")
        if key:
            self.meta[key.lower()] = attrs.get("content", "")

    def _start_link(self, attrs: dict[str, str]) -> None:
        self.link_tags.append(attrs)

    def _start_img(self, attrs: dict[str, str]) -> None:
        self.imgs.append(attrs)

    def _start_a(self, attrs: dict[str, str]) -> None:
        self._current_anchor = {
            "href": attrs.get("href", ""),
            "class": attrs.get("class", ""),
            "id": attrs.get("id", ""),
        }
        self._begin_capture("a")

    def _start_script(self, attrs: dict[str, str]) -> None:
        if attrs.get("type", "").lower() == "application/ld+json":
            self._begin_capture("ld_json")

    def _start_style(self, _attrs: dict[str, str]) -> None:
        self._begin_capture("style")

    def _start_h1(self, _attrs: dict[str, str]) -> None:
        self._begin_capture("h1")

    def _start_h2(self, _attrs: dict[str, str]) -> None:
        self._begin_capture("h2")

    def _start_form(self, _attrs: dict[str, str]) -> None:
        self.form_present = True

    def _begin_capture(self, name: str) -> None:
        self._capture = name
        self._buffer = []

    def handle_endtag(self, tag: str) -> None:
        if self._capture != tag and not (tag == "script" and self._capture == "ld_json"):
            return
        text = "".join(self._buffer).strip()
        if tag == "title":
            self.title = text
        elif tag == "a" and self._current_anchor is not None:
            self._current_anchor["text"] = text
            self.anchors.append(self._current_anchor)
            self._current_anchor = None
        elif tag == "script":
            self.ld_json_blocks.append(text)
        elif tag == "style":
            self.style_blocks.append(text)
        elif tag in ("h1", "h2"):
            getattr(self, tag).append(text)
        self._capture = None

    def handle_data(self, data: str) -> None:
        self.visible_text.append(data)
        if self._capture is not None:
            self._buffer.append(data)


def _parse(html_text: str) -> _DocumentParser:
    parser = _DocumentParser()
    parser.feed(html_text)
    return parser


def extract_logo_hints(base_url: str, html_text: str) -> list[LogoHint]:
    doc = _parse(html_text)
    hints = [
        *_favicon_hints(base_url, doc),
        *_og_image_hint(base_url, doc),
        *_img_logo_hints(base_url, doc),
    ]
    return dedupe_and_cap_logo_hints(hints)


def dedupe_and_cap_logo_hints(hints: list[LogoHint]) -> list[LogoHint]:
    """Quita duplicados por URL (se queda con la primera aparicion) y
    corta a `_MAX_LOGO_HINTS` (F-2, CWE-770): 2 MiB de HTML sin este tope
    puede llevar decenas de miles de `<link rel=icon>`/`<img>` -- cada
    uno, una descarga saliente y una escritura a disco. Llamado tanto
    aqui (favicon/og/img) como en `website_brand_extractor` sobre la
    lista ya combinada con los iconos del manifest, para que el TOTAL
    final -- no solo cada fuente por separado -- respete el tope."""
    seen: set[str] = set()
    capped: list[LogoHint] = []
    for hint in hints:
        if hint.url in seen:
            continue
        seen.add(hint.url)
        capped.append(hint)
        if len(capped) >= _MAX_LOGO_HINTS:
            break
    return capped


def _favicon_hints(base_url: str, doc: _DocumentParser) -> list[LogoHint]:
    hints: list[LogoHint] = []
    for link in doc.link_tags:
        rel, href = link.get("rel", "").lower(), link.get("href", "")
        if not href:
            continue
        if "apple-touch-icon" in rel:
            hints.append(LogoHint(urljoin(base_url, href), DiscoverySource.APPLE_TOUCH_ICON, 0.5))
        elif "icon" in rel:
            hints.append(LogoHint(urljoin(base_url, href), DiscoverySource.FAVICON, 0.3))
    return hints


def _og_image_hint(base_url: str, doc: _DocumentParser) -> list[LogoHint]:
    content = doc.meta.get("og:image")
    if not content:
        return []
    return [LogoHint(urljoin(base_url, content), DiscoverySource.OG_IMAGE, 0.6)]


def _img_logo_hints(base_url: str, doc: _DocumentParser) -> list[LogoHint]:
    hints: list[LogoHint] = []
    for img in doc.imgs:
        haystack = " ".join((img.get("alt", ""), img.get("src", ""), img.get("class", "")))
        if img.get("src") and _LOGO_HINT_PATTERN.search(haystack):
            hints.append(
                LogoHint(urljoin(base_url, img["src"]), DiscoverySource.IMG_LOGO_HINT, 0.7)
            )
    return hints


def extract_manifest_url(base_url: str, html_text: str) -> str | None:
    doc = _parse(html_text)
    for link in doc.link_tags:
        if link.get("rel", "").lower() == "manifest" and link.get("href"):
            return urljoin(base_url, link["href"])
    return None


def extract_manifest_icon_hints(base_url: str, manifest_json_text: str) -> list[LogoHint]:
    try:
        data = json.loads(manifest_json_text)
    except ValueError:
        return []
    icons = data.get("icons") if isinstance(data, dict) else None
    if not isinstance(icons, list):
        return []
    return [
        LogoHint(urljoin(base_url, icon["src"]), DiscoverySource.MANIFEST_ICON, 0.55)
        for icon in icons
        if isinstance(icon, dict) and icon.get("src")
    ]


def extract_stylesheet_urls(base_url: str, html_text: str) -> list[str]:
    doc = _parse(html_text)
    urls = [
        urljoin(base_url, link["href"])
        for link in doc.link_tags
        if link.get("rel", "").lower() == "stylesheet" and link.get("href")
    ]
    return urls[:_MAX_STYLESHEETS]


def extract_inline_style_text(html_text: str) -> str:
    return "\n".join(_parse(html_text).style_blocks)


def extract_web_font_link_candidates(html_text: str) -> list[TypographyCandidate]:
    doc = _parse(html_text)
    candidates: list[TypographyCandidate] = []
    for link in doc.link_tags:
        href = link.get("href", "")
        if "fonts.googleapis.com" in href or "fonts.google.com" in href:
            candidates.extend(_google_font_families(href))
        elif "use.typekit.net" in href:
            candidates.append(
                TypographyCandidate(
                    family="Adobe Fonts (Typekit)",
                    source=DiscoverySource.WEB_FONT_LINK,
                    confidence=0.5,
                )
            )
    return candidates


def _google_font_families(href: str) -> list[TypographyCandidate]:
    match = _GOOGLE_FONTS_FAMILY_PATTERN.search(href)
    if not match:
        return []
    name = match.group(1).replace("+", " ").strip()
    if not name:
        return []
    return [TypographyCandidate(family=name, source=DiscoverySource.WEB_FONT_LINK, confidence=0.75)]


def extract_css_font_family_candidates(css_text: str) -> list[TypographyCandidate]:
    counts: dict[str, int] = {}
    for match in _FONT_FAMILY_PATTERN.finditer(css_text):
        family = _first_named_family(match.group(1))
        if family:
            counts[family] = counts.get(family, 0) + 1
    ranked = sorted(counts.items(), key=lambda kv: kv[1], reverse=True)
    confidences = (0.6, 0.4)
    return [
        TypographyCandidate(
            family=name, source=DiscoverySource.CSS_FONT_FAMILY, confidence=confidence
        )
        for (name, _count), confidence in zip(ranked, confidences, strict=False)
    ]


def _first_named_family(declaration: str) -> str | None:
    for raw in declaration.split(","):
        name = raw.strip().strip("'\"")
        if name and name.lower() not in _GENERIC_FONT_FAMILIES:
            return name
    return None


_SHORTHAND_HEX_LENGTH = 4  # "#abc"


def _normalize_hex(value: str) -> str:
    if len(value) == _SHORTHAND_HEX_LENGTH:
        return "#" + "".join(ch * 2 for ch in value[1:].upper())
    return value.upper()


def extract_css_custom_property_colors(css_text: str) -> list[ColorCandidate]:
    candidates: list[ColorCandidate] = []
    for name, hex_value in _CUSTOM_PROPERTY_PATTERN.findall(css_text):
        confidence = 0.7 if _BRAND_PROPERTY_NAME_PATTERN.search(name) else 0.4
        candidates.append(
            ColorCandidate(
                hex=_normalize_hex(hex_value),
                source=DiscoverySource.CSS_CUSTOM_PROPERTY,
                confidence=confidence,
            )
        )
    return candidates


def extract_css_most_used_colors(css_text: str) -> list[ColorCandidate]:
    counts: dict[str, int] = {}
    for match in _HEX6_PATTERN.finditer(css_text):
        hex_value = match.group().upper()
        counts[hex_value] = counts.get(hex_value, 0) + 1
    ranked = sorted(counts.items(), key=lambda kv: kv[1], reverse=True)[:3]
    confidences = (0.5, 0.4, 0.3)
    return [
        ColorCandidate(
            hex=hex_value, source=DiscoverySource.CSS_MOST_USED_COLOR, confidence=confidence
        )
        for (hex_value, _count), confidence in zip(ranked, confidences, strict=False)
    ]


def extract_business_name_candidates(html_text: str) -> list[BusinessNameCandidate]:
    doc = _parse(html_text)
    candidates: list[BusinessNameCandidate] = []
    og_site_name = doc.meta.get("og:site_name")
    if og_site_name and og_site_name.strip():
        candidates.append(
            BusinessNameCandidate(
                name=og_site_name.strip(), source=DiscoverySource.OG_SITE_NAME, confidence=0.8
            )
        )
    if doc.title:
        candidates.append(
            BusinessNameCandidate(name=doc.title, source=DiscoverySource.TITLE_TAG, confidence=0.5)
        )
    candidates.extend(_schema_org_organization_names(doc))
    return candidates


def _schema_org_organization_names(doc: _DocumentParser) -> list[BusinessNameCandidate]:
    names: list[BusinessNameCandidate | None] = []
    for block in doc.ld_json_blocks:
        try:
            data = json.loads(block)
        except ValueError:
            continue
        entries = data if isinstance(data, list) else [data]
        names.extend(_organization_name_from_entry(entry) for entry in entries)
    return [name for name in names if name is not None]


def _organization_name_from_entry(entry: object) -> BusinessNameCandidate | None:
    if not isinstance(entry, dict) or entry.get("@type") != "Organization" or not entry.get("name"):
        return None
    return BusinessNameCandidate(
        name=str(entry["name"]).strip(),
        source=DiscoverySource.SCHEMA_ORG_ORGANIZATION,
        confidence=0.85,
    )


def extract_social_links(html_text: str) -> list[SocialLinkCandidate]:
    doc = _parse(html_text)
    seen: set[str] = set()
    links: list[SocialLinkCandidate] = []
    for anchor in doc.anchors:
        href = anchor.get("href", "")
        network = _social_network_for(href)
        if network is None or href in seen:
            continue
        seen.add(href)
        links.append(SocialLinkCandidate(network=network, url=href))
    return links


def _social_network_for(href: str) -> SocialNetwork | None:
    host = (urlsplit(href).hostname or "").lower().removeprefix("www.")
    return _SOCIAL_DOMAINS.get(host)


def extract_contact_channels(page_url: str, html_text: str) -> list[ContactChannelCandidate]:
    doc = _parse(html_text)
    kinds = set(detect_contact_channel_kinds(" ".join(doc.visible_text)))
    if doc.form_present:
        kinds.add(ContactChannelKind.CONTACT_FORM)
    if any(_is_whatsapp_link(a.get("href", "")) for a in doc.anchors):
        kinds.add(ContactChannelKind.WHATSAPP)
    return [
        ContactChannelCandidate(kind=kind, page_url=page_url)
        for kind in sorted(kinds, key=lambda k: k.value)
    ]


def _is_whatsapp_link(href: str) -> bool:
    return (urlsplit(href).hostname or "").lower() in _WHATSAPP_HOSTS


def extract_linked_page_urls(base_url: str, html_text: str) -> list[str]:
    doc = _parse(html_text)
    origin = _origin_of(base_url)
    seen: set[str] = set()
    matches: list[str] = []
    for anchor in doc.anchors:
        href = anchor.get("href", "")
        if not href or href.startswith("#"):
            continue
        absolute = urljoin(base_url, href)
        if _origin_of(absolute) != origin or absolute in seen:
            continue
        if not _looks_interesting(absolute, anchor.get("text", "")):
            continue
        seen.add(absolute)
        matches.append(absolute)
        if len(matches) >= _MAX_LINKED_PAGES:
            break
    return matches


def _origin_of(url: str) -> tuple[str, str]:
    parts = urlsplit(url)
    return (parts.scheme, (parts.hostname or "").lower())


def _looks_interesting(url: str, text: str) -> bool:
    haystack = f"{url} {text}".lower()
    return any(keyword in haystack for keyword in _INTERESTING_PAGE_KEYWORDS)


def extract_copy_samples(html_text: str) -> list[CopySample]:
    doc = _parse(html_text)
    samples = [
        *_clean_samples(doc.h1[:1], DiscoverySource.HERO_HEADLINE),
        *_clean_samples(
            [h for h in doc.h2 if len(h) <= _MAX_TAGLINE_LENGTH][:3], DiscoverySource.TAGLINE
        ),
        *_clean_samples(_cta_texts(doc), DiscoverySource.CTA_TEXT),
    ]
    return samples[:_MAX_COPY_SAMPLES]


def _cta_texts(doc: _DocumentParser) -> list[str]:
    return [
        a["text"]
        for a in doc.anchors
        if _CTA_HINT_PATTERN.search(a.get("class", "") + " " + a.get("id", ""))
        and _MIN_CTA_LENGTH <= len(a.get("text", "")) <= _MAX_CTA_LENGTH
    ][:5]


def _clean_samples(texts: list[str], source: DiscoverySource) -> list[CopySample]:
    """Recorta a `_MAX_SAMPLE_TEXT_LENGTH` ANTES de `strip_pii` (F-1): un
    `<h1>` hostil de hasta 2 MiB no tiene por que llegar entero al regex
    de email/telefono solo para tirar el resultado a 280 caracteres
    despues -- recortar primero es, ademas, mas barato."""
    samples: list[CopySample] = []
    for text in texts:
        cleaned = strip_pii(text[:_MAX_SAMPLE_TEXT_LENGTH])
        if not cleaned:
            continue
        try:
            samples.append(CopySample(source=source, text=cleaned))
        except (BlankFieldError, PiiDetectedInCopySampleError, ValueError):
            continue
    return samples

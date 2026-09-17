"""Objetos de valor compartidos del paquete de campana (data-model.md
"Objetos de valor"). Cero imports de framework; cada uno valida su propia
forma en el constructor -- hace irrepresentable un dato invalido en vez de
confiar en que alguien lo compruebe despues.

Reutiliza, nunca reimplementa: `google_search_targeting.valid_keywords`
para `KeywordPlan` (mismas cotas 1..50, sin duplicados). `LandingUrl`
aplica el mismo criterio que `ad_child_creation._url` (HTTPS, host
publico, sin credenciales, sin fragmento) con una implementacion propia:
`_url` es privada de `proposals.domain` y no forma parte de la lista de
reuso explicita de T014 (`creation_budget`, `validate_child_payload`), que
si se reutiliza en `platform_completeness.py`."""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal
from enum import StrEnum
from ipaddress import ip_address
from urllib.parse import urlsplit

from safent_ads.creative.domain.identifiers import AssetId
from safent_ads.packages.domain.errors import PackageBudgetError, PlannedTreeError
from safent_ads.proposals.domain.google_search_targeting import valid_keywords
from safent_ads.proposals.domain.money import Money

_CONTROL_CHARACTER_BOUNDARY = 32


def _require_text(value: str, maximum: int, *, field: str) -> None:
    if (
        not value.strip()
        or len(value) > maximum
        or any(ord(char) < _CONTROL_CHARACTER_BOUNDARY for char in value)
    ):
        raise PlannedTreeError(f"{field}_invalid")


def _require_text_tuple(
    values: tuple[str, ...], minimum: int, maximum: int, item_maximum: int, *, field: str
) -> None:
    if not (minimum <= len(values) <= maximum):
        raise PlannedTreeError(f"{field}_count_invalid")
    for value in values:
        _require_text(value, item_maximum, field=field)
    if len(set(values)) != len(values):
        raise PlannedTreeError(f"{field}_duplicate")


# ---------------------------------------------------------------------------
# LandingUrl
# ---------------------------------------------------------------------------

_MAX_URL_LENGTH = 2048
_HTTPS_PORT = 443
_LOCAL_HOST_SUFFIXES = (".local", ".internal", ".localhost")


def _is_valid_https_url(value: str) -> bool:
    if not value or len(value) > _MAX_URL_LENGTH:
        return False
    try:
        parsed = urlsplit(value)
    except ValueError:
        return False
    host = parsed.hostname or ""
    if not (
        parsed.scheme == "https"
        and parsed.port in (None, _HTTPS_PORT)
        and not parsed.username
        and not parsed.password
        and not parsed.fragment
        and "." in host
        and not host.endswith(_LOCAL_HOST_SUFFIXES)
    ):
        return False
    try:
        address = ip_address(host)
    except ValueError:
        return bool(re.fullmatch(r"[A-Za-z0-9.-]+", host))
    return address.is_global


@dataclass(frozen=True, slots=True)
class LandingUrl:
    """Destino validado (invariante 4, parte HTTPS/host/credenciales/
    fragmento). El cruce de dominio registrable con la oferta o la marca
    exige datos externos y vive en `LandingDomainPolicyPort` (T020/T021),
    fuera de esta capa pura."""

    value: str

    def __post_init__(self) -> None:
        if not _is_valid_https_url(self.value):
            raise PlannedTreeError("landing_url_invalid")

    def __str__(self) -> str:
        return self.value


# ---------------------------------------------------------------------------
# Audience
# ---------------------------------------------------------------------------

_MAX_AUDIENCE_PLAIN_LENGTH = 200


@dataclass(frozen=True, slots=True)
class Audience:
    """A quien se le enseña, en castellano llano y en la forma nativa que
    exige la plataforma -- las dos caras del mismo hecho, nunca una sin la
    otra (data-model.md)."""

    plain: str
    native: dict[str, object]

    def __post_init__(self) -> None:
        _require_text(self.plain, _MAX_AUDIENCE_PLAIN_LENGTH, field="audience_plain")
        if not self.native:
            raise PlannedTreeError("audience_native_required")

    def to_canonical(self) -> dict[str, object]:
        return {"plain": self.plain, "native": dict(self.native)}


# ---------------------------------------------------------------------------
# KeywordPlan (solo Google)
# ---------------------------------------------------------------------------


class MatchType(StrEnum):
    EXACT = "EXACT"
    PHRASE = "PHRASE"
    BROAD = "BROAD"


@dataclass(frozen=True, slots=True)
class Keyword:
    text: str
    match_type: MatchType


@dataclass(frozen=True, slots=True)
class KeywordPlan:
    """Solo Google: que se compra y con que concordancia. Delega en
    `valid_keywords` -- nunca reimplementa sus cotas (1..50, sin
    duplicados, sin caracteres de control)."""

    keywords: tuple[Keyword, ...]

    def __post_init__(self) -> None:
        if not valid_keywords(self.to_canonical()):
            raise PlannedTreeError("keyword_plan_invalid")

    @property
    def plain(self) -> tuple[str, ...]:
        return tuple(keyword.text for keyword in self.keywords)

    def to_canonical(self) -> list[dict[str, object]]:
        return [
            {"text": keyword.text, "match_type": keyword.match_type.value}
            for keyword in self.keywords
        ]


# ---------------------------------------------------------------------------
# AdCopyPlan
# ---------------------------------------------------------------------------

_MAX_PRIMARY_TEXT_LENGTH = 2000
_MAX_META_HEADLINE_LENGTH = 128
_MAX_META_DESCRIPTION_LENGTH = 256


@dataclass(frozen=True, slots=True)
class MetaAdCopyPlan:
    primary_text: str
    headline: str
    description: str

    def __post_init__(self) -> None:
        _require_text(
            self.primary_text, _MAX_PRIMARY_TEXT_LENGTH, field="meta_ad_copy_primary_text"
        )
        _require_text(self.headline, _MAX_META_HEADLINE_LENGTH, field="meta_ad_copy_headline")
        _require_text(
            self.description, _MAX_META_DESCRIPTION_LENGTH, field="meta_ad_copy_description"
        )

    def to_canonical(self) -> dict[str, object]:
        return {
            "primary_text": self.primary_text,
            "headline": self.headline,
            "description": self.description,
        }


_MIN_RSA_HEADLINES = 3
_MAX_RSA_HEADLINES = 15
_MAX_RSA_HEADLINE_LENGTH = 30
_MIN_RSA_DESCRIPTIONS = 2
_MAX_RSA_DESCRIPTIONS = 4
_MAX_RSA_DESCRIPTION_LENGTH = 90


@dataclass(frozen=True, slots=True)
class GoogleAdCopyPlan:
    headlines: tuple[str, ...]
    descriptions: tuple[str, ...]

    def __post_init__(self) -> None:
        _require_text_tuple(
            self.headlines,
            _MIN_RSA_HEADLINES,
            _MAX_RSA_HEADLINES,
            _MAX_RSA_HEADLINE_LENGTH,
            field="google_ad_copy_headline",
        )
        _require_text_tuple(
            self.descriptions,
            _MIN_RSA_DESCRIPTIONS,
            _MAX_RSA_DESCRIPTIONS,
            _MAX_RSA_DESCRIPTION_LENGTH,
            field="google_ad_copy_description",
        )

    def to_canonical(self) -> dict[str, object]:
        return {"headlines": list(self.headlines), "descriptions": list(self.descriptions)}


AdCopyPlan = MetaAdCopyPlan | GoogleAdCopyPlan


# ---------------------------------------------------------------------------
# AdCreativeRef
# ---------------------------------------------------------------------------

_MAX_CHECKSUM_LENGTH = 128
_MAX_PREVIEW_KEY_LENGTH = 256
_MAX_MIME_TYPE_LENGTH = 64
_MIN_DIMENSION_PX = 1
_MAX_DIMENSION_PX = 20000


@dataclass(frozen=True, slots=True)
class ImageCreativeRef:
    """Meta: obligatoria. Google: admitida pero no exigida (RSA es texto
    puro). `preview_key` sirve `GET /creative-previews/{key}` -- la imagen
    en si nunca se guarda en `packages` (data-model.md). `mime_type`/
    `width`/`height` entran en `to_canonical` (data-model.md Revision 2,
    R2.1): la huella cubre el contenido exacto del activo, no solo su
    identidad; `preview_key` queda fuera por ser una clave de enrutado del
    servidor, no contenido firmado."""

    asset_id: AssetId
    checksum: str
    preview_key: str
    mime_type: str
    width: int
    height: int

    def __post_init__(self) -> None:
        _require_text(self.checksum, _MAX_CHECKSUM_LENGTH, field="creative_checksum")
        _require_text(self.preview_key, _MAX_PREVIEW_KEY_LENGTH, field="creative_preview_key")
        _require_text(self.mime_type, _MAX_MIME_TYPE_LENGTH, field="creative_mime_type")
        if not (_MIN_DIMENSION_PX <= self.width <= _MAX_DIMENSION_PX):
            raise PlannedTreeError("creative_width_invalid")
        if not (_MIN_DIMENSION_PX <= self.height <= _MAX_DIMENSION_PX):
            raise PlannedTreeError("creative_height_invalid")

    def to_canonical(self) -> dict[str, object]:
        return {
            "kind": "image",
            "asset_id": str(self.asset_id),
            "checksum": self.checksum,
            "mime_type": self.mime_type,
            "width": self.width,
            "height": self.height,
        }


@dataclass(frozen=True, slots=True)
class TextOnlyCreativeRef:
    """Google RSA: sin imagen obligatoria."""

    def to_canonical(self) -> dict[str, object]:
        return {"kind": "text_only"}


AdCreativeRef = ImageCreativeRef | TextOnlyCreativeRef


# ---------------------------------------------------------------------------
# AssetGroupAssetPlan (solo PERFORMANCE_MAX -- data-model.md, tasks.md T022)
# ---------------------------------------------------------------------------

_MIN_ASSET_GROUP_HEADLINES = 3
_MAX_ASSET_GROUP_HEADLINES = 15
_MAX_ASSET_GROUP_HEADLINE_LENGTH = 30
_MIN_ASSET_GROUP_LONG_HEADLINES = 1
_MAX_ASSET_GROUP_LONG_HEADLINES = 5
_MAX_ASSET_GROUP_LONG_HEADLINE_LENGTH = 90
_MIN_ASSET_GROUP_DESCRIPTIONS = 2
_MAX_ASSET_GROUP_DESCRIPTIONS = 5
_MAX_ASSET_GROUP_DESCRIPTION_LENGTH = 90
_MAX_ASSET_GROUP_FIRST_DESCRIPTION_LENGTH = 60
_MAX_ASSET_GROUP_BUSINESS_NAME_LENGTH = 25
_ASPECT_RATIO_TOLERANCE = Decimal("0.02")
_SQUARE_ASPECT_RATIO = Decimal(1)
_MARKETING_IMAGE_ASPECT_RATIO = Decimal("1.91")
# Nombre de negocio + logo + imagen de marketing + imagen cuadrada: cuatro
# recursos fijos, ademas de los tres bloques de texto de longitud variable.
_ASSET_GROUP_FIXED_ASSET_COUNT = 4

# Aritmetica explicita (threat-model.md D-1/AL-6): un `asset.create` y un
# `asset_group_asset.create` (el enlace) por cada recurso, mas la propia
# creacion del `asset_group` -- nunca "25 a ojo" (research.md), que no
# cuadraba con las cotas reales de este mismo objeto de valor. Reutilizable
# por el bróker (T034) como defensa en profundidad, antes de mutar.
_MAX_ASSET_GROUP_RESOURCES = (
    _MAX_ASSET_GROUP_HEADLINES
    + _MAX_ASSET_GROUP_LONG_HEADLINES
    + _MAX_ASSET_GROUP_DESCRIPTIONS
    + _ASSET_GROUP_FIXED_ASSET_COUNT
)
MAX_ASSET_GROUP_OPERATIONS = 2 * _MAX_ASSET_GROUP_RESOURCES + 1


def _asset_group_operation_count(resource_count: int) -> int:
    return 2 * resource_count + 1


def _require_aspect_ratio(image: ImageCreativeRef, expected: Decimal, *, field: str) -> None:
    actual = Decimal(image.width) / Decimal(image.height)
    if abs(actual - expected) > expected * _ASPECT_RATIO_TOLERANCE:
        raise PlannedTreeError(f"{field}_aspect_ratio_invalid")


@dataclass(frozen=True, slots=True)
class AssetGroupAssetPlan:
    """Solo PERFORMANCE_MAX: el bloque de titulares, descripciones e
    imagenes que ES el anuncio (data-model.md `AssetGroupAssetPlan`). La
    relacion de aspecto se comprueba aqui, en dominio, para que una imagen
    mal recortada falle ANTES de subirse -- el paso caro nunca se gasta en
    vano (threat-model.md T-10)."""

    headlines: tuple[str, ...]
    long_headlines: tuple[str, ...]
    descriptions: tuple[str, ...]
    business_name: str
    logo: ImageCreativeRef
    marketing_image: ImageCreativeRef
    square_image: ImageCreativeRef

    def __post_init__(self) -> None:
        _require_text_tuple(
            self.headlines,
            _MIN_ASSET_GROUP_HEADLINES,
            _MAX_ASSET_GROUP_HEADLINES,
            _MAX_ASSET_GROUP_HEADLINE_LENGTH,
            field="asset_group_headline",
        )
        _require_text_tuple(
            self.long_headlines,
            _MIN_ASSET_GROUP_LONG_HEADLINES,
            _MAX_ASSET_GROUP_LONG_HEADLINES,
            _MAX_ASSET_GROUP_LONG_HEADLINE_LENGTH,
            field="asset_group_long_headline",
        )
        _require_text_tuple(
            self.descriptions,
            _MIN_ASSET_GROUP_DESCRIPTIONS,
            _MAX_ASSET_GROUP_DESCRIPTIONS,
            _MAX_ASSET_GROUP_DESCRIPTION_LENGTH,
            field="asset_group_description",
        )
        if len(self.descriptions[0]) > _MAX_ASSET_GROUP_FIRST_DESCRIPTION_LENGTH:
            raise PlannedTreeError("asset_group_first_description_too_long")
        _require_text(
            self.business_name,
            _MAX_ASSET_GROUP_BUSINESS_NAME_LENGTH,
            field="asset_group_business_name",
        )
        _require_aspect_ratio(self.logo, _SQUARE_ASPECT_RATIO, field="asset_group_logo")
        _require_aspect_ratio(
            self.marketing_image,
            _MARKETING_IMAGE_ASPECT_RATIO,
            field="asset_group_marketing_image",
        )
        _require_aspect_ratio(
            self.square_image, _SQUARE_ASPECT_RATIO, field="asset_group_square_image"
        )
        resource_count = (
            len(self.headlines)
            + len(self.long_headlines)
            + len(self.descriptions)
            + _ASSET_GROUP_FIXED_ASSET_COUNT
        )
        if _asset_group_operation_count(resource_count) > MAX_ASSET_GROUP_OPERATIONS:
            raise PlannedTreeError("asset_group_too_large")

    @property
    def images(self) -> tuple[ImageCreativeRef, ImageCreativeRef, ImageCreativeRef]:
        return (self.logo, self.marketing_image, self.square_image)

    def to_canonical(self) -> dict[str, object]:
        return {
            "headlines": list(self.headlines),
            "long_headlines": list(self.long_headlines),
            "descriptions": list(self.descriptions),
            "business_name": self.business_name,
            "logo": self.logo.to_canonical(),
            "marketing_image": self.marketing_image.to_canonical(),
            "square_image": self.square_image.to_canonical(),
        }


# ---------------------------------------------------------------------------
# PublishAs (data-model.md Revision 2, R2.1)
# ---------------------------------------------------------------------------

_MAX_PAGE_ID_LENGTH = 128
_MAX_PAGE_NAME_LENGTH = 128


@dataclass(frozen=True, slots=True)
class MetaPublishAs:
    """La pagina desde la que se publica en Meta -- ya existe, el dueño la
    ve en el panel («Se publicará como «Clínica X»»); no la crea la saga,
    por eso se firma (`package_hash` R2.1: `publish_as`). Google no tiene
    equivalente: `CampaignPackage.publish_as` es `None` en esa plataforma."""

    page_id: str
    page_name: str

    def __post_init__(self) -> None:
        _require_text(self.page_id, _MAX_PAGE_ID_LENGTH, field="publish_as_page_id")
        _require_text(self.page_name, _MAX_PAGE_NAME_LENGTH, field="publish_as_page_name")

    def to_canonical(self) -> dict[str, object]:
        return {"page_id": self.page_id, "page_name": self.page_name}


# ---------------------------------------------------------------------------
# CallToAction
# ---------------------------------------------------------------------------


class CallToAction(StrEnum):
    LEARN_MORE = "LEARN_MORE"
    SHOP_NOW = "SHOP_NOW"
    SIGN_UP = "SIGN_UP"
    CONTACT_US = "CONTACT_US"
    BOOK_TRAVEL = "BOOK_TRAVEL"


# ---------------------------------------------------------------------------
# PackageBudget
# ---------------------------------------------------------------------------

_MONTH_DAYS = Decimal(30)


@dataclass(frozen=True, slots=True)
class PackageBudget:
    """`daily × duration_days = total_cap`, el numero que el dueno mira
    (data-model.md `total_cap`). Construida solo via `derive` para que la
    derivacion sea intrinseca -- nunca dos numeros que podrian divergir."""

    daily: Money
    monthly_equivalent: Money
    total_cap: Money
    envelope_headroom: Money | None = None
    envelope_reason: str | None = None

    def __post_init__(self) -> None:
        if not self.daily.is_positive():
            raise PackageBudgetError("package_budget_daily_not_positive")

    @classmethod
    def derive(
        cls,
        *,
        daily: Money,
        duration_days: int,
        account_daily_cap: Money | None = None,
        envelope_headroom: Money | None = None,
        envelope_reason: str | None = None,
    ) -> PackageBudget:
        """Invariante 5: `daily_budget > 0`; `daily_budget <= tope diario
        de la cuenta`; `total_cap <= headroom` cuando el sobre lo conoce.
        `account_daily_cap`/`envelope_headroom` llegan ya resueltos por la
        capa de aplicacion (`AccountDailyCapPort`, `BudgetEnvelopeReadPort`,
        T020/T021) -- este metodo no hace ninguna llamada externa."""
        if not daily.is_positive():
            raise PackageBudgetError("package_budget_daily_not_positive")
        if account_daily_cap is not None and daily > account_daily_cap:
            raise PackageBudgetError("package_budget_exceeds_account_daily_cap")
        total_cap = daily.scaled_by(Decimal(duration_days))
        if envelope_headroom is not None and total_cap > envelope_headroom:
            raise PackageBudgetError("package_budget_exceeds_envelope")
        return cls(
            daily=daily,
            monthly_equivalent=daily.scaled_by(_MONTH_DAYS),
            total_cap=total_cap,
            envelope_headroom=envelope_headroom,
            envelope_reason=envelope_reason,
        )

    def to_canonical(self) -> dict[str, object]:
        return {
            "daily": self.daily,
            "monthly_equivalent": self.monthly_equivalent,
            "total_cap": self.total_cap,
            "envelope_headroom": self.envelope_headroom,
            "envelope_reason": self.envelope_reason,
        }


# ---------------------------------------------------------------------------
# PackageRationale
# ---------------------------------------------------------------------------

_MAX_OWNER_REQUEST_LENGTH = 500
_MAX_WHY_LENGTH = 500


@dataclass(frozen=True, slots=True)
class PackageRationale:
    """El porque (FR-15): el encargo literal del dueño y el resumen llano.
    `success_criterion`/`kill_criterion` no se duplican aqui: viven en
    `PlannedCampaign` como fuente unica (ver nota de implementacion --
    `contracts/mcp-tools.md §2 RationaleArgs` solo declara `owner_request`/
    `why`; data-model.md los listaba tambien en `PackageRationale`, lo que
    habria creado dos copias que podrian divergir)."""

    owner_request: str
    why: str

    def __post_init__(self) -> None:
        _require_text(
            self.owner_request, _MAX_OWNER_REQUEST_LENGTH, field="rationale_owner_request"
        )
        _require_text(self.why, _MAX_WHY_LENGTH, field="rationale_why")

    def to_canonical(self) -> dict[str, object]:
        return {"owner_request": self.owner_request, "why": self.why}


# ---------------------------------------------------------------------------
# ResearchSummary
# ---------------------------------------------------------------------------


class ResearchNoteKind(StrEnum):
    BRAND = "brand"
    CATALOG = "catalog"
    CRM = "crm"
    RESULTS = "results"
    WEB = "web"
    META_AD_LIBRARY = "meta_ad_library"


_INTERNAL_RESEARCH_KINDS = frozenset(
    {
        ResearchNoteKind.BRAND,
        ResearchNoteKind.CATALOG,
        ResearchNoteKind.CRM,
        ResearchNoteKind.RESULTS,
    }
)
_EXTERNAL_RESEARCH_KINDS = frozenset({ResearchNoteKind.WEB, ResearchNoteKind.META_AD_LIBRARY})
_MAX_RESEARCH_SUMMARY_LENGTH = 300
_MAX_RESEARCH_NOTES = 6


@dataclass(frozen=True, slots=True)
class ResearchNote:
    kind: ResearchNoteKind
    summary: str
    observed_at: datetime
    url: str | None = None

    def __post_init__(self) -> None:
        _require_text(self.summary, _MAX_RESEARCH_SUMMARY_LENGTH, field="research_note_summary")
        if self.url is not None and not _is_valid_https_url(self.url):
            raise PlannedTreeError("research_note_url_invalid")

    def to_canonical(self) -> dict[str, object]:
        return {
            "kind": self.kind.value,
            "summary": self.summary,
            "url": self.url,
            "observed_at": self.observed_at.isoformat(),
        }


@dataclass(frozen=True, slots=True)
class ResearchSummary:
    """Que se miro (propio y competencia) y de donde. Ausencia = `None` en
    `CampaignPackage.research`, que el panel pinta como "Sin datos de
    competencia" -- nunca se rellena sola (data-model.md)."""

    internal: tuple[ResearchNote, ...] = ()
    external: tuple[ResearchNote, ...] = ()

    def __post_init__(self) -> None:
        if len(self.internal) > _MAX_RESEARCH_NOTES:
            raise PlannedTreeError("research_internal_too_many")
        if len(self.external) > _MAX_RESEARCH_NOTES:
            raise PlannedTreeError("research_external_too_many")
        if any(note.kind not in _INTERNAL_RESEARCH_KINDS for note in self.internal):
            raise PlannedTreeError("research_internal_kind_invalid")
        if any(note.kind not in _EXTERNAL_RESEARCH_KINDS for note in self.external):
            raise PlannedTreeError("research_external_kind_invalid")

    def to_canonical(self) -> dict[str, object]:
        return {
            "internal": [note.to_canonical() for note in self.internal],
            "external": [note.to_canonical() for note in self.external],
        }

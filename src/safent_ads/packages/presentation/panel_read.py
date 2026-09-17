"""`GET /packages`, `GET /packages/{id}` (tasks.md T030; contracts/api.md
§2/§3). Los textos llanos (`objective_label`, `native_summary`,
`audience_plain`'s hermanos `geo_plain`/`schedule_plain`/`keywords_plain`/
`bid_plain`) se redactan AQUI -- es presentacion, no dominio (plan.md
§"Module design": "panel y Telegram dicen exactamente lo mismo")."""

from __future__ import annotations

from datetime import datetime, timedelta
from decimal import Decimal
from typing import Annotated, Any
from urllib.parse import urlsplit

from fastapi import APIRouter, Depends, Query
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from safent_ads.creative.application.ports import AssetStorePort
from safent_ads.creative.domain.storage import StorageUri
from safent_ads.iam.presentation.errors import ApiError
from safent_ads.packages.application.ports import PackagePublicationRecord, PackageStepRepository
from safent_ads.packages.domain.approval_envelope import StepKind
from safent_ads.packages.domain.campaign_package import CampaignPackage, PackageState
from safent_ads.packages.domain.identifiers import PackageId, PackageIdFormatError
from safent_ads.packages.domain.planned_tree import (
    AdSetNative,
    CampaignObjective,
    EuPoliticalAdvertisingDeclaration,
    GoogleAdGroupNative,
    GoogleAssetGroupNative,
    GoogleCampaignNative,
    MetaAdSetNative,
    MetaCampaignNative,
    PlannedAd,
    PlannedAdSet,
    PlannedCampaign,
)
from safent_ads.packages.domain.values import (
    CallToAction,
    GoogleAdCopyPlan,
    ImageCreativeRef,
    KeywordPlan,
    MetaAdCopyPlan,
    PackageBudget,
    PackageRationale,
    ResearchSummary,
)
from safent_ads.packages.infrastructure.sql_package_repository import SqlCampaignPackageRepository
from safent_ads.packages.infrastructure.sql_package_step_repository import (
    SqlPackageStepRepository,
)
from safent_ads.packages.infrastructure.sql_publication_repository import (
    SqlPackagePublicationRepository,
)
from safent_ads.panel.presentation.deps import require_business_access
from safent_ads.proposals.domain.google_bidding import (
    GoogleBidding,
    ManualCpc,
    MaximizeClicks,
    MaximizeConversions,
)
from safent_ads.proposals.domain.google_channel_spec import (
    CHANNEL_SPECS,
    GoogleAdvertisingChannelType,
)
from safent_ads.proposals.domain.money import Money
from safent_ads.shared.ids import BusinessId

__all__ = ["build_package_read_router"]

_PREVIEW_TTL_SECONDS = 600  # 10 min, mismo criterio que creative.presentation.router
_KEYWORDS_SHOWN = 8
_GRACE_SECONDS = 45
_MAX_OWNER_CONTEXT_LENGTH = 500
_VISIBLE_STEP_KINDS = frozenset(
    {
        StepKind.CREATE_CAMPAIGN,
        StepKind.CREATE_AD_SET,
        StepKind.CREATE_AD,
        StepKind.ACTIVATE_CAMPAIGN,
    }
)

_OBJECTIVE_LABELS: dict[CampaignObjective, str] = {
    CampaignObjective.RESERVATIONS: "Conseguir reservas",
    CampaignObjective.LEADS: "Conseguir clientes potenciales",
    CampaignObjective.SALES: "Conseguir ventas",
    CampaignObjective.TRAFFIC: "Llevar tráfico al sitio",
    CampaignObjective.CALLS: "Conseguir llamadas",
    CampaignObjective.AWARENESS: "Dar a conocer la marca",
}

_CTA_LABELS: dict[CallToAction, str] = {
    CallToAction.LEARN_MORE: "Más información",
    CallToAction.SHOP_NOW: "Comprar ahora",
    CallToAction.SIGN_UP: "Registrarse",
    CallToAction.CONTACT_US: "Contactar",
    CallToAction.BOOK_TRAVEL: "Reservar",
}

# Assumption documentada: nombres humanos completos solo para los paises
# que hoy tiene sentido anunciar (spec.md Assumptions §2, EUR/es-ES).
# Cualquier otro codigo se enseña tal cual -- nunca se inventa un nombre.
_COUNTRY_NAMES: dict[str, str] = {"ES": "España", "PT": "Portugal", "FR": "Francia", "IT": "Italia"}

_NOT_FOUND = ApiError(status_code=404, code="NOT_FOUND", message="No encontrado.")

# tasks.md T036 -- etiqueta de canal en lenguaje del dueño (data-model.md
# `GoogleCampaignNative` union de cuatro variantes). Claves = miembros del
# enum de `google_channel_spec.py` (INV-15: nunca el literal de cadena
# suelto, ver tests/architecture/test_no_channel_literals_outside_spec.py).
_CHANNEL_LABELS: dict[GoogleAdvertisingChannelType, str] = {
    GoogleAdvertisingChannelType.SEARCH: "Búsqueda",
    GoogleAdvertisingChannelType.DISPLAY: "Display",
    GoogleAdvertisingChannelType.DEMAND_GEN: "Generación de demanda",
    GoogleAdvertisingChannelType.PERFORMANCE_MAX: "Máximo rendimiento",
}

# Etiquetas de fila de `native_summary` que el panel (T037) reconoce para
# pintarlas desplegadas por defecto -- nunca plegadas como el resto del
# array (casilla 23 de threat-model.md §8; mismo criterio que 003 ME-3).
_CHANNEL_LABEL_KEY = "Canal"
_BIDDING_LABEL_KEY = "Tipo y puja"
_OPTIMIZES_FOR_LABEL_KEY = "Optimiza para"
_AUTOMATION_LABEL_KEY = "Automatización"
_SPEND_NOTICE_LABEL_KEY = "Aviso de gasto"
_ASSET_GROUP_COUNT_LABEL_KEY = "Grupos de recursos"
_EU_POLITICAL_LABEL_KEY = "Publicidad política UE"

_AUTOMATION_NOTICE = (
    "Sólo se anuncia lo que has aprobado: ni páginas ni textos añadidos por Google."
)

# Clave del literal forzado en `google_channel_spec.CHANNEL_SPECS` (T021,
# `_AUTOMATION_OFF`) -- nunca reimplementado aqui: si mañana Busqueda o
# Display tambien apagan la automatizacion de texto, esta fila aparece sola
# sin tocar `panel_read.py` (INV-15, unica fuente de verdad de la fila).
_URL_EXPANSION_OPT_OUT_KEY = "url_expansion_opt_out"

_ASSET_GROUP_IMAGE_ALT: dict[str, str] = {
    "logo": "Logo",
    "marketing_image": "Imagen de marketing",
    "square_image": "Imagen cuadrada",
}


def _parse_state_filter(raw: str) -> PackageState:
    """M3 (revision de codigo): un `state` de query invalido es entrada del
    cliente, no un bug del servidor -- `422`, nunca el `500` que
    `PackageState(raw)` lanzaria sin capturar."""
    try:
        return PackageState(raw)
    except ValueError as exc:
        raise ApiError(
            status_code=422, code="VALIDATION_ERROR", message="state invalido"
        ) from exc


def _parse_package_id(raw: str) -> PackageId:
    """Un `package_id` que ni siquiera tiene forma de ULID es tan
    inexistente como uno que no esta en la base -- mismo `404` uniforme
    (contracts/api.md §2), nunca una excepcion de formato sin capturar."""
    try:
        return PackageId.parse(raw)
    except PackageIdFormatError as exc:
        raise _NOT_FOUND from exc


def build_package_read_router(
    session_factory: async_sessionmaker[AsyncSession], asset_store: AssetStorePort
) -> APIRouter:
    router = APIRouter(prefix="/api/v1/packages", tags=["packages"])

    @router.get("")
    async def list_packages(
        business_id: Annotated[str, Depends(require_business_access)],
        state: str | None = None,
        limit: Annotated[int, Query(ge=1, le=100)] = 50,
        cursor: str | None = None,
    ) -> dict[str, Any]:
        parsed_state = _parse_state_filter(state) if state is not None else None
        async with session_factory() as session:
            items, next_cursor = await SqlCampaignPackageRepository(session).list_for_business(
                business_id=BusinessId.parse(business_id),
                state=parsed_state,
                limit=limit,
                cursor=cursor,
            )
        return {
            "items": [
                {
                    "package_id": str(item.package_id),
                    "state": item.state.value,
                    "platform": item.platform.value,
                    "account_name": item.account_name,
                    "campaign_name": item.campaign_name,
                    "ads_count": item.ads_count,
                    "money": {
                        "daily": item.daily_budget.to_canonical(),
                        "total_cap": item.total_cap.to_canonical(),
                    },
                    "created_at": item.created_at.isoformat(),
                    "expires_at": item.expires_at.isoformat(),
                }
                for item in items
            ],
            "next_cursor": next_cursor,
        }

    @router.get("/{package_id}")
    async def get_package(
        package_id: str, business_id: Annotated[str, Depends(require_business_access)]
    ) -> dict[str, Any]:
        async with session_factory() as session:
            package = await SqlCampaignPackageRepository(session).get(
                _parse_package_id(package_id), business_id=BusinessId.parse(business_id)
            )
            if package is None:
                raise _NOT_FOUND
            publication = await SqlPackagePublicationRepository(session).get_by_package_id(
                package.package_id
            )
            steps = SqlPackageStepRepository(session)
            return await _build_preview(package, publication, asset_store, steps)

    return router


async def _build_preview(
    package: CampaignPackage,
    publication: PackagePublicationRecord | None,
    asset_store: AssetStorePort,
    steps: PackageStepRepository,
) -> dict[str, Any]:
    ad_sets = [await _ad_set_preview(ad_set, asset_store) for ad_set in package.ad_sets]
    asset_group_count = sum(
        1 for ad_set in package.ad_sets if isinstance(ad_set.native, GoogleAssetGroupNative)
    )
    return {
        "package_id": str(package.package_id),
        "state": package.state.value,
        "package_hash": package.package_hash.value,
        "expires_at": package.expires_at.isoformat(),
        "approvable": _approvable(package),
        "not_approvable_reason": _not_approvable_reason(package),
        "platform": {
            "code": package.account_ref.platform.value,
            "label": package.account_ref.platform.value.capitalize(),
            # contracts/api.md §2 (`PackagePreview.platform`): `account` es
            # `{ entity_ref, name }` -- `name` no tiene todavia una etiqueta
            # propia en `platform_accounts` (accounts/application/
            # list_platform_accounts.py `_to_view`: "por defecto el
            # externo"), asi que hasta esa migracion el nombre legible ES
            # el `external_id` de la cuenta, nunca `None` ni el `entity_ref`
            # entero. `publish_as` es hermano de `account`, no su campo
            # (mismo contrato, R2.B): anidarlo ahi rompia el parseo zod del
            # panel real (`account.name` requerido y ausente).
            "account": {
                "entity_ref": str(package.account_ref),
                "name": package.account_ref.external_id,
            },
            "publish_as": (
                {"page_name": package.publish_as.page_name}
                if package.publish_as is not None
                else None
            ),
        },
        "campaign": {
            "name": package.campaign.name,
            "objective_label": _OBJECTIVE_LABELS[package.campaign.objective],
            "duration_label": f"{package.campaign.duration_days} días",
            "native_summary": _native_summary(
                package.campaign.native,
                objective_label=_OBJECTIVE_LABELS[package.campaign.objective],
                asset_group_count=asset_group_count,
                total_cap=package.budget.total_cap,
            ),
            "ad_sets": ad_sets,
        },
        "money": _money_block(package.budget),
        "why": _why_block(package.rationale, package.campaign, package.research),
        "on_approve": _on_approve_block(package),
        "publication": (
            await _publication_status(publication, steps) if publication is not None else None
        ),
    }


def _approvable(package: CampaignPackage) -> bool:
    return package.state is PackageState.PROPOSED


def _not_approvable_reason(package: CampaignPackage) -> str | None:
    if _approvable(package):
        return None
    labels = {
        PackageState.APPROVED: "Ya está aprobado, esperando a publicarse.",
        PackageState.PUBLISHING: "Se está publicando ahora mismo.",
        PackageState.PUBLISHED: "Ya está publicado y activo.",
        PackageState.REJECTED: "Se rechazó. Pide un paquete nuevo.",
        PackageState.EXPIRED: "Caducó. Pide un paquete nuevo.",
        PackageState.INVALIDATED: "Cambió después de aprobarse. Revísalo de nuevo.",
        PackageState.PARTIALLY_PUBLISHED: "Se publicó solo en parte.",
        PackageState.FAILED: "No se pudo publicar.",
        PackageState.VERIFYING: "Comprobando el resultado en la plataforma.",
    }
    return labels.get(package.state, "No se puede aprobar en este estado.")


def _maximize_clicks_plain(bidding: MaximizeClicks) -> str:
    ceiling = bidding.cpc_bid_ceiling
    if ceiling is None:
        return "Maximizar clics"
    return f"Maximizar clics, tope {ceiling.amount} € por clic"


def _maximize_conversions_plain(bidding: MaximizeConversions) -> str:
    target_cpa = bidding.target_cpa
    if target_cpa is None:
        return "Maximizar conversiones"
    return f"Maximizar conversiones, objetivo {target_cpa.amount} € por conversión"


def _maximize_conversion_value_plain(target_roas: Decimal | None) -> str:
    if target_roas is None:
        return "Maximizar el valor de conversión"
    return f"Maximizar el valor de conversión, objetivo ×{target_roas} de retorno"


def _google_bidding_plain(bidding: GoogleBidding) -> str:
    """T036 "estrategia de puja con su objetivo": nunca un texto fijo -- una
    fila de SEARCH con `MaximizeConversions` (permitido desde 005) no puede
    seguir diciendo «CPC manual» (bug que este cambio corrige de paso, sin
    tocar el `to_canonical` que firma el `package_hash`, casilla 3)."""
    if isinstance(bidding, ManualCpc):
        return "Puja manual por clic"
    if isinstance(bidding, MaximizeClicks):
        return _maximize_clicks_plain(bidding)
    if isinstance(bidding, MaximizeConversions):
        return _maximize_conversions_plain(bidding)
    return _maximize_conversion_value_plain(bidding.target_roas)


def _native_summary(
    native: GoogleCampaignNative | MetaCampaignNative,
    *,
    objective_label: str,
    asset_group_count: int,
    total_cap: Money,
) -> list[dict[str, str]]:
    if isinstance(native, GoogleCampaignNative):
        return _google_native_summary(
            native,
            objective_label=objective_label,
            asset_group_count=asset_group_count,
            total_cap=total_cap,
        )
    categories = ", ".join(category.value for category in native.special_ad_categories) or "Ninguna"
    return [
        {"label": "Objetivo y subasta", "value": f"{native.objective.value} · Puja más barata"},
        {"label": "Categorías especiales", "value": categories},
    ]


def _google_native_summary(
    native: GoogleCampaignNative,
    *,
    objective_label: str,
    asset_group_count: int,
    total_cap: Money,
) -> list[dict[str, str]]:
    """Casilla 23 (threat-model.md §8): canal, puja con objetivo, «Optimiza
    para» y el aviso de automatización se declaran SIEMPRE que apliquen --
    el panel (T037) los pinta desplegados, a diferencia de la política
    UE/categorías (última fila), que sigue bajo «Ajustes que exige la
    plataforma» sin cambios (contracts/api.md §7 punto 6)."""
    rows = [
        {"label": _CHANNEL_LABEL_KEY, "value": _CHANNEL_LABELS[native.advertising_channel_type]},
        {"label": _BIDDING_LABEL_KEY, "value": _google_bidding_plain(native.bidding_strategy)},
    ]
    if native.conversion_goals:
        # Nunca `conversion_goals[i].resource_name` (prohibido T036): la meta
        # que el dueño entiende es su propio objetivo de negocio, no el ID
        # opaco de Google Ads.
        rows.append({"label": _OPTIMIZES_FOR_LABEL_KEY, "value": objective_label})
    if native.bidding_strategy.is_conversion_based:
        rows.append(
            {
                "label": _SPEND_NOTICE_LABEL_KEY,
                "value": (
                    "Algunos días puede gastar hasta el doble del diario; "
                    f"el mes no pasa de {total_cap.amount} €."
                ),
            }
        )
    channel_spec = CHANNEL_SPECS[native.advertising_channel_type]
    if _URL_EXPANSION_OPT_OUT_KEY in channel_spec.forced_literals:
        rows.append({"label": _AUTOMATION_LABEL_KEY, "value": _AUTOMATION_NOTICE})
    if asset_group_count > 0:
        noun = "bloque" if asset_group_count == 1 else "bloques"
        rows.append(
            {"label": _ASSET_GROUP_COUNT_LABEL_KEY, "value": f"{asset_group_count} {noun}"}
        )
    eu_political = (
        "Contiene"
        if native.contains_eu_political_advertising is EuPoliticalAdvertisingDeclaration.CONTAINS
        else "No contiene"
    )
    rows.append({"label": _EU_POLITICAL_LABEL_KEY, "value": eu_political})
    return rows


def _node_label(native: AdSetNative) -> str:
    """T036 "etiqueta del nodo en lenguaje del dueño": el grupo de recursos
    nunca se cuenta como anuncio (mismo criterio que 003 ME-3)."""
    if isinstance(native, GoogleAssetGroupNative):
        return "Grupo de recursos"
    if isinstance(native, GoogleAdGroupNative):
        return "Grupo de anuncios"
    return "Conjunto de anuncios"


async def _ad_set_preview(ad_set: PlannedAdSet, asset_store: AssetStorePort) -> dict[str, Any]:
    ads = [await _ad_preview(ad, asset_store) for ad in ad_set.ads]
    asset_group = (
        await _asset_group_preview(ad_set.native, asset_store)
        if isinstance(ad_set.native, GoogleAssetGroupNative)
        else None
    )
    return {
        "local_ref": str(ad_set.local_ref),
        "name": ad_set.name,
        "node_label": _node_label(ad_set.native),
        "audience_plain": ad_set.audience.plain,
        "geo_plain": _geo_plain(ad_set.native),
        "schedule_plain": _schedule_plain(ad_set),
        "keywords_plain": _keywords_plain(ad_set.keywords),
        "keywords_note": _keywords_note(ad_set.keywords),
        "bid_plain": _bid_plain(ad_set.cpc_bid),
        "asset_group": asset_group,
        "ads": ads,
    }


async def _asset_group_preview(
    native: GoogleAssetGroupNative, asset_store: AssetStorePort
) -> dict[str, Any]:
    """T036: lo único que ES el anuncio en PERFORMANCE_MAX (`ads_per_node =
    (0, 0)`, ningún `AdPreview` debajo) -- sin `resource_name`,
    `connection_id` ni `page_id` (prohibido T036). `final_url` va completo
    porque no hay ningún `AdPreview.landing` que lo lleve; `audience_signal`
    no es un campo del dominio en v1 (`GoogleAssetGroupNative` docstring),
    así que el recuento es siempre 0, nunca `null` silencioso."""
    assets = native.assets
    images = [
        await _asset_group_image(image, asset_store, alt)
        for image, alt in (
            (assets.logo, _ASSET_GROUP_IMAGE_ALT["logo"]),
            (assets.marketing_image, _ASSET_GROUP_IMAGE_ALT["marketing_image"]),
            (assets.square_image, _ASSET_GROUP_IMAGE_ALT["square_image"]),
        )
    ]
    return {
        "business_name": assets.business_name,
        "images": images,
        "headlines": list(assets.headlines),
        "long_headlines": list(assets.long_headlines),
        "descriptions": list(assets.descriptions),
        "audience_signal_count": 0,
        "final_url": str(native.final_url),
    }


async def _asset_group_image(
    image: ImageCreativeRef, asset_store: AssetStorePort, alt: str
) -> dict[str, Any]:
    preview_url = await asset_store.signed_preview_url(
        StorageUri(image.preview_key), _PREVIEW_TTL_SECONDS
    )
    return {
        "preview_url": preview_url,
        "width": image.width,
        "height": image.height,
        "alt": alt,
        "asset_id": str(image.asset_id),
        "policy": "ok",
    }


def _geo_plain(native: GoogleAdGroupNative | GoogleAssetGroupNative | MetaAdSetNative) -> str:
    if isinstance(native, GoogleAdGroupNative | GoogleAssetGroupNative):
        return "Configurado en Google Ads"
    names = [_COUNTRY_NAMES.get(code, code) for code in native.countries]
    return ", ".join(names)


def _schedule_plain(ad_set: PlannedAdSet) -> str:
    if ad_set.schedule is None:
        return "Todos los días"
    days = ", ".join(str(day) for day in ad_set.schedule.weekdays)
    return f"{days}, {ad_set.schedule.start_time}-{ad_set.schedule.end_time}"


def _keywords_plain(keywords: KeywordPlan | None) -> list[str] | None:
    if keywords is None:
        return None
    return list(keywords.plain[:_KEYWORDS_SHOWN])


def _keywords_note(keywords: KeywordPlan | None) -> str | None:
    if keywords is None:
        return None
    total = len(keywords.keywords)
    if total <= _KEYWORDS_SHOWN:
        return f"Se compran {total} palabras clave."
    return f"Se compran {total} palabras clave (mostrando las primeras {_KEYWORDS_SHOWN})."


def _bid_plain(cpc_bid: Money | None) -> str | None:
    if cpc_bid is None:
        return None
    return f"Hasta {cpc_bid.amount} {cpc_bid.currency} por clic"


async def _ad_preview(ad: PlannedAd, asset_store: AssetStorePort) -> dict[str, Any]:
    image = None
    if isinstance(ad.creative, ImageCreativeRef):
        preview_url = await asset_store.signed_preview_url(
            StorageUri(ad.creative.preview_key), _PREVIEW_TTL_SECONDS
        )
        image = {
            "preview_url": preview_url,
            "width": ad.creative.width,
            "height": ad.creative.height,
            "alt": ad.name,
            "asset_id": str(ad.creative.asset_id),
            "policy": "ok",
        }
    return {
        "local_ref": str(ad.local_ref),
        "name": ad.name,
        "image": image,
        "texts": _copy_texts(ad.copy),
        "cta_label": _CTA_LABELS.get(ad.cta) if ad.cta is not None else None,
        "landing": {"url": str(ad.landing), "display": _short_url(str(ad.landing))},
        "actions": {"can_replace_image": image is not None, "can_regenerate": image is not None},
    }


def _copy_texts(copy: MetaAdCopyPlan | GoogleAdCopyPlan) -> list[dict[str, str]]:
    if isinstance(copy, MetaAdCopyPlan):
        return [
            {"label": "Titular", "value": copy.headline},
            {"label": "Texto", "value": copy.primary_text},
            {"label": "Descripción", "value": copy.description},
        ]
    texts = [{"label": "Titular", "value": headline} for headline in copy.headlines]
    texts.extend(
        {"label": "Descripción", "value": description} for description in copy.descriptions
    )
    return texts


def _short_url(url: str) -> str:
    parsed = urlsplit(url)
    path = parsed.path.rstrip("/")
    return f"{parsed.hostname}{path}"


def _money_block(budget: PackageBudget) -> dict[str, Any]:
    if budget.envelope_headroom is not None:
        envelope_label = f"Te queda {budget.envelope_headroom.amount} € de tope este mes."
    else:
        envelope_label = "Tope mensual no configurado."
    return {
        "daily": budget.daily.to_canonical(),
        "monthly_equivalent": budget.monthly_equivalent.to_canonical(),
        "total_cap": budget.total_cap.to_canonical(),
        "envelope": {
            "headroom": (
                budget.envelope_headroom.to_canonical()
                if budget.envelope_headroom is not None
                else None
            ),
            "reason": budget.envelope_reason,
            "label": envelope_label,
        },
        "cap_label": f"Nunca gastará más de {budget.total_cap.amount} € en total.",
    }


def _why_block(
    rationale: PackageRationale, campaign: PlannedCampaign, research: ResearchSummary | None
) -> dict[str, Any]:
    return {
        "owner_request": rationale.owner_request,
        "summary": rationale.why,
        "success_criterion": campaign.success_criterion,
        "kill_criterion": campaign.kill_criterion,
        "research": (
            {
                "internal": [
                    {
                        "kind": note.kind.value,
                        "summary": note.summary,
                        "observed_at": note.observed_at.isoformat(),
                    }
                    for note in research.internal
                ],
                "external": [
                    {
                        "kind": note.kind.value,
                        "summary": note.summary,
                        "url": note.url,
                        "observed_at": note.observed_at.isoformat(),
                    }
                    for note in research.external
                ],
            }
            if research is not None
            else None
        ),
    }


def _pmax_on_approve_sentence(
    asset_groups: list[GoogleAssetGroupNative], platform_label: str
) -> str:
    """Máximo rendimiento no crea «anuncios» -- el grupo de recursos ES el
    anuncio (`_node_label`, 003 ME-3), así que un paquete PMax con
    `ads_count == 0` nunca puede decir «0 anuncios» (design.md §13.1: nada
    de jerga de proveedor, y nada que suene a que no se va a crear nada)."""
    group_count = len(asset_groups)
    image_count = sum(len(group.assets.images) for group in asset_groups)
    noun = "grupo" if group_count == 1 else "grupos"
    return (
        f"Se crearán 1 campaña y {group_count} {noun} de recursos con {image_count} imágenes "
        f"en {platform_label}, y la campaña quedará activa."
    )


def _on_approve_block(package: CampaignPackage) -> dict[str, Any]:
    ads_count = sum(len(ad_set.ads) for ad_set in package.ad_sets)
    platform_label = package.account_ref.platform.value.capitalize()
    asset_groups = [
        ad_set.native
        for ad_set in package.ad_sets
        if isinstance(ad_set.native, GoogleAssetGroupNative)
    ]
    sentence = (
        _pmax_on_approve_sentence(asset_groups, platform_label)
        if asset_groups
        else (
            f"Se crearán 1 campaña, {len(package.ad_sets)} conjuntos y {ads_count} anuncios en "
            f"{platform_label}, y la campaña quedará activa."
        )
    )
    return {
        "creates": {"campaigns": 1, "ad_sets": len(package.ad_sets), "ads": ads_count},
        "activates": True,
        "sentence": sentence,
        "undo_sentence": (
            f"Tienes {_GRACE_SECONDS} segundos para cancelarlo entero. "
            "Después, «Deshacer» pausa la campaña."
        ),
        "grace_seconds": _GRACE_SECONDS,
    }


_HALT_REASON_PLAIN: dict[str, str] = {
    "package_changed_mid_publication": "El paquete cambió a mitad de publicación.",
    "brake_engaged": "Los cambios están parados.",
    "package_parent_unconfirmed": "Esperando confirmación del paso anterior.",
    "cancelled_by_owner": "Cancelada por el propietario.",
}


async def _publication_status(
    record: PackagePublicationRecord, steps: PackageStepRepository
) -> dict[str, Any]:
    """`done_count`/`total_count` cuentan solo los pasos VISIBLES (BL-6):
    `upload_creative` nunca se enseña, y mientras corre, la frase dice
    «Preparando las imágenes…» en vez de un recuento que el dueño no pidió
    entender."""
    envelope = record.envelope
    visible_steps = tuple(
        step for step in envelope.step_plan if step.step_kind in _VISIBLE_STEP_KINDS
    )
    done_count = 0
    campaign_entity_ref: str | None = None
    preparing_creative = False
    for step in envelope.step_plan:
        step_record = await steps.get(record.publication_id, step.step_index)
        is_done = step_record is not None and step_record.state == "done"
        if step.step_kind is StepKind.UPLOAD_CREATIVE and not is_done:
            preparing_creative = True
        if step.step_kind not in _VISIBLE_STEP_KINDS:
            continue
        if is_done:
            done_count += 1
        if step.step_kind is StepKind.CREATE_CAMPAIGN and step_record is not None:
            campaign_entity_ref = step_record.created_entity_ref

    progress_sentence = _progress_sentence(
        record.state, done_count, len(visible_steps), preparing=preparing_creative
    )
    return {
        "state": record.state,
        "done_count": done_count,
        "total_count": len(visible_steps),
        "progress_sentence": progress_sentence,
        "halted": _halted_block(record),
        "campaign_entity_ref": campaign_entity_ref,
        "activated_at": None,
        "undo_deadline": _cancel_deadline(record),
    }


def _progress_sentence(state: str, done_count: int, total_count: int, *, preparing: bool) -> str:
    if state == "completed":
        return "Campaña publicada y activa."
    if preparing and done_count == 0:
        return "Preparando las imágenes…"
    return f"Creado {done_count} de {total_count}. Nada está entregando todavía."


def _halted_block(record: PackagePublicationRecord) -> dict[str, Any] | None:
    if record.state != "halted":
        return None
    reason = record.halt_reason or "step_failed"
    reason_plain = _HALT_REASON_PLAIN.get(reason, "No se pudo continuar. Revisa el detalle.")
    return {
        "reason_plain": reason_plain,
        "next_step_plain": "Puedes continuar o revisarlo en Campañas.",
        "can_resume": True,
    }


def _cancel_deadline(record: PackagePublicationRecord) -> datetime | None:
    """FR-08: solo mientras la publicación sigue `pending` (dentro de los
    45 s de gracia, antes de que se haya escrito nada) hay una cuenta
    atrás real que enseñar. Tras eso, «Deshacer» pasa a pausar sin ventana
    propia (`plan.md` R2.5), así que no hay plazo que mostrar."""
    if record.state != "pending":
        return None
    return record.started_at + timedelta(seconds=_GRACE_SECONDS)

"""Completitud nativa por plataforma (data-model.md invariante 6). Delega
la validacion de forma en `proposals.domain.campaign_creation.creation_budget`
y `proposals.domain.ad_child_creation.validate_child_payload` -- nunca
reimplementa sus reglas (T014). Traduce sus excepciones a
`PlatformCompletenessError` en el borde del contexto acotado: un llamador
de `packages` no necesita importar tipos de `proposals` para capturar el
fallo.

Nota de implementacion (alcance de T014, actualizada por T112/R2.7): un
anuncio (`kind="ad"`) de Meta con creatividad inline exige `page_id` (de la
conexion de la cuenta) e `image_hash` (el manejador de plataforma que
devuelve el paso `UPLOAD_CREATIVE`, nunca una URL) -- datos que "el
servidor decide" (`contracts/mcp-tools.md §2`, seccion "Lo que el servidor
decide, no el modelo") via `ActiveAccountLookupPort`/`CreativeAssetLookupPort`,
fuera del alcance de un dominio puro. Por eso
esta capa delega SOLO lo que no exige E/S: completitud de campana
(`creation_budget`) y de conjunto/grupo (`validate_child_payload`,
`kind="ad_set"`, identica en ambas plataformas sin datos externos). La
completitud del anuncio (invariante 3: creatividad, copy, CTA, landing) la
protege `PlannedAd.__post_init__`/`AdCopyPlan` (T012) -- son cotas de forma
y presencia, no formato de cable especifico de Meta."""

from __future__ import annotations

from typing import cast

from safent_ads.packages.domain.errors import PlatformCompletenessError
from safent_ads.packages.domain.holes import creative_hole, image_local_ref
from safent_ads.packages.domain.planned_tree import (
    GoogleAdGroupNative,
    GoogleAssetGroupNative,
    GoogleCampaignNative,
    MetaAdSetNative,
    PlannedAd,
    PlannedAdSet,
    PlannedCampaign,
)
from safent_ads.packages.domain.values import GoogleAdCopyPlan, MetaAdCopyPlan
from safent_ads.proposals.domain.ad_child_creation import (
    AdChildCreationError,
    validate_child_payload,
)
from safent_ads.proposals.domain.campaign_creation import CampaignCreationError, creation_budget
from safent_ads.proposals.domain.money import Money
from safent_ads.shared.ids import EntityRef, PlatformCode


def validate_campaign_completeness(
    campaign: PlannedCampaign, account_ref: EntityRef | None = None
) -> Money:
    """Invariante 6 a nivel de campana: delega en `creation_budget`."""
    try:
        return creation_budget({"creation_plan": campaign_wire_plan(campaign)}, account_ref)
    except CampaignCreationError as exc:
        raise PlatformCompletenessError(str(exc)) from exc


def validate_ad_set_completeness(ad_set: PlannedAdSet, platform: PlatformCode) -> None:
    """Invariante 6 a nivel de conjunto/grupo: delega en
    `validate_child_payload`. No exige datos externos en ninguna
    plataforma."""
    try:
        validate_child_payload({"child_plan": ad_set_wire_plan(ad_set, platform)})
    except AdChildCreationError as exc:
        raise PlatformCompletenessError(str(exc)) from exc


def campaign_wire_plan(campaign: PlannedCampaign) -> dict[str, object]:
    platform_value = "google" if isinstance(campaign.native, GoogleCampaignNative) else "meta"
    return {
        "schema_version": 1,
        "platform": platform_value,
        "name": campaign.name,
        "status": "PAUSED",
        "daily_budget": campaign.daily_budget.to_canonical(),
        "native": campaign.native.to_canonical(),
    }


def ad_set_wire_plan(ad_set: PlannedAdSet, platform: PlatformCode) -> dict[str, object]:
    return {
        "schema_version": 1,
        "platform": platform.value,
        "kind": "ad_set",
        "status": "PAUSED",
        "native": _ad_set_native_wire(ad_set),
    }


def _ad_set_native_wire(ad_set: PlannedAdSet) -> dict[str, object]:
    if isinstance(ad_set.native, GoogleAdGroupNative):
        return _google_ad_group_wire(ad_set)
    if isinstance(ad_set.native, GoogleAssetGroupNative):
        return _google_asset_group_wire(ad_set)
    return _meta_ad_set_native_wire(ad_set)


def _google_ad_group_wire(ad_set: PlannedAdSet) -> dict[str, object]:
    if ad_set.keywords is None or ad_set.cpc_bid is None:
        raise PlatformCompletenessError("planned_ad_set_google_fields_missing")
    native = ad_set.native
    if not isinstance(native, GoogleAdGroupNative):  # pragma: no cover - narrows for mypy
        raise PlatformCompletenessError("planned_ad_set_native_mismatch")
    return {
        "name": ad_set.name,
        **native.to_canonical(),
        "cpc_bid": ad_set.cpc_bid.to_canonical(),
        "keywords": ad_set.keywords.to_canonical(),
    }


_ASSET_GROUP_IMAGE_FIELDS = ("logo", "marketing_image", "square_image")


def _google_asset_group_wire(ad_set: PlannedAdSet) -> dict[str, object]:
    """Sin `keywords`/`cpc_bid` (data-model.md: prohibidos en la fila de
    PERFORMANCE_MAX) -- el resto de la forma sale de `native.to_canonical()`,
    igual que ya delega en `creation_budget`/`validate_child_payload` el
    resto de este modulo (T023: ninguna regla de plataforma se reimplementa
    aqui), con dos huecos que `to_canonical()` no puede rellenar por si solo
    (data-model.md, gap cerrado en esta rama):

    1. `assets.name` -- `AssetGroup.name` que el dueño firma (`ad_set.name`,
       mismo origen que `_google_ad_group_wire`); vive DENTRO de `assets`,
       no como hermano de `kind`/`final_url`, para no tocar el conjunto
       exacto de claves que `proposals.domain.ad_child_creation.
       _validate_google_asset_group_native` exige a nivel de `native`
       (`{"kind", "final_url", "assets"}`) -- ese fichero no es de este
       carril.
    2. `assets.{logo,marketing_image,square_image}` -- cada imagen pasa de
       su objeto de valor completo (checksum/mime/dimensiones) al hueco
       simbolico `{creative_of:<local_ref>}` (R2.2), el MISMO local_ref que
       `approval_envelope._ad_set_depends_on` ya ata al `depends_on` de este
       mismo paso `CREATE_AD_SET`. Sin esto, `chokepoint_step_executor.
       substitute_holes` no encuentra nada que resolver y `native_ad_child.
       google_create` recibiria el objeto de imagen entero donde Google
       exige el `resource_name` de texto del recurso ya subido."""
    native = ad_set.native
    if not isinstance(native, GoogleAssetGroupNative):  # pragma: no cover - narrows for mypy
        raise PlatformCompletenessError("planned_ad_set_native_mismatch")
    canonical = native.to_canonical()
    assets = dict(cast("dict[str, object]", canonical["assets"]))
    assets["name"] = ad_set.name
    for field_name in _ASSET_GROUP_IMAGE_FIELDS:
        checksum = getattr(native.assets, field_name).checksum
        assets[field_name] = creative_hole(image_local_ref(checksum))
    return {**canonical, "assets": assets}


def _meta_ad_set_native_wire(ad_set: PlannedAdSet) -> dict[str, object]:
    native = ad_set.native
    if not isinstance(native, MetaAdSetNative):  # pragma: no cover - narrows for mypy
        raise PlatformCompletenessError("planned_ad_set_native_mismatch")
    return {"name": ad_set.name, **native.to_canonical()}


def ad_wire_plan(
    ad: PlannedAd, platform: PlatformCode, *, page_id: str | None, image_hash: str | None
) -> dict[str, object]:
    """La forma exacta que `proposals.domain.ad_child_creation.
    validate_child_payload` exige para `kind="ad"` (tasks.md T024, gap
    descubierto en esta rama: `_create_ad_template`, T102, no producia
    todavia esta forma -- solo `campaign_wire_plan`/`ad_set_wire_plan`
    tenian su contraparte de cable). `page_id`/`image_hash` no son I/O: los
    resuelve la capa de aplicacion antes de llamar (`publish_as.page_id`
    ya vive en el paquete; `image_hash` es el hueco `{creative_of:X}` sin
    resolver al firmar el sobre, y el manejador confirmado al ejecutar el
    paso -- nunca una URL, que R2.7/BL-6 retira de la superficie)."""
    if platform is PlatformCode.GOOGLE:
        return {
            "schema_version": 1,
            "platform": "google",
            "kind": "ad",
            "status": "PAUSED",
            "native": _google_ad_native_wire(ad),
        }
    return {
        "schema_version": 1,
        "platform": "meta",
        "kind": "ad",
        "status": "PAUSED",
        "native": _meta_ad_native_wire(ad, page_id=page_id, image_hash=image_hash),
    }


def _google_ad_native_wire(ad: PlannedAd) -> dict[str, object]:
    if not isinstance(ad.copy, GoogleAdCopyPlan):
        raise PlatformCompletenessError("planned_ad_platform_mismatch")
    return {
        "type": "RESPONSIVE_SEARCH_AD",
        "headlines": list(ad.copy.headlines),
        "descriptions": list(ad.copy.descriptions),
        "final_url": str(ad.landing),
    }


def _meta_ad_native_wire(
    ad: PlannedAd, *, page_id: str | None, image_hash: str | None
) -> dict[str, object]:
    if page_id is None or image_hash is None or not isinstance(ad.copy, MetaAdCopyPlan) or (
        ad.cta is None
    ):
        raise PlatformCompletenessError("planned_ad_meta_fields_missing")
    landing = str(ad.landing)
    return {
        "name": ad.name,
        "creative_inline": {
            "object_story_spec": {
                "page_id": page_id,
                "link_data": {
                    "link": landing,
                    "image_hash": image_hash,
                    "message": ad.copy.primary_text,
                    "name": ad.copy.headline,
                    "description": ad.copy.description,
                    "call_to_action": {"type": ad.cta.value, "value": {"link": landing}},
                },
            }
        },
    }

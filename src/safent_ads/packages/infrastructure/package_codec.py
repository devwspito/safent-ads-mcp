"""Codec de persistencia del arbol declarado (T027; migracion 0042/0044:
`campaign_packages.plan/budget/rationale/research/publish_as`).

No reutiliza `*.to_canonical()` a ciegas para la RECONSTRUCCION: esos
metodos son la proyeccion de HASH (`package_hash.py`), que a proposito deja
fuera `preview_key` en `ImageCreativeRef` (es una clave de enrutado del
servidor, no contenido firmado -- ver su docstring). El codec de
almacenamiento SI necesita `preview_key` para poder construir la vista
previa en lectura sin volver a consultar `creative`, asi que `encode_plan`
parte de `to_canonical()` y le inyecta ese unico campo adicional;
`decode_plan` hace el camino inverso completo, reconstruyendo cada objeto
de valor del arbol.

Reutiliza `to_jsonable`/`canonical_json_bytes` de `proposals.domain.
diff_hash` para el `Money`/`Decimal` que aparecen sueltos en `budget` --
mismo criterio que `proposals.infrastructure.value_codec`, sin
reimplementar la conversion."""

from __future__ import annotations

import json
from datetime import datetime
from decimal import Decimal
from typing import Any

from safent_ads.creative.domain.identifiers import AssetId
from safent_ads.packages.domain.errors import PackageDomainError
from safent_ads.packages.domain.planned_tree import (
    AdRef,
    AdSetRef,
    CampaignObjective,
    DeliverySchedule,
    EuPoliticalAdvertisingDeclaration,
    GoogleAdGroupNative,
    GoogleAdGroupType,
    GoogleAssetGroupNative,
    GoogleCampaignNative,
    GoogleDemandGenCampaignNative,
    GoogleDisplayCampaignNative,
    GoogleNetworkSettings,
    GooglePerformanceMaxCampaignNative,
    GoogleSearchCampaignNative,
    GoogleTargetingMode,
    MetaAdSetNative,
    MetaBidStrategy,
    MetaBillingEvent,
    MetaBudgetMode,
    MetaBuyingType,
    MetaCampaignNative,
    MetaDestinationType,
    MetaObjective,
    MetaOptimizationGoal,
    PlannedAd,
    PlannedAdSet,
    PlannedCampaign,
    SpecialAdCategory,
)
from safent_ads.packages.domain.values import (
    AdCopyPlan,
    AdCreativeRef,
    AssetGroupAssetPlan,
    Audience,
    CallToAction,
    GoogleAdCopyPlan,
    ImageCreativeRef,
    Keyword,
    KeywordPlan,
    LandingUrl,
    MatchType,
    MetaAdCopyPlan,
    MetaPublishAs,
    PackageBudget,
    PackageRationale,
    ResearchNote,
    ResearchNoteKind,
    ResearchSummary,
    TextOnlyCreativeRef,
)
from safent_ads.proposals.domain.conversion_goal import ConversionGoal
from safent_ads.proposals.domain.diff_hash import to_jsonable
from safent_ads.proposals.domain.google_bidding import (
    GoogleBidding,
    ManualCpc,
    MaximizeClicks,
    MaximizeConversions,
    MaximizeConversionValue,
)
from safent_ads.proposals.domain.google_channel_spec import (
    GoogleAdvertisingChannelType,
    GoogleBiddingStrategy,
)
from safent_ads.proposals.domain.money import Money

__all__ = [
    "PackageCodecError",
    "decode_budget",
    "decode_plan",
    "decode_publish_as",
    "decode_rationale",
    "decode_research",
    "dump_json",
    "encode_budget",
    "encode_plan",
    "encode_publish_as",
    "encode_rationale",
    "encode_research",
]


class PackageCodecError(PackageDomainError):
    """El JSON almacenado no respeta la forma que este codec espera --
    nunca deberia ocurrir sobre una fila escrita por este mismo modulo."""


_ASSET_GROUP_KIND = "ASSET_GROUP"


def dump_json(value: dict[str, Any] | list[Any]) -> str:
    return json.dumps(to_jsonable(value), separators=(",", ":"))


# ---------------------------------------------------------------------------
# plan (campaign + ad_sets)
# ---------------------------------------------------------------------------


def encode_plan(campaign: PlannedCampaign, ad_sets: tuple[PlannedAdSet, ...]) -> str:
    payload = {
        "campaign": campaign.to_canonical(),
        "ad_sets": [_encode_ad_set(ad_set) for ad_set in ad_sets],
    }
    return dump_json(payload)


def _encode_ad_set(ad_set: PlannedAdSet) -> dict[str, Any]:
    canonical = ad_set.to_canonical()
    canonical["ads"] = [_encode_ad(ad) for ad in ad_set.ads]
    if isinstance(ad_set.native, GoogleAssetGroupNative):
        canonical["native"] = _encode_asset_group_native(ad_set.native)
    return canonical


def _encode_ad(ad: PlannedAd) -> dict[str, Any]:
    canonical = ad.to_canonical()
    if isinstance(ad.creative, ImageCreativeRef):
        canonical["creative"]["preview_key"] = ad.creative.preview_key  # type: ignore[index]
    return canonical


def _encode_asset_group_native(native: GoogleAssetGroupNative) -> dict[str, Any]:
    """Igual que `_encode_ad`: `to_canonical()` deja fuera `preview_key`
    (es una clave de enrutado del servidor, no contenido firmado) y este
    codec de almacenamiento la reinyecta para poder pintar la vista previa
    en lectura sin volver a consultar `creative`."""
    canonical = native.to_canonical()
    assets = canonical["assets"]
    for role in ("logo", "marketing_image", "square_image"):
        assets[role]["preview_key"] = getattr(native.assets, role).preview_key  # type: ignore[index]
    return canonical


def decode_plan(raw: str) -> tuple[PlannedCampaign, tuple[PlannedAdSet, ...]]:
    data = json.loads(raw)
    campaign = _decode_campaign(_require_dict(data, "campaign"))
    ad_sets = tuple(_decode_ad_set(item) for item in _require_list(data, "ad_sets"))
    return campaign, ad_sets


def _decode_campaign(data: dict[str, Any]) -> PlannedCampaign:
    native_data = _require_dict(data, "native")
    return PlannedCampaign(
        name=_require_str(data, "name"),
        objective=CampaignObjective(_require_str(data, "objective")),
        daily_budget=_decode_money(_require_dict(data, "daily_budget")),
        duration_days=int(data["duration_days"]),
        native=_decode_campaign_native(native_data),
        success_criterion=_require_str(data, "success_criterion"),
        kill_criterion=_require_str(data, "kill_criterion"),
    )


def _decode_bidding(data: object) -> GoogleBidding:
    """`ManualCpc` viaja como cadena suelta (misma proyeccion que hoy);
    el resto, como objeto etiquetado por `kind` (T021 `_bidding_to_canonical`)."""
    if data == GoogleBiddingStrategy.MANUAL_CPC.value:
        return ManualCpc()
    if not isinstance(data, dict):
        raise PackageCodecError(f"puja irreconocible: {data!r}")
    kind = GoogleBiddingStrategy(data["kind"])
    if kind is GoogleBiddingStrategy.MANUAL_CPC:
        return ManualCpc()
    if kind is GoogleBiddingStrategy.MAXIMIZE_CLICKS:
        ceiling = data.get("cpc_bid_ceiling")
        return MaximizeClicks(cpc_bid_ceiling=_decode_money(ceiling) if ceiling else None)
    if kind is GoogleBiddingStrategy.MAXIMIZE_CONVERSIONS:
        target_cpa = data.get("target_cpa")
        return MaximizeConversions(target_cpa=_decode_money(target_cpa) if target_cpa else None)
    target_roas = data.get("target_roas")
    return MaximizeConversionValue(
        target_roas=Decimal(str(target_roas)) if target_roas is not None else None
    )


def _decode_conversion_goals(data: object) -> tuple[ConversionGoal, ...]:
    if not isinstance(data, list):
        return ()
    return tuple(ConversionGoal(resource_name=item["resource_name"]) for item in data)


def _decode_geographic_targeting(data: dict[str, Any]) -> dict[str, object] | None:
    geo = data.get("geographic_targeting")
    return dict(geo) if isinstance(geo, dict) else None


def _decode_campaign_native(data: dict[str, Any]) -> GoogleCampaignNative | MetaCampaignNative:
    raw_channel = data.get("advertising_channel_type")
    channel = GoogleAdvertisingChannelType(raw_channel) if isinstance(raw_channel, str) else None
    if channel is GoogleAdvertisingChannelType.SEARCH:
        networks = _require_dict(data, "network_settings")
        return GoogleSearchCampaignNative(
            bidding_strategy=_decode_bidding(data["bidding_strategy"]),
            contains_eu_political_advertising=EuPoliticalAdvertisingDeclaration(
                data["contains_eu_political_advertising"]
            ),
            network_settings=GoogleNetworkSettings(
                target_google_search=bool(networks["target_google_search"]),
                target_search_network=bool(networks["target_search_network"]),
                target_content_network=bool(networks["target_content_network"]),
                target_partner_search_network=bool(networks["target_partner_search_network"]),
            ),
            geographic_targeting=_decode_geographic_targeting(data),
            conversion_goals=_decode_conversion_goals(data.get("conversion_goals")),
        )
    if channel is GoogleAdvertisingChannelType.DISPLAY:
        return GoogleDisplayCampaignNative(
            bidding_strategy=_decode_bidding(data["bidding_strategy"]),
            contains_eu_political_advertising=EuPoliticalAdvertisingDeclaration(
                data["contains_eu_political_advertising"]
            ),
            geographic_targeting=_decode_geographic_targeting(data),
            conversion_goals=_decode_conversion_goals(data.get("conversion_goals")),
        )
    if channel is GoogleAdvertisingChannelType.DEMAND_GEN:
        return GoogleDemandGenCampaignNative(
            bidding_strategy=_decode_bidding(data["bidding_strategy"]),
            contains_eu_political_advertising=EuPoliticalAdvertisingDeclaration(
                data["contains_eu_political_advertising"]
            ),
            conversion_goals=_decode_conversion_goals(data.get("conversion_goals")),
            geographic_targeting=_decode_geographic_targeting(data),
        )
    if channel is GoogleAdvertisingChannelType.PERFORMANCE_MAX:
        return GooglePerformanceMaxCampaignNative(
            bidding_strategy=_decode_bidding(data["bidding_strategy"]),
            contains_eu_political_advertising=EuPoliticalAdvertisingDeclaration(
                data["contains_eu_political_advertising"]
            ),
            conversion_goals=_decode_conversion_goals(data.get("conversion_goals")),
            geographic_targeting=_decode_geographic_targeting(data),
        )
    if "objective" in data and "buying_type" in data:
        return MetaCampaignNative(
            objective=MetaObjective(data["objective"]),
            buying_type=MetaBuyingType(data["buying_type"]),
            bid_strategy=MetaBidStrategy(data["bid_strategy"]),
            special_ad_categories=tuple(
                SpecialAdCategory(value) for value in data.get("special_ad_categories", [])
            ),
            special_ad_category_country=tuple(data.get("special_ad_category_country", [])),
        )
    raise PackageCodecError(f"native de campaña irreconocible: {data!r}")


def _decode_ad_set(data: dict[str, Any]) -> PlannedAdSet:
    native_data = _require_dict(data, "native")
    native = _decode_ad_set_native(native_data)
    schedule_data = data.get("schedule")
    keywords_data = data.get("keywords")
    cpc_bid_data = data.get("cpc_bid")
    return PlannedAdSet(
        local_ref=AdSetRef(_require_str(data, "local_ref")),
        name=_require_str(data, "name"),
        audience=_decode_audience(_require_dict(data, "audience")),
        native=native,
        ads=tuple(_decode_ad(item) for item in _require_list(data, "ads")),
        schedule=_decode_schedule(schedule_data) if schedule_data else None,
        keywords=_decode_keyword_plan(keywords_data) if keywords_data else None,
        cpc_bid=_decode_money(cpc_bid_data) if cpc_bid_data else None,
    )


def _decode_ad_set_native(
    data: dict[str, Any],
) -> GoogleAdGroupNative | GoogleAssetGroupNative | MetaAdSetNative:
    if data.get("kind") == _ASSET_GROUP_KIND:
        return GoogleAssetGroupNative(
            final_url=LandingUrl(_require_str(data, "final_url")),
            assets=_decode_asset_group_assets(_require_dict(data, "assets")),
        )
    if "type" in data:
        return GoogleAdGroupNative(
            type=GoogleAdGroupType(data["type"]),
            bidding_strategy=GoogleBiddingStrategy(data["bidding_strategy"]),
            targeting_mode=GoogleTargetingMode(data["targeting_mode"]),
        )
    if "budget_mode" in data:
        targeting = _require_dict(data, "targeting")
        countries = tuple(targeting["geo_locations"]["countries"])
        return MetaAdSetNative(
            budget_mode=MetaBudgetMode(data["budget_mode"]),
            billing_event=MetaBillingEvent(data["billing_event"]),
            optimization_goal=MetaOptimizationGoal(data["optimization_goal"]),
            destination_type=MetaDestinationType(data["destination_type"]),
            countries=countries,
            age_min=int(targeting["age_min"]),
            age_max=int(targeting["age_max"]),
            dsa_beneficiary=_require_str(data, "dsa_beneficiary"),
            dsa_payor=_require_str(data, "dsa_payor"),
        )
    raise PackageCodecError(f"native de conjunto irreconocible: {data!r}")


def _decode_audience(data: dict[str, Any]) -> Audience:
    return Audience(plain=_require_str(data, "plain"), native=dict(_require_dict(data, "native")))


def _decode_schedule(data: dict[str, Any]) -> DeliverySchedule:
    return DeliverySchedule(
        weekdays=tuple(int(day) for day in data["weekdays"]),
        start_time=_require_str(data, "start_time"),
        end_time=_require_str(data, "end_time"),
    )


def _decode_keyword_plan(data: list[dict[str, Any]]) -> KeywordPlan:
    keywords = tuple(
        Keyword(text=item["text"], match_type=MatchType(item["match_type"])) for item in data
    )
    return KeywordPlan(keywords)


def _decode_ad(data: dict[str, Any]) -> PlannedAd:
    cta = data.get("cta")
    return PlannedAd(
        local_ref=AdRef(_require_str(data, "local_ref")),
        name=_require_str(data, "name"),
        creative=_decode_creative(_require_dict(data, "creative")),
        copy=_decode_copy(_require_dict(data, "copy")),
        landing=LandingUrl(_require_str(data, "landing")),
        cta=CallToAction(cta) if cta is not None else None,
    )


def _decode_creative(data: dict[str, Any]) -> AdCreativeRef:
    if data.get("kind") == "text_only":
        return TextOnlyCreativeRef()
    return _decode_image_creative(data)


def _decode_image_creative(data: dict[str, Any]) -> ImageCreativeRef:
    return ImageCreativeRef(
        asset_id=AssetId.parse(_require_str(data, "asset_id")),
        checksum=_require_str(data, "checksum"),
        preview_key=_require_str(data, "preview_key"),
        mime_type=_require_str(data, "mime_type"),
        width=int(data["width"]),
        height=int(data["height"]),
    )


def _decode_asset_group_assets(data: dict[str, Any]) -> AssetGroupAssetPlan:
    return AssetGroupAssetPlan(
        headlines=tuple(data["headlines"]),
        long_headlines=tuple(data["long_headlines"]),
        descriptions=tuple(data["descriptions"]),
        business_name=_require_str(data, "business_name"),
        logo=_decode_image_creative(_require_dict(data, "logo")),
        marketing_image=_decode_image_creative(_require_dict(data, "marketing_image")),
        square_image=_decode_image_creative(_require_dict(data, "square_image")),
    )


def _decode_copy(data: dict[str, Any]) -> AdCopyPlan:
    if "headlines" in data:
        return GoogleAdCopyPlan(
            headlines=tuple(data["headlines"]), descriptions=tuple(data["descriptions"])
        )
    return MetaAdCopyPlan(
        primary_text=_require_str(data, "primary_text"),
        headline=_require_str(data, "headline"),
        description=_require_str(data, "description"),
    )


def _decode_money(data: dict[str, Any]) -> Money:
    return Money(amount=Decimal(str(data["amount"])), currency=str(data["currency"]))


# ---------------------------------------------------------------------------
# budget / rationale / research / publish_as
# ---------------------------------------------------------------------------


def encode_budget(budget: PackageBudget) -> str:
    return dump_json(budget.to_canonical())


def decode_budget(raw: str) -> PackageBudget:
    data = json.loads(raw)
    headroom = data.get("envelope_headroom")
    return PackageBudget(
        daily=_decode_money(data["daily"]),
        monthly_equivalent=_decode_money(data["monthly_equivalent"]),
        total_cap=_decode_money(data["total_cap"]),
        envelope_headroom=_decode_money(headroom) if headroom else None,
        envelope_reason=data.get("envelope_reason"),
    )


def encode_rationale(rationale: PackageRationale) -> str:
    return dump_json(rationale.to_canonical())


def decode_rationale(raw: str) -> PackageRationale:
    data = json.loads(raw)
    return PackageRationale(owner_request=data["owner_request"], why=data["why"])


def encode_research(research: ResearchSummary | None) -> str | None:
    return dump_json(research.to_canonical()) if research is not None else None


def decode_research(raw: str | None) -> ResearchSummary | None:
    if raw is None:
        return None
    data = json.loads(raw)
    return ResearchSummary(
        internal=tuple(_decode_research_note(item) for item in data.get("internal", [])),
        external=tuple(_decode_research_note(item) for item in data.get("external", [])),
    )


def _decode_research_note(data: dict[str, Any]) -> ResearchNote:
    observed_at = data["observed_at"]
    return ResearchNote(
        kind=ResearchNoteKind(data["kind"]),
        summary=data["summary"],
        url=data.get("url"),
        observed_at=_decode_datetime(observed_at),
    )


def _decode_datetime(value: str) -> datetime:
    return datetime.fromisoformat(value)


def encode_publish_as(publish_as: MetaPublishAs | None) -> str | None:
    return dump_json(publish_as.to_canonical()) if publish_as is not None else None


def decode_publish_as(raw: str | None) -> MetaPublishAs | None:
    if raw is None:
        return None
    data = json.loads(raw)
    return MetaPublishAs(page_id=data["page_id"], page_name=data["page_name"])


def _require_dict(data: dict[str, Any], key: str) -> dict[str, Any]:
    value = data.get(key)
    if not isinstance(value, dict):
        raise PackageCodecError(f"se esperaba un objeto en {key!r}: {data!r}")
    return value


def _require_list(data: dict[str, Any], key: str) -> list[Any]:
    value = data.get(key)
    if not isinstance(value, list):
        raise PackageCodecError(f"se esperaba una lista en {key!r}: {data!r}")
    return value


def _require_str(data: dict[str, Any], key: str) -> str:
    value = data.get(key)
    if not isinstance(value, str):
        raise PackageCodecError(f"se esperaba texto en {key!r}: {data!r}")
    return value

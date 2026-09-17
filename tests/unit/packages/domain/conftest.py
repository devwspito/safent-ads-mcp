"""Factories compartidas para los tests de `packages.domain`."""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta

from safent_ads.creative.domain.identifiers import AssetId
from safent_ads.packages.domain.campaign_package import CampaignPackage
from safent_ads.packages.domain.identifiers import OfferingId, PackageId
from safent_ads.packages.domain.planned_tree import (
    AdRef,
    AdSetRef,
    CampaignObjective,
    EuPoliticalAdvertisingDeclaration,
    GoogleAdGroupNative,
    GoogleAdGroupType,
    GoogleAssetGroupNative,
    GoogleBiddingStrategy,
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
)
from safent_ads.packages.domain.values import (
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
    TextOnlyCreativeRef,
)
from safent_ads.proposals.domain.conversion_goal import ConversionGoal
from safent_ads.proposals.domain.google_bidding import ManualCpc, MaximizeConversions
from safent_ads.proposals.domain.money import Money
from safent_ads.shared.ids import BusinessId, EntityLevel, EntityRef, PlatformCode

NOW = datetime(2026, 9, 14, 10, 0, tzinfo=UTC)


def business_id() -> BusinessId:
    return BusinessId.new()


def account_ref(
    business: BusinessId,
    *,
    platform: PlatformCode = PlatformCode.META,
    connection_id: uuid.UUID | None = None,
    external_id: str = "act_123",
) -> EntityRef:
    return EntityRef(
        platform, EntityLevel.ACCOUNT, external_id, business.value, connection_id or uuid.uuid4()
    )


def meta_campaign_native(**overrides: object) -> MetaCampaignNative:
    defaults: dict[str, object] = {
        "objective": MetaObjective.OUTCOME_LEADS,
        "buying_type": MetaBuyingType.AUCTION,
        "bid_strategy": MetaBidStrategy.LOWEST_COST_WITHOUT_CAP,
        "special_ad_categories": (),
        "special_ad_category_country": (),
    }
    defaults.update(overrides)
    return MetaCampaignNative(**defaults)  # type: ignore[arg-type]


def google_campaign_native(**overrides: object) -> GoogleSearchCampaignNative:
    defaults: dict[str, object] = {
        "bidding_strategy": ManualCpc(),
        "contains_eu_political_advertising": (
            EuPoliticalAdvertisingDeclaration.DOES_NOT_CONTAIN
        ),
        "network_settings": GoogleNetworkSettings(True, True, False, False),
    }
    defaults.update(overrides)
    return GoogleSearchCampaignNative(**defaults)  # type: ignore[arg-type]


def performance_max_campaign_native(**overrides: object) -> GooglePerformanceMaxCampaignNative:
    defaults: dict[str, object] = {
        "bidding_strategy": MaximizeConversions(),
        "contains_eu_political_advertising": (
            EuPoliticalAdvertisingDeclaration.DOES_NOT_CONTAIN
        ),
        "conversion_goals": (
            ConversionGoal("customers/1112223333/conversionActions/4445556666"),
        ),
    }
    defaults.update(overrides)
    return GooglePerformanceMaxCampaignNative(**defaults)  # type: ignore[arg-type]


def meta_campaign(**overrides: object) -> PlannedCampaign:
    defaults: dict[str, object] = {
        "name": "Reserva de citas",
        "objective": CampaignObjective.RESERVATIONS,
        "daily_budget": Money.of("20.00"),
        "duration_days": 14,
        "native": meta_campaign_native(),
        "success_criterion": "CPL bajo 15 EUR en 7 dias",
        "kill_criterion": "CPL sobre 40 EUR durante 3 dias seguidos",
    }
    defaults.update(overrides)
    return PlannedCampaign(**defaults)  # type: ignore[arg-type]


def google_campaign(**overrides: object) -> PlannedCampaign:
    defaults: dict[str, object] = {
        "name": "Reserva de citas Google",
        "objective": CampaignObjective.RESERVATIONS,
        "daily_budget": Money.of("20.00"),
        "duration_days": 14,
        "native": google_campaign_native(),
        "success_criterion": "CPL bajo 15 EUR en 7 dias",
        "kill_criterion": "CPL sobre 40 EUR durante 3 dias seguidos",
    }
    defaults.update(overrides)
    return PlannedCampaign(**defaults)  # type: ignore[arg-type]


def audience(**overrides: object) -> Audience:
    defaults: dict[str, object] = {
        "plain": "Mujeres y hombres de 25 a 65 años, a 15 km de Valencia",
        "native": {"age_min": 25, "age_max": 65, "geo": "valencia"},
    }
    defaults.update(overrides)
    return Audience(**defaults)  # type: ignore[arg-type]


def image_creative(checksum: str = "a" * 64, **overrides: object) -> ImageCreativeRef:
    defaults: dict[str, object] = {
        "asset_id": AssetId.new(),
        "checksum": checksum,
        "preview_key": "preview-key-1",
        "mime_type": "image/png",
        "width": 1200,
        "height": 628,
    }
    defaults.update(overrides)
    return ImageCreativeRef(**defaults)  # type: ignore[arg-type]


def meta_ad(
    local_ref: str = "as#1/ad#1", checksum: str = "a" * 64, **overrides: object
) -> PlannedAd:
    defaults: dict[str, object] = {
        "local_ref": AdRef(local_ref),
        "name": "Anuncio 1",
        "creative": image_creative(checksum=checksum),
        "copy": MetaAdCopyPlan(
            primary_text="Reserva tu cita hoy mismo, sin esperas.",
            headline="Reserva ya",
            description="Citas veterinarias fuera de horario",
        ),
        "landing": LandingUrl("https://clinicax.example/reservar"),
        "cta": CallToAction.LEARN_MORE,
    }
    defaults.update(overrides)
    return PlannedAd(**defaults)  # type: ignore[arg-type]


def google_ad(local_ref: str = "as#1/ad#1", **overrides: object) -> PlannedAd:
    defaults: dict[str, object] = {
        "local_ref": AdRef(local_ref),
        "name": "Anuncio 1",
        "creative": TextOnlyCreativeRef(),
        "copy": GoogleAdCopyPlan(
            headlines=("Reserva ya", "Cita veterinaria", "Atencion 24h"),
            descriptions=("Reserva tu cita en minutos", "Atencion profesional cercana"),
        ),
        "landing": LandingUrl("https://clinicax.example/reservar"),
        "cta": None,
    }
    defaults.update(overrides)
    return PlannedAd(**defaults)  # type: ignore[arg-type]


def meta_ad_set(
    local_ref: str = "as#1", ads: tuple[PlannedAd, ...] | None = None, **overrides: object
) -> PlannedAdSet:
    defaults: dict[str, object] = {
        "local_ref": AdSetRef(local_ref),
        "name": "Conjunto 1",
        "audience": audience(),
        "native": MetaAdSetNative(
            budget_mode=MetaBudgetMode.CAMPAIGN,
            billing_event=MetaBillingEvent.IMPRESSIONS,
            optimization_goal=MetaOptimizationGoal.LINK_CLICKS,
            destination_type=MetaDestinationType.WEBSITE,
            countries=("ES",),
            age_min=25,
            age_max=65,
            dsa_beneficiary="Clinica X",
            dsa_payor="Clinica X",
        ),
        "ads": ads if ads is not None else (meta_ad(local_ref=f"{local_ref}/ad#1"),),
    }
    defaults.update(overrides)
    return PlannedAdSet(**defaults)  # type: ignore[arg-type]


def google_ad_set(
    local_ref: str = "as#1", ads: tuple[PlannedAd, ...] | None = None, **overrides: object
) -> PlannedAdSet:
    defaults: dict[str, object] = {
        "local_ref": AdSetRef(local_ref),
        "name": "Grupo 1",
        "audience": audience(),
        "native": GoogleAdGroupNative(
            type=GoogleAdGroupType.SEARCH_STANDARD,
            bidding_strategy=GoogleBiddingStrategy.MANUAL_CPC,
            targeting_mode=GoogleTargetingMode.INHERIT_CAMPAIGN,
        ),
        "ads": ads if ads is not None else (google_ad(local_ref=f"{local_ref}/ad#1"),),
        "keywords": KeywordPlan((Keyword("reserva cita veterinario", MatchType.PHRASE),)),
        "cpc_bid": Money.of("0.80"),
    }
    defaults.update(overrides)
    return PlannedAdSet(**defaults)  # type: ignore[arg-type]


def asset_group_assets(**overrides: object) -> AssetGroupAssetPlan:
    defaults: dict[str, object] = {
        "headlines": ("Reserva ya", "Cita veterinaria", "Atencion 24h"),
        "long_headlines": ("Reserva tu cita veterinaria en minutos",),
        "descriptions": ("Reserva tu cita en minutos", "Atencion profesional cercana"),
        "business_name": "Clinica X",
        "logo": image_creative(checksum="b" * 64, width=1080, height=1080),
        "marketing_image": image_creative(checksum="c" * 64, width=1200, height=628),
        "square_image": image_creative(checksum="d" * 64, width=1080, height=1080),
    }
    defaults.update(overrides)
    return AssetGroupAssetPlan(**defaults)  # type: ignore[arg-type]


def performance_max_ad_set(
    local_ref: str = "as#1", ads: tuple[PlannedAd, ...] = (), **overrides: object
) -> PlannedAdSet:
    defaults: dict[str, object] = {
        "local_ref": AdSetRef(local_ref),
        "name": "Grupo de recursos 1",
        "audience": audience(),
        "native": GoogleAssetGroupNative(
            final_url=LandingUrl("https://clinicax.example/reservar"),
            assets=asset_group_assets(),
        ),
        "ads": ads,
    }
    defaults.update(overrides)
    return PlannedAdSet(**defaults)  # type: ignore[arg-type]


def package_budget(daily: Money | None = None, duration_days: int = 14) -> PackageBudget:
    return PackageBudget.derive(daily=daily or Money.of("20.00"), duration_days=duration_days)


def package_rationale(**overrides: object) -> PackageRationale:
    defaults: dict[str, object] = {
        "owner_request": "Hazme una campaña de reserva de citas veterinarias",
        "why": "Tus clientes buscan cita fuera de horario y no hay campaña que los recoja",
    }
    defaults.update(overrides)
    return PackageRationale(**defaults)  # type: ignore[arg-type]


def meta_publish_as(**overrides: object) -> MetaPublishAs:
    defaults: dict[str, object] = {"page_id": "1234567890", "page_name": "Clinica X"}
    defaults.update(overrides)
    return MetaPublishAs(**defaults)  # type: ignore[arg-type]


_UNSET: object = object()


def propose_meta_package(
    *,
    business: BusinessId | None = None,
    account: EntityRef | None = None,
    publish_as: MetaPublishAs | None | object = _UNSET,
    campaign: PlannedCampaign | None = None,
    ad_sets: tuple[PlannedAdSet, ...] | None = None,
    offering_id: OfferingId | None = None,
    now: datetime = NOW,
    ttl_hours: int = 72,
) -> CampaignPackage:
    resolved_business = business or business_id()
    resolved_account = account or account_ref(resolved_business, platform=PlatformCode.META)
    resolved_publish_as = meta_publish_as() if publish_as is _UNSET else publish_as
    return CampaignPackage.propose(
        package_id=PackageId.new(),
        business_id=resolved_business,
        account_ref=resolved_account,
        publish_as=resolved_publish_as,  # type: ignore[arg-type]
        offering_id=offering_id or OfferingId("offering-1"),
        campaign=campaign or meta_campaign(),
        ad_sets=(meta_ad_set(),) if ad_sets is None else ad_sets,
        budget=package_budget(),
        rationale=package_rationale(),
        research=None,
        now=now,
        expires_at=now + timedelta(hours=ttl_hours),
    )


def propose_google_package(
    *,
    business: BusinessId | None = None,
    account: EntityRef | None = None,
    campaign: PlannedCampaign | None = None,
    ad_sets: tuple[PlannedAdSet, ...] | None = None,
    offering_id: OfferingId | None = None,
    now: datetime = NOW,
    ttl_hours: int = 72,
) -> CampaignPackage:
    resolved_business = business or business_id()
    resolved_account = account or account_ref(resolved_business, platform=PlatformCode.GOOGLE)
    return CampaignPackage.propose(
        package_id=PackageId.new(),
        business_id=resolved_business,
        account_ref=resolved_account,
        publish_as=None,
        offering_id=offering_id or OfferingId("offering-1"),
        campaign=campaign or google_campaign(),
        ad_sets=(google_ad_set(),) if ad_sets is None else ad_sets,
        budget=package_budget(),
        rationale=package_rationale(),
        research=None,
        now=now,
        expires_at=now + timedelta(hours=ttl_hours),
    )

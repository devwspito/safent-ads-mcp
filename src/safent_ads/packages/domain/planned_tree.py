"""El arbol declarado: `PlannedCampaign` -> `PlannedAdSet` -> `PlannedAd`
(data-model.md). V1 solo soporta Google SEARCH/CPC manual y Meta
auction/CBO en pausa -- igual que `proposals.domain.campaign_creation`, del
que este modulo es la contraparte de dominio puro para `packages` (T012).

`AdSetRef`/`AdRef` (threat-model.md BL-2) son ordinales estables y
direccionables: la posicion de cada nodo dentro de las tuplas del arbol
coincide siempre con su ordinal declarado (`_require_positional_ad_refs`
aqui; `_require_positional_ad_set_refs` en `campaign_package.py`), asi que
una futura proyeccion de pasos puede derivar `step_index` y el `local_ref`
del padre (`parent_local_ref`) sin ambiguedad ni resolucion en tiempo de
ejecucion."""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Final

from safent_ads.packages.domain.errors import PlannedTreeError
from safent_ads.packages.domain.values import (
    AdCopyPlan,
    AdCreativeRef,
    AssetGroupAssetPlan,
    Audience,
    CallToAction,
    GoogleAdCopyPlan,
    ImageCreativeRef,
    KeywordPlan,
    LandingUrl,
    MetaAdCopyPlan,
)
from safent_ads.proposals.domain.conversion_goal import ConversionGoal
from safent_ads.proposals.domain.google_bidding import (
    GoogleBidding,
    ManualCpc,
    MaximizeClicks,
    MaximizeConversions,
    MaximizeConversionValue,
)
from safent_ads.proposals.domain.google_channel_spec import (
    CHANNEL_SPECS,
    FieldRule,
    GoogleAdvertisingChannelType,
    GoogleBiddingStrategy,
    GoogleChannelSpec,
    spec_for_child_type,
)
from safent_ads.proposals.domain.google_search_targeting import valid_geography
from safent_ads.proposals.domain.money import Money
from safent_ads.shared.ids import PlatformCode

_ASSET_GROUP_KIND: Final[str] = "ASSET_GROUP"
_MAX_CONVERSION_GOALS = 10

_CONTROL_CHARACTER_BOUNDARY = 32
_MAX_PLANNED_NAME_LENGTH = 128
_COUNTRY_CODE_LENGTH = 2


def _require_text(value: str, maximum: int, *, field: str) -> None:
    if (
        not value.strip()
        or len(value) > maximum
        or any(ord(char) < _CONTROL_CHARACTER_BOUNDARY for char in value)
    ):
        raise PlannedTreeError(f"{field}_invalid")


# ---------------------------------------------------------------------------
# Campaign-level native choices
# ---------------------------------------------------------------------------


class CampaignObjective(StrEnum):
    """El objetivo en llano que declara el dueño/agente; el servidor lo
    mapea al enum nativo (`mcp-tools.md §2`)."""

    RESERVATIONS = "reservas"
    LEADS = "leads"
    SALES = "ventas"
    TRAFFIC = "trafico"
    CALLS = "llamadas"
    AWARENESS = "reconocimiento"


class EuPoliticalAdvertisingDeclaration(StrEnum):
    CONTAINS = "CONTAINS_EU_POLITICAL_ADVERTISING"
    DOES_NOT_CONTAIN = "DOES_NOT_CONTAIN_EU_POLITICAL_ADVERTISING"


@dataclass(frozen=True, slots=True)
class GoogleNetworkSettings:
    target_google_search: bool
    target_search_network: bool
    target_content_network: bool
    target_partner_search_network: bool

    def to_canonical(self) -> dict[str, object]:
        return {
            "target_google_search": self.target_google_search,
            "target_search_network": self.target_search_network,
            "target_content_network": self.target_content_network,
            "target_partner_search_network": self.target_partner_search_network,
        }


def _bidding_kind(bidding: GoogleBidding) -> GoogleBiddingStrategy:
    if isinstance(bidding, ManualCpc):
        return GoogleBiddingStrategy.MANUAL_CPC
    if isinstance(bidding, MaximizeClicks):
        return GoogleBiddingStrategy.MAXIMIZE_CLICKS
    if isinstance(bidding, MaximizeConversions):
        return GoogleBiddingStrategy.MAXIMIZE_CONVERSIONS
    return GoogleBiddingStrategy.MAXIMIZE_CONVERSION_VALUE


def _bidding_to_canonical(bidding: GoogleBidding) -> object:
    """`ManualCpc` -- sin campos -- se proyecta como la cadena suelta de
    siempre (byte a byte igual que hoy para Busqueda); las pujas con
    objetivo opcional se etiquetan (`kind`) porque, a diferencia de
    `ManualCpc`, dos instancias de la misma clase pueden no ser
    equivalentes (`target_cpa` presente o ausente)."""
    kind = _bidding_kind(bidding)
    if isinstance(bidding, ManualCpc):
        return kind.value
    payload: dict[str, object] = {"kind": kind.value}
    if isinstance(bidding, MaximizeClicks) and bidding.cpc_bid_ceiling is not None:
        payload["cpc_bid_ceiling"] = bidding.cpc_bid_ceiling
    elif isinstance(bidding, MaximizeConversions) and bidding.target_cpa is not None:
        payload["target_cpa"] = bidding.target_cpa
    elif isinstance(bidding, MaximizeConversionValue) and bidding.target_roas is not None:
        payload["target_roas"] = bidding.target_roas
    return payload


def _conversion_goals_to_canonical(goals: tuple[ConversionGoal, ...]) -> list[dict[str, object]]:
    return [{"resource_name": goal.resource_name} for goal in goals]


def _require_google_campaign_native(
    *,
    spec: GoogleChannelSpec,
    bidding: GoogleBidding,
    conversion_goals: tuple[ConversionGoal, ...],
    geographic_targeting: dict[str, object] | None,
) -> None:
    """Invariantes comunes a las cuatro variantes (data-model.md
    "GoogleCampaignNative pasa a ser una union de cuatro variantes"):
    puja admitida por la fila, metas de conversion cuando la fila o la
    puja las exige, geografia valida si esta presente."""
    if _bidding_kind(bidding) not in spec.allowed_bidding:
        raise PlannedTreeError("google_campaign_bidding_not_allowed")
    if len(conversion_goals) > _MAX_CONVERSION_GOALS:
        raise PlannedTreeError("google_campaign_conversion_goals_too_many")
    if (spec.requires_conversion_goals or bidding.is_conversion_based) and not conversion_goals:
        raise PlannedTreeError("google_campaign_conversion_goals_required")
    if geographic_targeting is not None and not valid_geography(geographic_targeting):
        raise PlannedTreeError("google_campaign_geography_invalid")


@dataclass(frozen=True, slots=True)
class GoogleSearchCampaignNative:
    """La de hoy, SIN CAMBIOS de forma canonica (data-model.md): la puja
    pasa de un enum de un solo valor a `GoogleBidding` (ampliado), pero
    `ManualCpc` sigue proyectando la misma cadena suelta de siempre."""

    bidding_strategy: GoogleBidding
    contains_eu_political_advertising: EuPoliticalAdvertisingDeclaration
    network_settings: GoogleNetworkSettings
    geographic_targeting: dict[str, object] | None = None
    conversion_goals: tuple[ConversionGoal, ...] = ()
    advertising_channel_type: GoogleAdvertisingChannelType = field(
        default=GoogleAdvertisingChannelType.SEARCH, init=False
    )

    def __post_init__(self) -> None:
        _require_google_campaign_native(
            spec=CHANNEL_SPECS[self.advertising_channel_type],
            bidding=self.bidding_strategy,
            conversion_goals=self.conversion_goals,
            geographic_targeting=self.geographic_targeting,
        )

    def to_canonical(self) -> dict[str, object]:
        payload: dict[str, object] = {
            "advertising_channel_type": self.advertising_channel_type.value,
            "bidding_strategy": _bidding_to_canonical(self.bidding_strategy),
            "contains_eu_political_advertising": self.contains_eu_political_advertising.value,
            "network_settings": self.network_settings.to_canonical(),
        }
        if self.geographic_targeting is not None:
            payload["geographic_targeting"] = dict(self.geographic_targeting)
        if self.conversion_goals:
            payload["conversion_goals"] = _conversion_goals_to_canonical(self.conversion_goals)
        return payload


@dataclass(frozen=True, slots=True)
class GoogleDisplayCampaignNative:
    bidding_strategy: GoogleBidding
    contains_eu_political_advertising: EuPoliticalAdvertisingDeclaration
    geographic_targeting: dict[str, object] | None = None
    conversion_goals: tuple[ConversionGoal, ...] = ()
    advertising_channel_type: GoogleAdvertisingChannelType = field(
        default=GoogleAdvertisingChannelType.DISPLAY, init=False
    )

    def __post_init__(self) -> None:
        _require_google_campaign_native(
            spec=CHANNEL_SPECS[self.advertising_channel_type],
            bidding=self.bidding_strategy,
            conversion_goals=self.conversion_goals,
            geographic_targeting=self.geographic_targeting,
        )

    def to_canonical(self) -> dict[str, object]:
        payload: dict[str, object] = {
            "advertising_channel_type": self.advertising_channel_type.value,
            "bidding_strategy": _bidding_to_canonical(self.bidding_strategy),
            "contains_eu_political_advertising": self.contains_eu_political_advertising.value,
        }
        if self.geographic_targeting is not None:
            payload["geographic_targeting"] = dict(self.geographic_targeting)
        if self.conversion_goals:
            payload["conversion_goals"] = _conversion_goals_to_canonical(self.conversion_goals)
        return payload


def _forced_literal_campaign_native(
    *,
    channel_type: GoogleAdvertisingChannelType,
    bidding_strategy: GoogleBidding,
    contains_eu_political_advertising: EuPoliticalAdvertisingDeclaration,
    geographic_targeting: dict[str, object] | None,
    conversion_goals: tuple[ConversionGoal, ...],
) -> dict[str, object]:
    """Comun a Demand Gen y Maximo Rendimiento: metas de conversion
    obligatorias (BL-1) y los literales forzados de la fila (T021) entran
    en la proyeccion canonica -- de ahi que firmen el `package_hash`."""
    spec = CHANNEL_SPECS[channel_type]
    payload: dict[str, object] = {
        "advertising_channel_type": channel_type.value,
        "bidding_strategy": _bidding_to_canonical(bidding_strategy),
        "contains_eu_political_advertising": contains_eu_political_advertising.value,
        "conversion_goals": _conversion_goals_to_canonical(conversion_goals),
    }
    if geographic_targeting is not None:
        payload["geographic_targeting"] = dict(geographic_targeting)
    payload.update(spec.forced_literals)
    return payload


@dataclass(frozen=True, slots=True)
class GoogleDemandGenCampaignNative:
    bidding_strategy: GoogleBidding
    contains_eu_political_advertising: EuPoliticalAdvertisingDeclaration
    conversion_goals: tuple[ConversionGoal, ...]
    geographic_targeting: dict[str, object] | None = None
    advertising_channel_type: GoogleAdvertisingChannelType = field(
        default=GoogleAdvertisingChannelType.DEMAND_GEN, init=False
    )

    def __post_init__(self) -> None:
        _require_google_campaign_native(
            spec=CHANNEL_SPECS[self.advertising_channel_type],
            bidding=self.bidding_strategy,
            conversion_goals=self.conversion_goals,
            geographic_targeting=self.geographic_targeting,
        )

    def to_canonical(self) -> dict[str, object]:
        return _forced_literal_campaign_native(
            channel_type=self.advertising_channel_type,
            bidding_strategy=self.bidding_strategy,
            contains_eu_political_advertising=self.contains_eu_political_advertising,
            geographic_targeting=self.geographic_targeting,
            conversion_goals=self.conversion_goals,
        )


@dataclass(frozen=True, slots=True)
class GooglePerformanceMaxCampaignNative:
    bidding_strategy: GoogleBidding
    contains_eu_political_advertising: EuPoliticalAdvertisingDeclaration
    conversion_goals: tuple[ConversionGoal, ...]
    geographic_targeting: dict[str, object] | None = None
    advertising_channel_type: GoogleAdvertisingChannelType = field(
        default=GoogleAdvertisingChannelType.PERFORMANCE_MAX, init=False
    )

    def __post_init__(self) -> None:
        _require_google_campaign_native(
            spec=CHANNEL_SPECS[self.advertising_channel_type],
            bidding=self.bidding_strategy,
            conversion_goals=self.conversion_goals,
            geographic_targeting=self.geographic_targeting,
        )

    def to_canonical(self) -> dict[str, object]:
        return _forced_literal_campaign_native(
            channel_type=self.advertising_channel_type,
            bidding_strategy=self.bidding_strategy,
            contains_eu_political_advertising=self.contains_eu_political_advertising,
            geographic_targeting=self.geographic_targeting,
            conversion_goals=self.conversion_goals,
        )


GoogleCampaignNative = (
    GoogleSearchCampaignNative
    | GoogleDisplayCampaignNative
    | GoogleDemandGenCampaignNative
    | GooglePerformanceMaxCampaignNative
)


class MetaObjective(StrEnum):
    OUTCOME_AWARENESS = "OUTCOME_AWARENESS"
    OUTCOME_ENGAGEMENT = "OUTCOME_ENGAGEMENT"
    OUTCOME_LEADS = "OUTCOME_LEADS"
    OUTCOME_SALES = "OUTCOME_SALES"
    OUTCOME_TRAFFIC = "OUTCOME_TRAFFIC"
    OUTCOME_APP_PROMOTION = "OUTCOME_APP_PROMOTION"


class MetaBuyingType(StrEnum):
    AUCTION = "AUCTION"


class MetaBidStrategy(StrEnum):
    LOWEST_COST_WITHOUT_CAP = "LOWEST_COST_WITHOUT_CAP"


class SpecialAdCategory(StrEnum):
    CREDIT = "CREDIT"
    EMPLOYMENT = "EMPLOYMENT"
    HOUSING = "HOUSING"
    ISSUES_ELECTIONS_POLITICS = "ISSUES_ELECTIONS_POLITICS"
    FINANCIAL_PRODUCTS_SERVICES = "FINANCIAL_PRODUCTS_SERVICES"


def _require_valid_country_codes(countries: tuple[str, ...], *, field: str) -> None:
    if len(set(countries)) != len(countries):
        raise PlannedTreeError(f"{field}_duplicate")
    if any(
        len(code) != _COUNTRY_CODE_LENGTH
        or not code.isascii()
        or not code.isalpha()
        or not code.isupper()
        for code in countries
    ):
        raise PlannedTreeError(f"{field}_invalid")


@dataclass(frozen=True, slots=True)
class MetaCampaignNative:
    """Completitud nativa Meta (invariante 6): objetivo + subasta + tope
    minimo + categorias especiales **declaradas**, posiblemente vacias."""

    objective: MetaObjective
    buying_type: MetaBuyingType
    bid_strategy: MetaBidStrategy
    special_ad_categories: tuple[SpecialAdCategory, ...] = ()
    special_ad_category_country: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        if len(set(self.special_ad_categories)) != len(self.special_ad_categories):
            raise PlannedTreeError("meta_campaign_categories_duplicate")
        _require_valid_country_codes(
            self.special_ad_category_country, field="meta_campaign_category_country"
        )
        if self.special_ad_categories and not self.special_ad_category_country:
            raise PlannedTreeError("meta_campaign_category_countries_required")

    def to_canonical(self) -> dict[str, object]:
        return {
            "objective": self.objective.value,
            "buying_type": self.buying_type.value,
            "bid_strategy": self.bid_strategy.value,
            "special_ad_categories": [category.value for category in self.special_ad_categories],
            "special_ad_category_country": list(self.special_ad_category_country),
        }


CampaignNative = GoogleCampaignNative | MetaCampaignNative


# ---------------------------------------------------------------------------
# PlannedCampaign
# ---------------------------------------------------------------------------

_MIN_DURATION_DAYS = 7
_MAX_DURATION_DAYS = 90
_MAX_CRITERION_LENGTH = 280


def _require_channel_minimums(
    native: CampaignNative, daily_budget: Money, duration_days: int
) -> None:
    """T026 (contracts/mcp-tools.md §5): ningun canal de Google aprende de
    una campana que no puede pagar ni un dia de aprendizaje. Meta no tiene
    fila que consultar, se ignora."""
    if not isinstance(native, GoogleCampaignNative):
        return
    spec = CHANNEL_SPECS[native.advertising_channel_type]
    if daily_budget < spec.min_daily_budget:
        raise PlannedTreeError("planned_campaign_daily_budget_below_channel_minimum")
    if duration_days < spec.min_duration_days:
        raise PlannedTreeError("planned_campaign_duration_below_channel_minimum")
    target_cpa = getattr(native.bidding_strategy, "target_cpa", None)
    if target_cpa is not None and daily_budget < target_cpa:
        raise PlannedTreeError("planned_campaign_daily_budget_below_channel_minimum")


@dataclass(frozen=True, slots=True)
class PlannedCampaign:
    """El contenedor: nombre, objetivo, presupuesto diario y elecciones
    nativas obligatorias. `native.platform == paquete.platform` lo exige
    `CampaignPackage.propose` (no aqui: esta clase no conoce la cuenta)."""

    name: str
    objective: CampaignObjective
    daily_budget: Money
    duration_days: int
    native: CampaignNative
    success_criterion: str
    kill_criterion: str

    def __post_init__(self) -> None:
        _require_text(self.name, _MAX_PLANNED_NAME_LENGTH, field="planned_campaign_name")
        if not self.daily_budget.is_positive():
            raise PlannedTreeError("planned_campaign_daily_budget_not_positive")
        if self.daily_budget.currency != "EUR":
            raise PlannedTreeError("planned_campaign_currency_unsupported")
        if not (_MIN_DURATION_DAYS <= self.duration_days <= _MAX_DURATION_DAYS):
            raise PlannedTreeError("planned_campaign_duration_invalid")
        _require_channel_minimums(self.native, self.daily_budget, self.duration_days)
        _require_text(
            self.success_criterion,
            _MAX_CRITERION_LENGTH,
            field="planned_campaign_success_criterion",
        )
        _require_text(
            self.kill_criterion, _MAX_CRITERION_LENGTH, field="planned_campaign_kill_criterion"
        )

    @property
    def platform(self) -> PlatformCode:
        if isinstance(self.native, GoogleCampaignNative):
            return PlatformCode.GOOGLE
        return PlatformCode.META

    def to_canonical(self) -> dict[str, object]:
        return {
            "name": self.name,
            "objective": self.objective.value,
            "daily_budget": self.daily_budget,
            "duration_days": self.duration_days,
            "native": self.native.to_canonical(),
            "success_criterion": self.success_criterion,
            "kill_criterion": self.kill_criterion,
        }


# ---------------------------------------------------------------------------
# Local refs -- ordinales estables y direccionables (threat-model.md BL-2)
# ---------------------------------------------------------------------------

CAMPAIGN_LOCAL_REF = "campaign"
_MAX_AD_SETS_PER_PACKAGE = 3
_MAX_ADS_PER_AD_SET = 4
_AD_SET_REF_PATTERN = re.compile(rf"^as#[1-{_MAX_AD_SETS_PER_PACKAGE}]$")
_AD_REF_PATTERN = re.compile(rf"^as#[1-{_MAX_AD_SETS_PER_PACKAGE}]/ad#[1-{_MAX_ADS_PER_AD_SET}]$")


@dataclass(frozen=True, slots=True)
class AdSetRef:
    """Ordinal estable `as#1`..`as#3` (`contracts/api.md LocalRef`)."""

    value: str

    def __post_init__(self) -> None:
        if not _AD_SET_REF_PATTERN.fullmatch(self.value):
            raise PlannedTreeError("ad_set_ref_invalid")

    def __str__(self) -> str:
        return self.value

    @property
    def index(self) -> int:
        return int(self.value.removeprefix("as#"))


@dataclass(frozen=True, slots=True)
class AdRef:
    """Ordinal estable `as#N/ad#M` (`contracts/api.md LocalRef`)."""

    value: str

    def __post_init__(self) -> None:
        if not _AD_REF_PATTERN.fullmatch(self.value):
            raise PlannedTreeError("ad_ref_invalid")

    def __str__(self) -> str:
        return self.value

    @property
    def ad_set_ref(self) -> str:
        return self.value.split("/", 1)[0]

    @property
    def index(self) -> int:
        return int(self.value.rsplit("#", 1)[1])


def parent_local_ref(local_ref: str) -> str | None:
    """Referencia estable al padre de `local_ref` dentro del arbol
    declarado. threat-model.md BL-2: la futura `PackageStepBinding` (T016,
    en rediseño) necesita atar cada paso a su padre sin ambiguedad; como el
    arbol ya garantiza que la posicion coincide con el ordinal declarado
    (`_require_positional_ad_refs`, `campaign_package._require_positional_ad_set_refs`),
    esta funcion pura basta -- no resuelve nada en tiempo de ejecucion."""
    if local_ref == CAMPAIGN_LOCAL_REF:
        return None
    if "/" in local_ref:
        return local_ref.split("/", 1)[0]
    return CAMPAIGN_LOCAL_REF


# ---------------------------------------------------------------------------
# Ad-set-level native choices
# ---------------------------------------------------------------------------


class GoogleAdGroupType(StrEnum):
    SEARCH_STANDARD = "SEARCH_STANDARD"


# Alias de compatibilidad (T021): la fila de Busqueda solo admite
# `MANUAL_CPC`, y ese unico valor ya vive en `GoogleBiddingStrategy`
# (`google_channel_spec.py`) -- reexportarlo aqui evita una segunda
# enumeracion con el mismo literal (INV-15) y no rompe a quien todavia
# importa el nombre antiguo (`mcp/presentation/package_tools.py`).
GoogleAdGroupBiddingStrategy = GoogleBiddingStrategy


class GoogleTargetingMode(StrEnum):
    INHERIT_CAMPAIGN = "INHERIT_CAMPAIGN"


@dataclass(frozen=True, slots=True)
class GoogleAdGroupNative:
    type: GoogleAdGroupType
    bidding_strategy: GoogleBiddingStrategy
    targeting_mode: GoogleTargetingMode

    def to_canonical(self) -> dict[str, object]:
        return {
            "type": self.type.value,
            "bidding_strategy": self.bidding_strategy.value,
            "targeting_mode": self.targeting_mode.value,
        }


@dataclass(frozen=True, slots=True)
class GoogleAssetGroupNative:
    """Solo PERFORMANCE_MAX: el grupo de recursos ES el anuncio, con 0
    `PlannedAd` debajo (data-model.md `GoogleAssetGroupNative`).

    `audience_signal` no es un campo (tasks.md T021, corrige a
    data-model.md): un opcional que hoy vale siempre `None` es un campo
    que mañana lleva listas de clientes (PII con base legal propia) sin
    que nadie lo haya revisado. En v2 lo decide el dueño explicitamente,
    no una fila mas de la tabla."""

    final_url: LandingUrl
    assets: AssetGroupAssetPlan

    def to_canonical(self) -> dict[str, object]:
        return {
            "kind": _ASSET_GROUP_KIND,
            "final_url": str(self.final_url),
            "assets": self.assets.to_canonical(),
        }


class MetaBudgetMode(StrEnum):
    CAMPAIGN = "CAMPAIGN"


class MetaBillingEvent(StrEnum):
    IMPRESSIONS = "IMPRESSIONS"


class MetaOptimizationGoal(StrEnum):
    LINK_CLICKS = "LINK_CLICKS"


class MetaDestinationType(StrEnum):
    WEBSITE = "WEBSITE"


_MIN_AGE = 18
_MAX_AGE = 65
_MAX_DSA_LENGTH = 128
_MIN_COUNTRIES = 1
_MAX_COUNTRIES = 25


@dataclass(frozen=True, slots=True)
class MetaAdSetNative:
    """`advantage_audience` no es un campo: v1 lo fija a 0 siempre
    (invariante 6, "nada se infiere" == nunca variable, nunca inferido)."""

    budget_mode: MetaBudgetMode
    billing_event: MetaBillingEvent
    optimization_goal: MetaOptimizationGoal
    destination_type: MetaDestinationType
    countries: tuple[str, ...]
    age_min: int
    age_max: int
    dsa_beneficiary: str
    dsa_payor: str

    def __post_init__(self) -> None:
        if not (_MIN_COUNTRIES <= len(self.countries) <= _MAX_COUNTRIES):
            raise PlannedTreeError("meta_ad_set_countries_count_invalid")
        _require_valid_country_codes(self.countries, field="meta_ad_set_country")
        if not (_MIN_AGE <= self.age_min <= self.age_max <= _MAX_AGE):
            raise PlannedTreeError("meta_ad_set_age_range_invalid")
        _require_text(self.dsa_beneficiary, _MAX_DSA_LENGTH, field="meta_ad_set_dsa_beneficiary")
        _require_text(self.dsa_payor, _MAX_DSA_LENGTH, field="meta_ad_set_dsa_payor")

    def to_canonical(self) -> dict[str, object]:
        return {
            "budget_mode": self.budget_mode.value,
            "billing_event": self.billing_event.value,
            "optimization_goal": self.optimization_goal.value,
            "destination_type": self.destination_type.value,
            "targeting": {
                "geo_locations": {"countries": list(self.countries)},
                "age_min": self.age_min,
                "age_max": self.age_max,
                "targeting_automation": {"advantage_audience": 0},
            },
            "dsa_beneficiary": self.dsa_beneficiary,
            "dsa_payor": self.dsa_payor,
        }


AdSetNative = GoogleAdGroupNative | GoogleAssetGroupNative | MetaAdSetNative


# ---------------------------------------------------------------------------
# DeliverySchedule
# ---------------------------------------------------------------------------

_TIME_OF_DAY_PATTERN = re.compile(r"^([01][0-9]|2[0-3]):[0-5][0-9]$")
_MIN_WEEKDAY = 0
_MAX_WEEKDAY = 6


@dataclass(frozen=True, slots=True)
class DeliverySchedule:
    """Calendario de entrega del conjunto (data-model.md
    `PlannedAdSet.schedule`). Nota de implementacion T012:
    `contracts/mcp-tools.md §2 PlannedAdSetArgs` todavia no expone un campo
    de entrada para esto, asi que en la practica queda `None`
    ("todos los dias", `AdSetPreview.schedule_plain` por defecto) hasta que
    el contrato de la herramienta lo incorpore -- el tipo existe para que
    el modelo de dominio no sea mas pobre que `data-model.md`."""

    weekdays: tuple[int, ...]
    start_time: str
    end_time: str

    def __post_init__(self) -> None:
        if not self.weekdays or len(set(self.weekdays)) != len(self.weekdays):
            raise PlannedTreeError("delivery_schedule_weekdays_invalid")
        if any(not (_MIN_WEEKDAY <= day <= _MAX_WEEKDAY) for day in self.weekdays):
            raise PlannedTreeError("delivery_schedule_weekday_out_of_range")
        valid_start = _TIME_OF_DAY_PATTERN.fullmatch(self.start_time)
        valid_end = _TIME_OF_DAY_PATTERN.fullmatch(self.end_time)
        if not valid_start or not valid_end:
            raise PlannedTreeError("delivery_schedule_time_invalid")
        if self.start_time >= self.end_time:
            raise PlannedTreeError("delivery_schedule_range_invalid")

    def to_canonical(self) -> dict[str, object]:
        return {
            "weekdays": list(self.weekdays),
            "start_time": self.start_time,
            "end_time": self.end_time,
        }


# ---------------------------------------------------------------------------
# PlannedAd
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class PlannedAd:
    """Lo que el usuario final ve (invariante 3, a nivel de anuncio). La
    plataforma se infiere del tipo de `copy` (`MetaAdCopyPlan` |
    `GoogleAdCopyPlan`): Meta exige creatividad de imagen y CTA; Google RSA
    los prohibe (es texto puro)."""

    local_ref: AdRef
    name: str
    creative: AdCreativeRef
    copy: AdCopyPlan
    landing: LandingUrl
    cta: CallToAction | None = None

    def __post_init__(self) -> None:
        _require_text(self.name, _MAX_PLANNED_NAME_LENGTH, field="planned_ad_name")
        if isinstance(self.copy, MetaAdCopyPlan):
            self._require_meta_creative()
        elif self.cta is not None:
            raise PlannedTreeError("planned_ad_cta_forbidden")

    def _require_meta_creative(self) -> None:
        if self.cta is None:
            raise PlannedTreeError("planned_ad_cta_required")
        if not isinstance(self.creative, ImageCreativeRef):
            raise PlannedTreeError("planned_ad_creative_required")

    def to_canonical(self) -> dict[str, object]:
        return {
            "local_ref": str(self.local_ref),
            "name": self.name,
            "creative": self.creative.to_canonical(),
            "copy": self.copy.to_canonical(),
            "cta": self.cta.value if self.cta is not None else None,
            "landing": str(self.landing),
        }


# ---------------------------------------------------------------------------
# PlannedAdSet
# ---------------------------------------------------------------------------

_MIN_ADS_PER_AD_SET = 1


def _require_positional_ad_refs(ad_set_ref: AdSetRef, ads: tuple[PlannedAd, ...]) -> None:
    for position, ad in enumerate(ads, start=1):
        if ad.local_ref.value != f"{ad_set_ref}/ad#{position}":
            raise PlannedTreeError("planned_ad_ref_out_of_position")


def _google_child_spec(native: AdSetNative) -> GoogleChannelSpec | None:
    """La fila que rige este nodo de segundo nivel (T021): grupo de
    anuncios por su `type`, grupo de recursos por el discriminador
    `ASSET_GROUP` (`spec_for_child_type`, T015). `None` para Meta -- no
    hay tabla que consultar."""
    if isinstance(native, GoogleAdGroupNative):
        return spec_for_child_type(native.type.value)
    if isinstance(native, GoogleAssetGroupNative):
        return spec_for_child_type(_ASSET_GROUP_KIND)
    return None


def _ads_bounds(native: AdSetNative) -> tuple[int, int]:
    spec = _google_child_spec(native)
    if spec is not None:
        return spec.ads_per_node
    return _MIN_ADS_PER_AD_SET, _MAX_ADS_PER_AD_SET


def _require_field_rule(value: object, rule: FieldRule, *, field_name: str) -> None:
    if rule is FieldRule.REQUIRED and value is None:
        raise PlannedTreeError(f"{field_name}_required")
    if rule is FieldRule.FORBIDDEN and value is not None:
        raise PlannedTreeError(f"{field_name}_forbidden")


def _require_ad_set_native_fields(
    *, native: AdSetNative, keywords: KeywordPlan | None, cpc_bid: Money | None
) -> None:
    spec = _google_child_spec(native)
    keywords_rule = spec.keywords if spec is not None else FieldRule.FORBIDDEN
    cpc_bid_rule = spec.cpc_bid if spec is not None else FieldRule.FORBIDDEN
    _require_field_rule(keywords, keywords_rule, field_name="planned_ad_set_keywords")
    _require_field_rule(cpc_bid, cpc_bid_rule, field_name="planned_ad_set_cpc_bid")


def _require_ads_match_platform(ads: tuple[PlannedAd, ...], *, is_google: bool) -> None:
    expected = GoogleAdCopyPlan if is_google else MetaAdCopyPlan
    if any(not isinstance(ad.copy, expected) for ad in ads):
        raise PlannedTreeError("planned_ad_platform_mismatch")


@dataclass(frozen=True, slots=True)
class PlannedAdSet:
    """En Meta, el conjunto de anuncios; en Google SEARCH, el grupo. Lleva
    el publico, el calendario y (Google) las palabras clave."""

    local_ref: AdSetRef
    name: str
    audience: Audience
    native: AdSetNative
    ads: tuple[PlannedAd, ...]
    schedule: DeliverySchedule | None = None
    keywords: KeywordPlan | None = None
    cpc_bid: Money | None = None

    def __post_init__(self) -> None:
        _require_text(self.name, _MAX_PLANNED_NAME_LENGTH, field="planned_ad_set_name")
        minimum_ads, maximum_ads = _ads_bounds(self.native)
        if not (minimum_ads <= len(self.ads) <= maximum_ads):
            raise PlannedTreeError("planned_ad_set_ads_count_invalid")
        _require_positional_ad_refs(self.local_ref, self.ads)
        _require_ad_set_native_fields(
            native=self.native, keywords=self.keywords, cpc_bid=self.cpc_bid
        )
        is_google = isinstance(self.native, GoogleAdGroupNative | GoogleAssetGroupNative)
        _require_ads_match_platform(self.ads, is_google=is_google)

    def to_canonical(self) -> dict[str, object]:
        return {
            "local_ref": str(self.local_ref),
            "name": self.name,
            "audience": self.audience.to_canonical(),
            "native": self.native.to_canonical(),
            "schedule": self.schedule.to_canonical() if self.schedule is not None else None,
            "keywords": self.keywords.to_canonical() if self.keywords is not None else None,
            "cpc_bid": self.cpc_bid,
            "ads": [ad.to_canonical() for ad in self.ads],
        }

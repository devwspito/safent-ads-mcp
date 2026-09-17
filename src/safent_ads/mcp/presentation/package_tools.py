"""`propose_campaign_package` (tasks.md T040; contracts/mcp-tools.md).

Modulo autonomo (mismo criterio de aislamiento que `opportunity_tools.py`/
`experiment_tools.py`): declara sus propios `Args` sobre `mcp.presentation.
args.ToolArgs` y su propio handler; `catalog.py` lo engancha con una linea.

La capa de esquema (este fichero) exige SOLO forma: patrones, longitudes,
enums cerrados, `extra="forbid"`, `strict=True` -- nunca reimplementa la
consistencia semantica del arbol (native/copy coherentes con `platform`,
`local_ref` en posicion, tope de conjuntos/anuncios), que `CampaignPackage.
propose()` ya exige con sus propios codigos de error (plan.md §"Validacion":
tres capas, ninguna confia en la anterior -- esta capa cubre la suya, el
dominio cubre la de el)."""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import UTC, date, datetime
from decimal import Decimal
from typing import Annotated, Any, Literal

from pydantic import BaseModel, BeforeValidator, ConfigDict, Field, model_validator

from safent_ads.mcp.application.caller_scope import CallerScope
from safent_ads.mcp.application.dto import PlatformCode as PlatformCodeArg
from safent_ads.mcp.application.errors import ToolDispatchError
from safent_ads.mcp.presentation.args import BusinessId as BusinessIdStr
from safent_ads.mcp.presentation.args import EntityRefStr, OpaqueId, ToolArgs
from safent_ads.mcp.presentation.args import _contains_url_scheme as _has_url_scheme
from safent_ads.mcp.presentation.campaign_creation_args import (
    ConversionGoalArgs,
    CreationBudgetArgs,
    GoogleCampaignNativeArgs,
    GoogleChannelNotEnabledError,
    GoogleDemandGenNativeArgs,
    GoogleDisplayNativeArgs,
    GoogleNetworksArgs,
    GooglePerformanceMaxNativeArgs,
    GoogleSearchNativeArgs,
    ManualCpcArgs,
    MaximizeClicksArgs,
    MaximizeConversionsArgs,
    MaximizeConversionValueArgs,
    MetaNativeCreationArgs,
    require_enabled_google_channel,
)
from safent_ads.mcp.presentation.google_search_args import (
    GoogleGeographicTargetingArgs,
    GoogleKeywordArgs,
)
from safent_ads.mcp.presentation.registry import ToolClass, ToolDefinition
from safent_ads.packages.application.errors import (
    AmbiguousAccountError,
    AssetGroupIncompleteError,
    BiddingNotAllowedForChannelError,
    BudgetEnvelopeExceededError,
    ChannelTypeNotEnabledError,
    ConversionActionRequiredError,
    CreativeAspectRatioInvalidError,
    CreativeNotUsableError,
    DailyBudgetBelowChannelMinimumError,
    DailyBudgetExceedsCapError,
    DuplicateOpenPackageError,
    DurationBelowChannelMinimumError,
    LandingUrlNotAllowedError,
    NoActiveAccountForPlatformError,
    OfferingNotFoundError,
    PackageApplicationError,
    PackageStructureInvalidError,
    PlatformNativeIncompleteError,
)
from safent_ads.packages.application.ports import CampaignPackageRepository
from safent_ads.packages.application.propose_campaign_package import (
    PlannedAdInput,
    PlannedAdSetInput,
    PlannedAssetGroupInput,
    ProposeCampaignPackage,
    ProposeCampaignPackageRequest,
)
from safent_ads.packages.domain.campaign_package import CampaignPackage
from safent_ads.packages.domain.errors import PlannedTreeError
from safent_ads.packages.domain.identifiers import OfferingId, PackageGroupId
from safent_ads.packages.domain.planned_tree import (
    AdRef,
    AdSetRef,
    CampaignObjective,
    EuPoliticalAdvertisingDeclaration,
    GoogleAdGroupBiddingStrategy,
    GoogleAdGroupNative,
    GoogleAdGroupType,
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
    PlannedCampaign,
    SpecialAdCategory,
)
from safent_ads.packages.domain.values import (
    AdCopyPlan,
    Audience,
    CallToAction,
    GoogleAdCopyPlan,
    Keyword,
    KeywordPlan,
    LandingUrl,
    MatchType,
    MetaAdCopyPlan,
    PackageRationale,
    ResearchNote,
    ResearchNoteKind,
    ResearchSummary,
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
    GoogleAdvertisingChannelType,
)
from safent_ads.proposals.domain.google_channel_spec import (
    GoogleBiddingStrategy as GoogleBiddingStrategyArg,
)
from safent_ads.proposals.domain.money import Money
from safent_ads.shared.ids import BusinessId, EntityRef, PlatformCode

__all__ = ["PackageToolServices", "build_package_tool_definitions"]

type Handler[ArgsT, ReturnT] = Callable[[ArgsT, CallerScope], Awaitable[ReturnT]]

_MAX_AD_SETS = 3
_MAX_ADS_PER_AD_SET = 4
_MAX_RESEARCH_NOTES = 6
_EUR_AMOUNT_PATTERN = r"^[0-9]{1,12}(\.[0-9]{1,2})?$"


class _Strict(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True, frozen=True)


def _parse_iso_date(value: object) -> object:
    return date.fromisoformat(value) if isinstance(value, str) else value


# --- dinero, publico, palabras clave ---------------------------------------


class MoneyArgs(_Strict):
    amount: Annotated[str, Field(pattern=_EUR_AMOUNT_PATTERN)]
    currency: Literal["EUR"]


class RationaleArgs(_Strict):
    owner_request: Annotated[str, Field(min_length=1, max_length=500)]
    why: Annotated[str, Field(min_length=1, max_length=500)]


class ResearchInternalNoteArgs(_Strict):
    kind: Literal["brand", "catalog", "crm", "results"]
    summary: Annotated[str, Field(min_length=1, max_length=300)]


class ResearchExternalNoteArgs(_Strict):
    """`url` carga una URL de verdad -- por eso hereda de `_Strict`, no de
    `ToolArgs`: el rechazo generico de URLs de `ToolArgs` es para texto
    libre que NO deberia llevar una (`query`, `cause_key`, ...), y aplicaria
    tambien aqui por error si esta clase heredase de `ToolArgs` (su
    `model_validator` corre en CADA subclase, no solo en la raiz del
    arbol). La validacion real de formato la exige el dominio
    (`LandingUrl`, misma funcion que `research.external[].url`,
    contracts/mcp-tools.md Revision 2 §R2.4)."""

    kind: Literal["web", "meta_ad_library"]
    summary: Annotated[str, Field(min_length=1, max_length=300)]
    url: Annotated[str, Field(min_length=1, max_length=2048)] | None = None
    # MCP transporta JSON: una fecha SIEMPRE llega como texto ISO-8601, no
    # como `datetime.date`. `strict=True` (de `_Strict`) rechazaria esa
    # cadena sin este `BeforeValidator` -- convierte antes de que el modo
    # estricto compruebe el tipo, en vez de relajar `strict` para toda la
    # clase.
    observed_at: Annotated[date, BeforeValidator(_parse_iso_date)]


class ResearchArgs(_Strict):
    internal: Annotated[list[ResearchInternalNoteArgs], Field(max_length=_MAX_RESEARCH_NOTES)] = (
        Field(default_factory=list)
    )
    external: Annotated[list[ResearchExternalNoteArgs], Field(max_length=_MAX_RESEARCH_NOTES)] = (
        Field(default_factory=list)
    )


# --- campana ----------------------------------------------------------------


class PlannedCampaignArgs(_Strict):
    name: Annotated[str, Field(min_length=1, max_length=128)]
    objective: Literal["reservas", "leads", "ventas", "trafico", "llamadas", "reconocimiento"]
    daily_budget: MoneyArgs
    duration_days: Annotated[int, Field(ge=7, le=90)]
    success_criterion: Annotated[str, Field(min_length=1, max_length=280)]
    kill_criterion: Annotated[str, Field(min_length=1, max_length=280)]
    native: GoogleCampaignNativeArgs | MetaNativeCreationArgs


# --- conjunto / grupo ---------------------------------------------------------


class GoogleAdSetNativeArgs(_Strict):
    type: Literal["SEARCH_STANDARD"]
    bidding_strategy: Literal[GoogleBiddingStrategyArg.MANUAL_CPC]
    targeting_mode: Literal["INHERIT_CAMPAIGN"]


_MIN_ASSET_GROUP_HEADLINES = 3
_MAX_ASSET_GROUP_HEADLINES = 15
_MIN_ASSET_GROUP_LONG_HEADLINES = 1
_MAX_ASSET_GROUP_LONG_HEADLINES = 5
_MIN_ASSET_GROUP_DESCRIPTIONS = 2
_MAX_ASSET_GROUP_DESCRIPTIONS = 5
_MAX_ASSET_GROUP_FIRST_DESCRIPTION_LENGTH = 60


class AssetGroupAssetArgs(_Strict):
    """Grupo de recursos de Maximo Rendimiento (contracts/mcp-tools.md §2
    `AssetGroupAssetArgs`): titulares/descripciones/imagenes que SON el
    anuncio -- Maximo Rendimiento no tiene `PlannedAdArgs`."""

    headlines: Annotated[
        list[Annotated[str, Field(min_length=1, max_length=30)]],
        Field(min_length=_MIN_ASSET_GROUP_HEADLINES, max_length=_MAX_ASSET_GROUP_HEADLINES),
    ]
    long_headlines: Annotated[
        list[Annotated[str, Field(min_length=1, max_length=90)]],
        Field(
            min_length=_MIN_ASSET_GROUP_LONG_HEADLINES, max_length=_MAX_ASSET_GROUP_LONG_HEADLINES
        ),
    ]
    descriptions: Annotated[
        list[Annotated[str, Field(min_length=1, max_length=90)]],
        Field(min_length=_MIN_ASSET_GROUP_DESCRIPTIONS, max_length=_MAX_ASSET_GROUP_DESCRIPTIONS),
    ]
    business_name: Annotated[str, Field(min_length=1, max_length=25)]
    logo_asset_id: OpaqueId
    marketing_image_asset_id: OpaqueId
    square_image_asset_id: OpaqueId

    @model_validator(mode="after")
    def _first_description_is_short(self) -> AssetGroupAssetArgs:
        if len(self.descriptions[0]) > _MAX_ASSET_GROUP_FIRST_DESCRIPTION_LENGTH:
            raise ValueError("asset_group_first_description_too_long")
        return self


class GoogleAssetGroupNativeArgs(_Strict):
    """Nodo de segundo nivel de Maximo Rendimiento -- el unico `native` de
    `PlannedAdSetArgs` sin anuncios debajo (`ads` vacio, ver
    `_ads_match_asset_group_shape`). `final_url` es una URL de verdad como
    `PlannedAdArgs.landing_url`: la forma NO es la autoridad, la allow-list
    de destino de T042 lo es (`_URL_BEARING_FIELD_NAMES`)."""

    kind: Literal["ASSET_GROUP"]
    final_url: Annotated[str, Field(min_length=1, max_length=2048)]
    assets: AssetGroupAssetArgs


class MetaTargetingAutomationArgs(_Strict):
    advantage_audience: Literal[0]


class MetaGeoLocationsArgs(_Strict):
    countries: Annotated[
        list[Annotated[str, Field(pattern=r"^[A-Z]{2}$")]], Field(min_length=1, max_length=25)
    ]


class MetaAdSetTargetingArgs(_Strict):
    geo_locations: MetaGeoLocationsArgs
    age_min: Annotated[int, Field(ge=18, le=65)]
    age_max: Annotated[int, Field(ge=18, le=65)]
    targeting_automation: MetaTargetingAutomationArgs


class MetaAdSetNativeArgs(_Strict):
    budget_mode: Literal["CAMPAIGN"]
    billing_event: Literal["IMPRESSIONS"]
    optimization_goal: Literal["LINK_CLICKS"]
    destination_type: Literal["WEBSITE"]
    targeting: MetaAdSetTargetingArgs
    dsa_beneficiary: Annotated[str, Field(min_length=1, max_length=128)]
    dsa_payor: Annotated[str, Field(min_length=1, max_length=128)]


class CpcBidArgs(_Strict):
    amount: Annotated[str, Field(pattern=r"^[0-9]{1,6}(\.[0-9]{1,2})?$")]
    currency: Literal["EUR"]


# --- anuncio -----------------------------------------------------------------


class MetaAdCopyArgs(_Strict):
    primary_text: Annotated[str, Field(min_length=1, max_length=2000)]
    headline: Annotated[str, Field(min_length=1, max_length=128)]
    description: Annotated[str, Field(min_length=1, max_length=256)]


class GoogleAdCopyArgs(_Strict):
    headlines: Annotated[
        list[Annotated[str, Field(min_length=1, max_length=30)]], Field(min_length=3, max_length=15)
    ]
    descriptions: Annotated[
        list[Annotated[str, Field(min_length=1, max_length=90)]], Field(min_length=2, max_length=4)
    ]


_AD_LOCAL_REF_PATTERN = rf"^as#[1-{_MAX_AD_SETS}]/ad#[1-{_MAX_ADS_PER_AD_SET}]$"


class PlannedAdArgs(_Strict):
    """`landing_url` carga una URL de verdad -- hereda de `_Strict`, no de
    `ToolArgs` (ver nota en `ResearchExternalNoteArgs`)."""

    local_ref: Annotated[str, Field(pattern=_AD_LOCAL_REF_PATTERN)]
    name: Annotated[str, Field(min_length=1, max_length=128)]
    creative_asset_id: OpaqueId | None = None
    landing_url: Annotated[str, Field(min_length=1, max_length=2048)]
    # `copy` es el nombre del contrato (mcp-tools.md §2 `PlannedAdArgs`);
    # como atributo Python choca con `BaseModel.copy()` (sin el plugin de
    # mypy de pydantic, `plugins = []`), asi que el campo de cable sigue
    # llamandose `copy` (alias) y el atributo Python es `ad_copy`.
    ad_copy: Annotated[MetaAdCopyArgs | GoogleAdCopyArgs, Field(alias="copy")]
    cta: Literal["LEARN_MORE", "SHOP_NOW", "SIGN_UP", "CONTACT_US", "BOOK_TRAVEL"] | None = None


class PlannedAdSetArgs(_Strict):
    local_ref: Annotated[str, Field(pattern=rf"^as#[1-{_MAX_AD_SETS}]$")]
    name: Annotated[str, Field(min_length=1, max_length=128)]
    audience_plain: Annotated[str, Field(min_length=1, max_length=200)]
    native: GoogleAdSetNativeArgs | GoogleAssetGroupNativeArgs | MetaAdSetNativeArgs
    keywords: Annotated[list[GoogleKeywordArgs], Field(min_length=1, max_length=50)] | None = None
    cpc_bid: CpcBidArgs | None = None
    ads: Annotated[list[PlannedAdArgs], Field(max_length=_MAX_ADS_PER_AD_SET)] = Field(
        default_factory=list
    )

    @model_validator(mode="after")
    def _ads_match_asset_group_shape(self) -> PlannedAdSetArgs:
        """Maximo Rendimiento no tiene anuncios (data-model.md `ads_per_node
        = (0, 0)`): el grupo de recursos ES el anuncio. Todo lo demas sigue
        exigiendo 1..4 (regla ya vigente, ahora explicita en vez de vivir
        solo en el `min_length` del campo)."""
        is_asset_group = isinstance(self.native, GoogleAssetGroupNativeArgs)
        if is_asset_group and self.ads:
            raise ValueError("planned_ad_set_asset_group_must_have_no_ads")
        if not is_asset_group and not self.ads:
            raise ValueError("planned_ad_set_requires_at_least_one_ad")
        return self


# --- paquete completo ---------------------------------------------------------


_URL_BEARING_FIELD_NAMES = frozenset({"landing_url", "url", "final_url"})
_MAX_URL_SCAN_DEPTH = 8


def _reject_or_validate_urls(value: Any, *, depth: int = 0) -> None:  # noqa: ANN401
    """Sustituye, SOLO en esta clase, la barrera generica de `ToolArgs`
    (`args.py::_reject_free_urls`, que ahora rechaza CUALQUIER URL a
    cualquier profundidad -- threat-model.md C-11/M-3): este paquete SI
    declara campos que son una URL de verdad por contrato
    (`PlannedAdArgs.landing_url`, `GoogleAssetGroupNativeArgs.final_url`,
    `ResearchExternalNoteArgs.url`, contracts/mcp-tools.md §2/§R2.4). Para
    esos exige un HTTPS valido de verdad (`LandingUrl`, la MISMA funcion que
    el dominio) en vez de prohibirlos sin mas -- mas estricto que un rechazo
    generico, no mas laxo; `final_url` pasa la MISMA forma que `landing_url`
    pero la forma no es la autoridad -- la allow-list de destino de T042 lo
    es. El resto del arbol sigue vetando cualquier URL libre (reutiliza
    `_contains_url_scheme`, nunca la reimplementa).

    Al superar `_MAX_URL_SCAN_DEPTH` se LANZA, no se devuelve en silencio
    (residual R-2 de 004, T028): el grupo de recursos anade dos niveles de
    anidamiento y un `return` silencioso dejaria sin escanear justo la rama
    mas profunda -- fail-closed, no fail-open."""
    if depth > _MAX_URL_SCAN_DEPTH:
        raise ValueError("url_scan_depth_exceeded")
    if isinstance(value, dict):
        for key, nested in value.items():
            if key in _URL_BEARING_FIELD_NAMES:
                _require_safe_https_url_or_none(nested)
                continue
            _reject_or_validate_urls(nested, depth=depth + 1)
        return
    if isinstance(value, list):
        for item in value:
            _reject_or_validate_urls(item, depth=depth + 1)
        return
    if isinstance(value, str) and _has_url_scheme(value):
        raise ValueError("no se admiten URLs en argumentos de herramienta")


def _require_safe_https_url_or_none(value: Any) -> None:  # noqa: ANN401
    if value is None or not isinstance(value, str):
        return
    try:
        LandingUrl(value)
    except PlannedTreeError as exc:
        raise ValueError(str(exc)) from exc


class ProposeCampaignPackageArgs(ToolArgs):
    business_id: BusinessIdStr
    platform: PlatformCodeArg
    account_ref: EntityRefStr | None = None
    offering_id: OpaqueId
    campaign: PlannedCampaignArgs
    ad_sets: Annotated[list[PlannedAdSetArgs], Field(min_length=1, max_length=_MAX_AD_SETS)]
    rationale: RationaleArgs
    research: ResearchArgs | None = None
    package_group_id: OpaqueId | None = None

    @model_validator(mode="before")
    @classmethod
    def _reject_urls_in_raw_strings(cls, data: Any) -> Any:  # noqa: ANN401
        if isinstance(data, dict):
            _reject_or_validate_urls(data)
        return data


# --- puertos que necesita el handler -----------------------------------------


@dataclass(frozen=True, slots=True)
class PackageToolServices:
    propose_campaign_package: ProposeCampaignPackage
    packages: CampaignPackageRepository
    # T076 (POLISH): mismo valor que `ProposeCampaignPackage` recibio al
    # construirse (composition/app.py) -- el handler lo comprueba pronto,
    # antes de `_to_request`/`execute()`, para que un canal apagado nunca
    # llegue a tocar un puerto (defensa en profundidad la vuelve a exigir
    # dentro de `execute()`).
    enabled_google_channels: frozenset[GoogleAdvertisingChannelType] = frozenset(
        {GoogleAdvertisingChannelType.SEARCH}
    )


# --- codigos tipados (contracts/mcp-tools.md §4) ------------------------------


class PackageStructureInvalidToolError(ToolDispatchError):
    code = "PACKAGE_STRUCTURE_INVALID"


class CreativeNotUsableToolError(ToolDispatchError):
    code = "CREATIVE_NOT_USABLE"


class LandingUrlNotAllowedToolError(ToolDispatchError):
    code = "LANDING_URL_NOT_ALLOWED"


class BudgetEnvelopeExceededToolError(ToolDispatchError):
    code = "BUDGET_ENVELOPE_EXCEEDED"


class DailyBudgetExceedsCapToolError(ToolDispatchError):
    code = "DAILY_BUDGET_EXCEEDS_CAP"


class AmbiguousAccountToolError(ToolDispatchError):
    code = "AMBIGUOUS_ACCOUNT"


class OfferingNotFoundToolError(ToolDispatchError):
    code = "OFFERING_NOT_FOUND"


class PlatformNativeIncompleteToolError(ToolDispatchError):
    code = "PLATFORM_NATIVE_INCOMPLETE"


class DuplicateOpenPackageToolError(ToolDispatchError):
    code = "DUPLICATE_OPEN_PACKAGE"


class NoActiveAccountToolError(ToolDispatchError):
    """`contracts/mcp-tools.md §4` no cubre "ninguna cuenta activa" -- ver
    `packages.application.errors.NoActiveAccountForPlatformError`."""

    code = "NO_ACTIVE_ACCOUNT"


# --- codigos tipados (contracts/mcp-tools.md §5) --


class BiddingNotAllowedForChannelToolError(ToolDispatchError):
    code = "BIDDING_NOT_ALLOWED_FOR_CHANNEL"


class ConversionActionRequiredToolError(ToolDispatchError):
    code = "CONVERSION_ACTION_REQUIRED"


class AssetGroupIncompleteToolError(ToolDispatchError):
    code = "ASSET_GROUP_INCOMPLETE"


class CreativeAspectRatioInvalidToolError(ToolDispatchError):
    code = "CREATIVE_ASPECT_RATIO_INVALID"


class DailyBudgetBelowChannelMinimumToolError(ToolDispatchError):
    code = "DAILY_BUDGET_BELOW_CHANNEL_MINIMUM"


class DurationBelowChannelMinimumToolError(ToolDispatchError):
    code = "DURATION_BELOW_CHANNEL_MINIMUM"


# --- tasks.md T076 (POLISH, "canales por configuracion") --------------------


class ChannelTypeNotEnabledToolError(ToolDispatchError):
    code = "CHANNEL_TYPE_NOT_ENABLED"


def build_package_tool_definitions(services: PackageToolServices) -> list[ToolDefinition[Any]]:
    return [
        ToolDefinition(
            name="propose_campaign_package",
            description=(
                "Propone un paquete de campana completo y PAUSADO: campana, conjuntos o "
                "grupos, anuncios con imagen y textos, publicos, palabras clave y "
                "presupuesto, en una sola llamada. No publica, no activa y no aprueba: el "
                "propietario lo revisa y publica desde el panel. Usa creatividades ya "
                "generadas y con politicas comprobadas; no inventes identificadores, "
                "categorias, politica UE, redes ni pujas."
            ),
            args_model=ProposeCampaignPackageArgs,
            tool_class=ToolClass.PROPOSAL,
            handler=_propose_campaign_package(services),
            business_id_of=_by_business_id,
        )
    ]


def _by_business_id(args: Any) -> str:  # noqa: ANN401 - extractor generico por posicion
    return str(args.business_id)


# Un unico punto de mapeo (contracts/mcp-tools.md §4/§5): cada
# `PackageApplicationError` que puede salir de `_to_request`/`execute` va a
# su codigo de contrato exacto, sin una cadena de `except` que crezca sin
# limite por cada canal nuevo.
_PROPOSE_ERROR_MAP: dict[type[PackageApplicationError], type[ToolDispatchError]] = {
    OfferingNotFoundError: OfferingNotFoundToolError,
    NoActiveAccountForPlatformError: NoActiveAccountToolError,
    AmbiguousAccountError: AmbiguousAccountToolError,
    DailyBudgetExceedsCapError: DailyBudgetExceedsCapToolError,
    BudgetEnvelopeExceededError: BudgetEnvelopeExceededToolError,
    DuplicateOpenPackageError: DuplicateOpenPackageToolError,
    CreativeNotUsableError: CreativeNotUsableToolError,
    LandingUrlNotAllowedError: LandingUrlNotAllowedToolError,
    PlatformNativeIncompleteError: PlatformNativeIncompleteToolError,
    BiddingNotAllowedForChannelError: BiddingNotAllowedForChannelToolError,
    ConversionActionRequiredError: ConversionActionRequiredToolError,
    AssetGroupIncompleteError: AssetGroupIncompleteToolError,
    CreativeAspectRatioInvalidError: CreativeAspectRatioInvalidToolError,
    DailyBudgetBelowChannelMinimumError: DailyBudgetBelowChannelMinimumToolError,
    DurationBelowChannelMinimumError: DurationBelowChannelMinimumToolError,
    PackageStructureInvalidError: PackageStructureInvalidToolError,
    ChannelTypeNotEnabledError: ChannelTypeNotEnabledToolError,
}


def _propose_campaign_package(
    services: PackageToolServices,
) -> Handler[ProposeCampaignPackageArgs, dict[str, Any]]:
    async def handler(
        args: ProposeCampaignPackageArgs, _caller_scope: CallerScope
    ) -> dict[str, Any]:
        try:
            request = _to_request(args, services.enabled_google_channels)
            package = await services.propose_campaign_package.execute(request)
        except tuple(_PROPOSE_ERROR_MAP) as exc:
            raise _PROPOSE_ERROR_MAP[type(exc)](str(exc)) from exc
        return _to_response(package)

    return handler


def _to_response(package: CampaignPackage) -> dict[str, Any]:
    ads_count = sum(len(ad_set.ads) for ad_set in package.ad_sets)
    summary = (
        f"1 campaña · {len(package.ad_sets)} conjuntos · {ads_count} anuncios · "
        f"{package.account_ref.platform.value.capitalize()}"
    )
    return {
        "package_id": str(package.package_id),
        "package_hash": package.package_hash.value,
        "state": package.state.value,
        "summary": summary,
        "money": {
            "daily": package.budget.daily.to_canonical(),
            "monthly_equivalent": package.budget.monthly_equivalent.to_canonical(),
            "total_cap": package.budget.total_cap.to_canonical(),
            "headroom": (
                package.budget.envelope_headroom.to_canonical()
                if package.budget.envelope_headroom is not None
                else None
            ),
            "headroom_reason": package.budget.envelope_reason,
        },
        "expires_at": package.expires_at.isoformat(),
        "panel_hint": "El propietario lo revisa y publica en Anuncios → Propuestas.",
    }


# --- Args -> dominio -----------------------------------------------------------


def _to_request(
    args: ProposeCampaignPackageArgs,
    enabled_google_channels: frozenset[GoogleAdvertisingChannelType],
) -> ProposeCampaignPackageRequest:
    platform = PlatformCode(args.platform.value)
    campaign = _decode_campaign(args.campaign, enabled_google_channels)
    ad_sets = tuple(_decode_ad_set(ad_set) for ad_set in args.ad_sets)
    return ProposeCampaignPackageRequest(
        business_id=BusinessId.parse(args.business_id),
        platform=platform,
        account_ref=EntityRef.parse(args.account_ref) if args.account_ref else None,
        offering_id=OfferingId(args.offering_id),
        campaign=campaign,
        ad_sets=ad_sets,
        rationale=PackageRationale(
            owner_request=args.rationale.owner_request, why=args.rationale.why
        ),
        research=_decode_research(args.research),
        package_group_id=PackageGroupId(args.package_group_id) if args.package_group_id else None,
    )


def _decode_campaign(
    args: PlannedCampaignArgs, enabled_google_channels: frozenset[GoogleAdvertisingChannelType]
) -> PlannedCampaign:
    daily_budget = Money.of(args.daily_budget.amount, args.daily_budget.currency)
    native = _decode_campaign_native(args.native, enabled_google_channels)
    try:
        return PlannedCampaign(
            name=args.name,
            objective=CampaignObjective(args.objective),
            daily_budget=daily_budget,
            duration_days=args.duration_days,
            native=native,
            success_criterion=args.success_criterion,
            kill_criterion=args.kill_criterion,
        )
    except PlannedTreeError as exc:
        raise _translate_channel_minimum_error(exc, native, daily_budget) from exc


_CHANNEL_MINIMUM_CODES = frozenset(
    {
        "planned_campaign_daily_budget_below_channel_minimum",
        "planned_campaign_duration_below_channel_minimum",
    }
)


def _translate_channel_minimum_error(
    exc: PlannedTreeError,
    native: GoogleCampaignNative | MetaCampaignNative,
    daily_budget: Money,
) -> PackageApplicationError | PlannedTreeError:
    """`PlannedCampaign.__post_init__` (dominio) ya exige el minimo de
    presupuesto/duracion del canal (contracts/mcp-tools.md §5) -- lo que
    faltaba era traducir sus dos codigos al tipo que `_propose_campaign_
    package` sabe mapear."""
    code = str(exc)
    if not isinstance(native, GoogleCampaignNative) or code not in _CHANNEL_MINIMUM_CODES:
        return exc
    spec = CHANNEL_SPECS[native.advertising_channel_type]
    if code == "planned_campaign_duration_below_channel_minimum":
        return DurationBelowChannelMinimumError(minimum=spec.min_duration_days)
    channel = native.advertising_channel_type.value
    if daily_budget < spec.min_daily_budget:
        return DailyBudgetBelowChannelMinimumError(
            minimum=str(spec.min_daily_budget.amount), channel=channel
        )
    target_cpa = getattr(native.bidding_strategy, "target_cpa", None)
    # El unico otro motivo por el que el dominio pudo lanzar este codigo
    # (`_require_channel_minimums`, planned_tree.py): `daily_budget` ya
    # paso `>= spec.min_daily_budget` arriba, asi que aqui SOLO queda el
    # caso `target_cpa`.
    assert target_cpa is not None  # noqa: S101 - invariante de _require_channel_minimums
    return DailyBudgetBelowChannelMinimumError(minimum=str(target_cpa.amount), channel=channel)


def _decode_campaign_native(
    native: GoogleSearchNativeArgs
    | GoogleDisplayNativeArgs
    | GoogleDemandGenNativeArgs
    | GooglePerformanceMaxNativeArgs
    | MetaNativeCreationArgs,
    enabled_google_channels: frozenset[GoogleAdvertisingChannelType],
) -> GoogleCampaignNative | MetaCampaignNative:
    if isinstance(native, MetaNativeCreationArgs):
        return _decode_meta_campaign_native(native)
    try:
        require_enabled_google_channel(native, enabled_google_channels)
    except GoogleChannelNotEnabledError as exc:
        raise ChannelTypeNotEnabledError(channel=exc.channel.value) from exc
    eu_political = EuPoliticalAdvertisingDeclaration(native.contains_eu_political_advertising)
    geographic_targeting = _decode_optional_geo(native.geographic_targeting)
    conversion_goals = _decode_conversion_goals(native.conversion_goals)
    if isinstance(native, GoogleSearchNativeArgs):
        return GoogleSearchCampaignNative(
            bidding_strategy=ManualCpc(),
            contains_eu_political_advertising=eu_political,
            network_settings=_decode_network_settings(native.network_settings),
            geographic_targeting=geographic_targeting,
            conversion_goals=conversion_goals,
        )
    bidding = _decode_bidding(native.bidding_strategy)
    if isinstance(native, GoogleDisplayNativeArgs):
        channel = GoogleAdvertisingChannelType.DISPLAY
    elif isinstance(native, GoogleDemandGenNativeArgs):
        channel = GoogleAdvertisingChannelType.DEMAND_GEN
    else:
        channel = GoogleAdvertisingChannelType.PERFORMANCE_MAX
    try:
        if channel is GoogleAdvertisingChannelType.DISPLAY:
            return GoogleDisplayCampaignNative(
                bidding_strategy=bidding,
                contains_eu_political_advertising=eu_political,
                geographic_targeting=geographic_targeting,
                conversion_goals=conversion_goals,
            )
        if channel is GoogleAdvertisingChannelType.DEMAND_GEN:
            return GoogleDemandGenCampaignNative(
                bidding_strategy=bidding,
                contains_eu_political_advertising=eu_political,
                conversion_goals=conversion_goals,
                geographic_targeting=geographic_targeting,
            )
        return GooglePerformanceMaxCampaignNative(
            bidding_strategy=bidding,
            contains_eu_political_advertising=eu_political,
            conversion_goals=conversion_goals,
            geographic_targeting=geographic_targeting,
        )
    except PlannedTreeError as exc:
        raise _translate_google_campaign_native_error(exc, channel) from exc


def _translate_google_campaign_native_error(
    exc: PlannedTreeError, channel: GoogleAdvertisingChannelType
) -> PackageApplicationError | PlannedTreeError:
    """`_require_google_campaign_native` (`packages.domain.planned_tree`)
    ya exige puja admitida y metas de conversion antes de que el arbol
    llegue a `execute()` -- lo que faltaba era traducir sus dos codigos
    del contrato (§5) al tipo que `_propose_campaign_package` sabe mapear;
    el resto (demasiadas metas, geografia invalida) no tiene fila en el
    contrato y sigue sin traducir, tal y como estaba."""
    code = str(exc)
    if code == "google_campaign_bidding_not_allowed":
        allowed = tuple(bidding.value for bidding in CHANNEL_SPECS[channel].allowed_bidding)
        return BiddingNotAllowedForChannelError(channel=channel.value, allowed=allowed)
    if code == "google_campaign_conversion_goals_required":
        return ConversionActionRequiredError()
    return exc


def _decode_meta_campaign_native(native: MetaNativeCreationArgs) -> MetaCampaignNative:
    return MetaCampaignNative(
        objective=MetaObjective(native.objective),
        buying_type=MetaBuyingType(native.buying_type),
        bid_strategy=MetaBidStrategy(native.bid_strategy),
        special_ad_categories=tuple(
            SpecialAdCategory(value) for value in native.special_ad_categories
        ),
        special_ad_category_country=tuple(native.special_ad_category_country),
    )


def _decode_network_settings(args: GoogleNetworksArgs) -> GoogleNetworkSettings:
    return GoogleNetworkSettings(
        target_google_search=args.target_google_search,
        target_search_network=args.target_search_network,
        target_content_network=args.target_content_network,
        target_partner_search_network=args.target_partner_search_network,
    )


def _decode_optional_geo(
    args: GoogleGeographicTargetingArgs | None,
) -> dict[str, object] | None:
    return _decode_geo_targeting(args) if args is not None else None


def _decode_geo_targeting(args: GoogleGeographicTargetingArgs) -> dict[str, object]:
    return {
        "geo_target_constants": list(args.geo_target_constants),
        "positive_geo_target_type": args.positive_geo_target_type,
    }


def _decode_conversion_goals(
    goals: list[ConversionGoalArgs] | None,
) -> tuple[ConversionGoal, ...]:
    if goals is None:
        return ()
    return tuple(ConversionGoal(goal.resource_name) for goal in goals)


def _decode_bidding(
    args: ManualCpcArgs
    | MaximizeClicksArgs
    | MaximizeConversionsArgs
    | MaximizeConversionValueArgs,
) -> GoogleBidding:
    if isinstance(args, ManualCpcArgs):
        return ManualCpc()
    if isinstance(args, MaximizeClicksArgs):
        return MaximizeClicks(cpc_bid_ceiling=_decode_bidding_money(args.cpc_bid_ceiling))
    if isinstance(args, MaximizeConversionsArgs):
        return MaximizeConversions(target_cpa=_decode_bidding_money(args.target_cpa))
    return MaximizeConversionValue(
        target_roas=Decimal(args.target_roas) if args.target_roas is not None else None
    )


def _decode_bidding_money(args: CreationBudgetArgs | None) -> Money | None:
    return Money.of(args.amount, args.currency) if args is not None else None


def _decode_ad_set(args: PlannedAdSetArgs) -> PlannedAdSetInput:
    keywords = _decode_keyword_plan(args.keywords) if args.keywords is not None else None
    cpc_bid = Money.of(args.cpc_bid.amount, args.cpc_bid.currency) if args.cpc_bid else None
    if isinstance(args.native, GoogleAssetGroupNativeArgs):
        return PlannedAdSetInput(
            local_ref=AdSetRef(args.local_ref),
            name=args.name,
            audience=Audience(plain=args.audience_plain, native=_audience_native(args.native)),
            native=_decode_asset_group(args.native),
            keywords=keywords,
            cpc_bid=cpc_bid,
            ads=(),
        )
    return PlannedAdSetInput(
        local_ref=AdSetRef(args.local_ref),
        name=args.name,
        audience=Audience(plain=args.audience_plain, native=_audience_native(args.native)),
        native=_decode_ad_set_native(args.native),
        keywords=keywords,
        cpc_bid=cpc_bid,
        ads=tuple(_decode_ad(ad) for ad in args.ads),
    )


def _decode_asset_group(args: GoogleAssetGroupNativeArgs) -> PlannedAssetGroupInput:
    """Se detiene en un DTO crudo, sin resolver (mismo motivo que
    `PlannedAdInput.creative_asset_id: str | None`, tasks.md T028):
    convertir un `asset_id` en el `ImageCreativeRef` verificado exige
    `CreativeAssetLookupPort` (E/S), que no le corresponde a esta capa."""
    return PlannedAssetGroupInput(
        final_url=args.final_url,
        headlines=tuple(args.assets.headlines),
        long_headlines=tuple(args.assets.long_headlines),
        descriptions=tuple(args.assets.descriptions),
        business_name=args.assets.business_name,
        logo_asset_id=args.assets.logo_asset_id,
        marketing_image_asset_id=args.assets.marketing_image_asset_id,
        square_image_asset_id=args.assets.square_image_asset_id,
    )


def _audience_native(
    native: GoogleAdSetNativeArgs | GoogleAssetGroupNativeArgs | MetaAdSetNativeArgs,
) -> dict[str, object]:
    """`Audience.native` es a QUIEN se le enseña: para Meta, el bloque
    `targeting` (geo/edad/automatizacion); Google SEARCH no modela
    demografia por grupo en este contrato (solo palabras clave) y el grupo
    de recursos de Maximo Rendimiento tampoco (`audience_signal` no es un
    campo, ver `planned_tree.GoogleAssetGroupNative`), asi que ambos dejan
    constancia de que heredan el ambito de la campana -- nunca un
    diccionario vacio, que el dominio rechaza (`audience_native_required`)."""
    if isinstance(native, MetaAdSetNativeArgs):
        return {"targeting": native.targeting.model_dump(mode="json")}
    if isinstance(native, GoogleAssetGroupNativeArgs):
        return {"targeting_mode": GoogleTargetingMode.INHERIT_CAMPAIGN.value}
    return {"targeting_mode": native.targeting_mode}


def _decode_ad_set_native(
    native: GoogleAdSetNativeArgs | MetaAdSetNativeArgs,
) -> GoogleAdGroupNative | MetaAdSetNative:
    if isinstance(native, GoogleAdSetNativeArgs):
        return GoogleAdGroupNative(
            type=GoogleAdGroupType(native.type),
            bidding_strategy=GoogleAdGroupBiddingStrategy(native.bidding_strategy),
            targeting_mode=GoogleTargetingMode(native.targeting_mode),
        )
    return MetaAdSetNative(
        budget_mode=MetaBudgetMode(native.budget_mode),
        billing_event=MetaBillingEvent(native.billing_event),
        optimization_goal=MetaOptimizationGoal(native.optimization_goal),
        destination_type=MetaDestinationType(native.destination_type),
        countries=tuple(native.targeting.geo_locations.countries),
        age_min=native.targeting.age_min,
        age_max=native.targeting.age_max,
        dsa_beneficiary=native.dsa_beneficiary,
        dsa_payor=native.dsa_payor,
    )


def _decode_keyword_plan(keywords: list[GoogleKeywordArgs]) -> KeywordPlan:
    return KeywordPlan(
        tuple(Keyword(text=item.text, match_type=MatchType(item.match_type)) for item in keywords)
    )


def _decode_ad(args: PlannedAdArgs) -> PlannedAdInput:
    return PlannedAdInput(
        local_ref=AdRef(args.local_ref),
        name=args.name,
        creative_asset_id=args.creative_asset_id,
        landing_url=args.landing_url,
        copy=_decode_copy(args.ad_copy),
        cta=CallToAction(args.cta) if args.cta is not None else None,
    )


def _decode_copy(copy: MetaAdCopyArgs | GoogleAdCopyArgs) -> AdCopyPlan:
    if isinstance(copy, MetaAdCopyArgs):
        return MetaAdCopyPlan(
            primary_text=copy.primary_text, headline=copy.headline, description=copy.description
        )
    return GoogleAdCopyPlan(headlines=tuple(copy.headlines), descriptions=tuple(copy.descriptions))


def _decode_research(args: ResearchArgs | None) -> ResearchSummary | None:
    if args is None:
        return None
    proposed_at = datetime.now(UTC)
    return ResearchSummary(
        internal=tuple(
            ResearchNote(
                kind=ResearchNoteKind(note.kind), summary=note.summary, observed_at=proposed_at
            )
            for note in args.internal
        ),
        external=tuple(
            ResearchNote(
                kind=ResearchNoteKind(note.kind),
                summary=note.summary,
                url=note.url,
                observed_at=_date_to_datetime(note.observed_at),
            )
            for note in args.external
        ),
    )


def _date_to_datetime(value: date) -> datetime:
    return datetime(value.year, value.month, value.day, tzinfo=UTC)

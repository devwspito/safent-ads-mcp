"""Explicit planning data; missing budget/URL is None, never a guessed amount."""

from typing import Annotated, Any, ClassVar, Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, model_validator

from safent_ads.opportunities.domain.campaign_brief import CampaignBrief
from safent_ads.proposals.domain.ad_child_creation import _url
from safent_ads.proposals.domain.campaign_creation import creation_budget
from safent_ads.proposals.domain.google_channel_spec import (
    FieldRule,
    GoogleAdvertisingChannelType,
    GoogleBiddingStrategy,
    GoogleChannelSpec,
    spec_for,
)
from safent_ads.proposals.domain.money import Money
from safent_ads.shared.ids import EntityRef, PlatformCode

_CONTROL_BOUNDARY = 32

_REQUIRED = (
    "title",
    "platform",
    "offering_id",
    "account_ref",
    "objective",
    "daily_budget",
    "duration_days",
    "success_criterion",
    "kill_criterion",
    "angle",
    "targeting_seed",
    "landing_url",
    # creation_plan is NOT here: it is optional and derived from the other
    # structured fields when the model leaves it null (hotfix 0.2.20).
)

# Native plan literals every derived plan uses: PAUSED, Search-only, no
# regulated category/EU-political declaration, matching what the owner
# already confirms in the draft's own prose before any plan exists.
_META_OBJECTIVE_KEYWORDS: tuple[tuple[str, str], ...] = (
    ("venta", "OUTCOME_SALES"),
    ("sale", "OUTCOME_SALES"),
    ("lead", "OUTCOME_LEADS"),
    ("interac", "OUTCOME_ENGAGEMENT"),
    ("engagement", "OUTCOME_ENGAGEMENT"),
    ("conocimiento", "OUTCOME_AWARENESS"),
    ("awareness", "OUTCOME_AWARENESS"),
    ("app", "OUTCOME_APP_PROMOTION"),
)
_DEFAULT_META_OBJECTIVE = "OUTCOME_TRAFFIC"
_EU_POLITICAL_ADVERTISING_NO = "DOES_NOT_CONTAIN_EU_POLITICAL_ADVERTISING"
_GOOGLE_SEARCH_ONLY_NETWORKS = {
    "target_google_search": True,
    "target_search_network": True,
    "target_content_network": False,
    "target_partner_search_network": False,
}
# Only what we can resolve without guessing; anything else omits
# geographic_targeting (it is optional -- see proposals.domain.campaign_creation._google).
_GOOGLE_COUNTRY_GEO_TARGETS: dict[str, str] = {
    "españa": "geoTargetConstants/2724",
    "espana": "geoTargetConstants/2724",
    "spain": "geoTargetConstants/2724",
}
_MIN_CONVERSION_GOALS = 1
_MAX_CONVERSION_GOALS = 10
_CONVERSION_GOAL_RESOURCE_NAME_PATTERN = r"^customers/[0-9]{1,20}/conversionActions/[0-9]{1,20}$"

# Sensible, minimal defaults per row (data-model.md §`GoogleChannelSpec`):
# SEARCH keeps the bare-string legacy form (`proposals.domain.campaign_
# creation._resolve_bidding` only accepts it for SEARCH); every other
# channel uses the tagged-object form, with no target -- the owner tunes it
# later from an explicit `creation_plan` if they want a CPA/ROAS goal.
_DEFAULT_GOOGLE_BIDDING_BY_CHANNEL: dict[GoogleAdvertisingChannelType, object] = {
    GoogleAdvertisingChannelType.SEARCH: GoogleBiddingStrategy.MANUAL_CPC.value,
    GoogleAdvertisingChannelType.DISPLAY: {"kind": GoogleBiddingStrategy.MANUAL_CPC.value},
    GoogleAdvertisingChannelType.DEMAND_GEN: {
        "kind": GoogleBiddingStrategy.MAXIMIZE_CONVERSIONS.value
    },
    GoogleAdvertisingChannelType.PERFORMANCE_MAX: {
        "kind": GoogleBiddingStrategy.MAXIMIZE_CONVERSIONS.value
    },
}


def _meta_objective_literal(objective: str) -> str:
    lowered = objective.casefold()
    for keyword, literal in _META_OBJECTIVE_KEYWORDS:
        if keyword in lowered:
            return literal
    return _DEFAULT_META_OBJECTIVE


def _google_geographic_targeting(geo: str | None) -> dict[str, Any] | None:
    if geo is None:
        return None
    lowered = geo.casefold()
    for name, geo_target_constant in _GOOGLE_COUNTRY_GEO_TARGETS.items():
        if name in lowered:
            return {
                "geo_target_constants": [geo_target_constant],
                "positive_geo_target_type": "PRESENCE",
            }
    return None


CREATION_PLAN_EXAMPLE: dict[str, str] = {
    "meta": (
        '{"schema_version":1,"platform":"meta","name":"<title>","status":"PAUSED",'
        '"daily_budget":{"amount":"20.00","currency":"EUR"},"native":{"objective":'
        '"OUTCOME_TRAFFIC","buying_type":"AUCTION","bid_strategy":"LOWEST_COST_WITHOUT_CAP",'
        '"special_ad_categories":[],"special_ad_category_country":[]}}'
    ),
    "google": (
        '{"schema_version":1,"platform":"google","name":"<title>","status":"PAUSED",'
        '"daily_budget":{"amount":"20.00","currency":"EUR"},"native":{'
        '"advertising_channel_type":"SEARCH","bidding_strategy":"MANUAL_CPC",'
        '"network_settings":{"target_google_search":true,"target_search_network":true,'
        '"target_content_network":false,"target_partner_search_network":false},'
        '"contains_eu_political_advertising":"DOES_NOT_CONTAIN_EU_POLITICAL_ADVERTISING"}}'
    ),
    # Tres ejemplos hermanos de "google" (contracts/mcp-tools.md §3), uno por
    # canal restante -- claves nuevas, no sustituyen ninguna: `campaign_
    # draft_tools.py` sigue mirando solo por `platform`.
    "google_display": (
        '{"schema_version":1,"platform":"google","name":"<title>","status":"PAUSED",'
        '"daily_budget":{"amount":"20.00","currency":"EUR"},"native":{'
        '"advertising_channel_type":"DISPLAY","bidding_strategy":{"kind":"MANUAL_CPC"},'
        '"contains_eu_political_advertising":"DOES_NOT_CONTAIN_EU_POLITICAL_ADVERTISING"}}'
    ),
    "google_demand_gen": (
        '{"schema_version":1,"platform":"google","name":"<title>","status":"PAUSED",'
        '"daily_budget":{"amount":"20.00","currency":"EUR"},"native":{'
        '"advertising_channel_type":"DEMAND_GEN",'
        '"bidding_strategy":{"kind":"MAXIMIZE_CONVERSIONS"},'
        '"contains_eu_political_advertising":"DOES_NOT_CONTAIN_EU_POLITICAL_ADVERTISING",'
        '"conversion_goals":[{"resource_name":"customers/1234567890/conversionActions/1"}],'
        '"url_expansion_opt_out":true,"text_asset_automation_enabled":false}}'
    ),
    "google_performance_max": (
        '{"schema_version":1,"platform":"google","name":"<title>","status":"PAUSED",'
        '"daily_budget":{"amount":"20.00","currency":"EUR"},"native":{'
        '"advertising_channel_type":"PERFORMANCE_MAX",'
        '"bidding_strategy":{"kind":"MAXIMIZE_CONVERSIONS"},'
        '"contains_eu_political_advertising":"DOES_NOT_CONTAIN_EU_POLITICAL_ADVERTISING",'
        '"conversion_goals":[{"resource_name":"customers/1234567890/conversionActions/1"}],'
        '"url_expansion_opt_out":true,"text_asset_automation_enabled":false}}'
    ),
}


class DraftError(ValueError):
    def __init__(self, code: str, missing: tuple[str, ...] = ()) -> None:
        super().__init__(code)
        self.code, self.missing = code, missing


class DraftBudget(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True, frozen=True)
    amount: Annotated[str, Field(pattern=r"^[0-9]{1,10}(\.[0-9]{1,2})?$")]
    currency: Literal["EUR"]


class DraftFields(BaseModel):
    """PATCH-shaped closed object. exclude_unset preserves fields not mentioned."""

    model_config = ConfigDict(extra="forbid", strict=True, frozen=True)
    # The stored draft always carries a canonical account reference. The MCP
    # input model turns this off so a bare account id can be resolved against
    # the business's connected accounts BEFORE the domain object is built.
    _require_canonical_account_ref: ClassVar[bool] = True
    title: Annotated[str, Field(min_length=1, max_length=128)] | None = None
    platform: Literal["google", "meta"] | None = None
    google_channel_type: (
        Literal[
            GoogleAdvertisingChannelType.SEARCH,
            GoogleAdvertisingChannelType.DISPLAY,
            GoogleAdvertisingChannelType.DEMAND_GEN,
            GoogleAdvertisingChannelType.PERFORMANCE_MAX,
        ]
        | None
    ) = None
    offering_id: Annotated[str, Field(pattern=r"^[0-9a-fA-F-]{36}$")] | None = None
    account_ref: Annotated[str, Field(max_length=512)] | None = None
    objective: Annotated[str, Field(min_length=1, max_length=280)] | None = None
    daily_budget: DraftBudget | None = None
    duration_days: Annotated[int, Field(ge=1, le=365)] | None = None
    success_criterion: Annotated[str, Field(min_length=1, max_length=280)] | None = None
    kill_criterion: Annotated[str, Field(min_length=1, max_length=280)] | None = None
    angle: Annotated[str, Field(min_length=1, max_length=280)] | None = None
    targeting_seed: Annotated[str, Field(min_length=1, max_length=280)] | None = None
    geo: Annotated[str, Field(max_length=64)] | None = None
    landing_url: Annotated[str, Field(max_length=2048)] | None = None
    image_url: Annotated[str, Field(max_length=2048)] | None = None
    meta_page_id: Annotated[str, Field(pattern=r"^[0-9]{1,32}$")] | None = None
    conversion_goals: (
        Annotated[
            list[Annotated[str, Field(pattern=_CONVERSION_GOAL_RESOURCE_NAME_PATTERN)]],
            Field(min_length=_MIN_CONVERSION_GOALS, max_length=_MAX_CONVERSION_GOALS),
        ]
        | None
    ) = None
    primary_text: Annotated[str, Field(max_length=2000)] | None = None
    headlines: (
        Annotated[list[Annotated[str, Field(min_length=1, max_length=30)]], Field(max_length=15)]
        | None
    ) = None
    descriptions: (
        Annotated[list[Annotated[str, Field(min_length=1, max_length=90)]], Field(max_length=4)]
        | None
    ) = None
    notes: Annotated[str, Field(max_length=4000)] | None = None
    creation_plan: dict[str, Any] | None = None

    @model_validator(mode="after")
    def validate_explicit_fields(self) -> "DraftFields":
        for key, value in self.model_dump().items():
            if isinstance(value, str) and (
                not value.strip()
                or any(
                    ord(char) < _CONTROL_BOUNDARY and not (key == "notes" and char in "\n\r\t")
                    for char in value
                )
            ):
                raise ValueError("draft_text_invalid")
            if key in {"landing_url", "image_url"} and value is not None:
                _url(value)
        if self.account_ref is not None and self._require_canonical_account_ref:
            EntityRef.parse(self.account_ref)
        if self.offering_id is not None:
            UUID(self.offering_id)
        if self.creation_plan is not None:
            creation_budget({"creation_plan": self.creation_plan})
        return self


def missing_fields(fields: DraftFields) -> tuple[str, ...]:
    return tuple(name for name in _REQUIRED if getattr(fields, name) is None)


def _default_meta_native(fields: DraftFields) -> dict[str, Any]:
    if fields.meta_page_id is None:
        raise DraftError("CAMPAIGN_DRAFT_INCOMPLETE", ("meta_page_id",))
    return {
        "objective": _meta_objective_literal(fields.objective or ""),
        "buying_type": "AUCTION",
        "bid_strategy": "LOWEST_COST_WITHOUT_CAP",
        "special_ad_categories": [],
        "special_ad_category_country": [],
    }


def _default_google_conversion_goals(fields: DraftFields) -> list[dict[str, str]]:
    if not fields.conversion_goals:
        raise DraftError("CAMPAIGN_DRAFT_INCOMPLETE", ("conversion_goals",))
    return [{"resource_name": resource_name} for resource_name in fields.conversion_goals]


def _default_google_native(fields: DraftFields, spec: GoogleChannelSpec) -> dict[str, Any]:
    """Construida desde la fila (contracts/mcp-tools.md §3): ningun literal
    de canal ni de puja vive aqui a mano -- `spec` es la unica fuente
    (`GoogleChannelSpec`, T010). Los literales forzados (BL-1) se escriben
    directamente en este dict server-side; no son un campo que el modelo
    pueda mandar (mcp.presentation.campaign_creation_args)."""
    native: dict[str, Any] = {
        "advertising_channel_type": spec.channel_type.value,
        "bidding_strategy": _DEFAULT_GOOGLE_BIDDING_BY_CHANNEL[spec.channel_type],
        "contains_eu_political_advertising": _EU_POLITICAL_ADVERTISING_NO,
    }
    if spec.network_settings is FieldRule.REQUIRED:
        native["network_settings"] = dict(_GOOGLE_SEARCH_ONLY_NETWORKS)
    geographic_targeting = _google_geographic_targeting(fields.geo)
    if geographic_targeting is not None:
        native["geographic_targeting"] = geographic_targeting
    if spec.requires_conversion_goals:
        native["conversion_goals"] = _default_google_conversion_goals(fields)
    native.update(spec.forced_literals)
    return native


def default_creation_plan(fields: DraftFields) -> dict[str, Any]:
    """Native PAUSED plan derived from the draft's own structured fields when
    the model left `creation_plan` null (hotfix 0.2.20, Bug A). Only the
    literals the platform actually needs; nothing inferred beyond that."""
    if fields.platform is None or fields.daily_budget is None or fields.title is None:
        raise DraftError("CAMPAIGN_DRAFT_INCOMPLETE", missing_fields(fields))
    daily_budget = {"amount": fields.daily_budget.amount, "currency": fields.daily_budget.currency}
    if fields.platform == "meta":
        native = _default_meta_native(fields)
    else:
        channel = fields.google_channel_type or GoogleAdvertisingChannelType.SEARCH
        native = _default_google_native(fields, spec_for(channel.value))
    return {
        "schema_version": 1,
        "platform": fields.platform,
        "name": fields.title,
        "status": "PAUSED",
        "daily_budget": daily_budget,
        "native": native,
    }


def completed_brief(fields: DraftFields) -> CampaignBrief:
    missing = missing_fields(fields)
    if missing:
        raise DraftError("CAMPAIGN_DRAFT_INCOMPLETE", missing)
    if (
        fields.daily_budget is None
        or fields.platform is None
        or fields.offering_id is None
        or fields.account_ref is None
        or fields.duration_days is None
        or fields.objective is None
        or fields.success_criterion is None
        or fields.kill_criterion is None
        or fields.angle is None
        or fields.targeting_seed is None
    ):
        raise DraftError("CAMPAIGN_DRAFT_INCOMPLETE", missing)
    plan = (
        fields.creation_plan
        if fields.creation_plan is not None
        else default_creation_plan(fields)
    )
    try:
        return CampaignBrief(
            objective=fields.objective,
            platform=PlatformCode(fields.platform),
            offering_id=fields.offering_id,
            daily_budget=Money.of(fields.daily_budget.amount, fields.daily_budget.currency),
            duration_days=fields.duration_days,
            success_criterion=fields.success_criterion,
            kill_criterion=fields.kill_criterion,
            angle=fields.angle,
            targeting_seed=fields.targeting_seed,
            geo=fields.geo,
            creation_plan=plan,
        )
    except ValueError as exc:
        raise DraftError("CAMPAIGN_DRAFT_PLAN_INVALID", (str(exc),)) from exc

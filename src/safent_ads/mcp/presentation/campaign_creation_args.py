"""Discoverable MCP schema for the same explicit, centrally validated plan
(tasks.md T027; contracts/mcp-tools.md §2, §3). The Google native block is a
union discriminated by `advertising_channel_type` -- one class per row of
`proposals.domain.google_channel_spec.CHANNEL_SPECS` -- so `strict=True` +
`extra="forbid"` reject any shape the row does not admit before Pydantic
even reaches `validate_original_json`.

Search is untouched (`GoogleSearchNativeArgs`, same fields, same
`bidding_strategy: Literal[MANUAL_CPC]`): the regression net in
`tests/contract/mcp/test_search_native_args_wire_shape_unchanged.py`
freezes this byte-for-byte. `GoogleTargetedNativeCreationArgs` is retired --
`geographic_targeting` is now an optional-by-absence field on every variant
(`_reject_explicit_nulls`: absent means absent, `null` is invalid; the
`model_serializer` below omits it, and every other "ausente" field, instead
of dumping `None`).

`url_expansion_opt_out` and the text-automation flag (data-model.md
§`GoogleChannelSpec.forced_literals`) are never fields here -- the model
cannot send them, `extra="forbid"` would reject the attempt even if it
tried. `_with_forced_literals` injects them into a COPY handed only to
`creation_budget` (the domain's exact-JSON check, which already requires
them for DEMAND_GEN/PERFORMANCE_MAX -- `proposals.domain.campaign_creation`
is owned by another lane and out of scope here); the value Pydantic actually
parses is always the client's own, unaugmented JSON."""

from __future__ import annotations

from typing import Annotated, Any, Literal

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    SerializerFunctionWrapHandler,
    model_serializer,
    model_validator,
)

from safent_ads.mcp.presentation.google_search_args import GoogleGeographicTargetingArgs
from safent_ads.proposals.domain.campaign_creation import creation_budget
from safent_ads.proposals.domain.google_channel_spec import (
    ChannelSpecError,
    GoogleAdvertisingChannelType,
    GoogleBiddingStrategy,
    spec_for,
)

_MIN_CONVERSION_GOALS = 1
_MAX_CONVERSION_GOALS = 10
_EuPoliticalDeclarationArgs = Literal[
    "CONTAINS_EU_POLITICAL_ADVERTISING", "DOES_NOT_CONTAIN_EU_POLITICAL_ADVERTISING"
]


class _StrictPlanPart(BaseModel):
    """`extra="forbid"` closes the door the model could otherwise use;
    `_reject_explicit_nulls`/`_omit_absent_fields` enforce the other half of
    "ausente" (data-model.md): an optional field is either present with a
    real value or missing -- `null` is neither, and never round-trips back
    out as one."""

    model_config = ConfigDict(extra="forbid", strict=True, frozen=True)

    @model_validator(mode="before")
    @classmethod
    def _reject_explicit_nulls(cls, value: object) -> object:
        if isinstance(value, dict) and any(item is None for item in value.values()):
            raise ValueError("plan_part_explicit_null_not_allowed")
        return value

    @model_serializer(mode="wrap")
    def _omit_absent_fields(self, handler: SerializerFunctionWrapHandler) -> dict[str, Any]:
        return {key: value for key, value in handler(self).items() if value is not None}


class CreationBudgetArgs(_StrictPlanPart):
    amount: Annotated[str, Field(pattern=r"^[0-9]{1,12}(\.[0-9]{1,2})?$")]
    currency: Literal["EUR"]


class GoogleNetworksArgs(_StrictPlanPart):
    target_google_search: bool
    target_search_network: bool
    target_content_network: bool
    target_partner_search_network: bool


class ConversionGoalArgs(_StrictPlanPart):
    resource_name: Annotated[
        str, Field(pattern=r"^customers/[0-9]{1,20}/conversionActions/[0-9]{1,20}$")
    ]


_ConversionGoalsArgs = Annotated[
    list[ConversionGoalArgs],
    Field(min_length=_MIN_CONVERSION_GOALS, max_length=_MAX_CONVERSION_GOALS),
]


# --- pujas (contracts/mcp-tools.md §2 "Pujas") -------------------------------


class ManualCpcArgs(_StrictPlanPart):
    kind: Literal[GoogleBiddingStrategy.MANUAL_CPC]


class MaximizeClicksArgs(_StrictPlanPart):
    kind: Literal[GoogleBiddingStrategy.MAXIMIZE_CLICKS]
    cpc_bid_ceiling: CreationBudgetArgs | None = None


class MaximizeConversionsArgs(_StrictPlanPart):
    kind: Literal[GoogleBiddingStrategy.MAXIMIZE_CONVERSIONS]
    target_cpa: CreationBudgetArgs | None = None


class MaximizeConversionValueArgs(_StrictPlanPart):
    kind: Literal[GoogleBiddingStrategy.MAXIMIZE_CONVERSION_VALUE]
    # A ratio, never `Money` (INV-17): euros of value per euro spent.
    target_roas: Annotated[str, Field(pattern=r"^[0-9]{1,2}(\.[0-9]{1,4})?$")] | None = None


GoogleBiddingArgs = Annotated[
    ManualCpcArgs | MaximizeClicksArgs | MaximizeConversionsArgs | MaximizeConversionValueArgs,
    Field(discriminator="kind"),
]
GoogleConversionBiddingArgs = Annotated[
    MaximizeConversionsArgs | MaximizeConversionValueArgs, Field(discriminator="kind")
]


# --- union discriminada del nativo de campana (contracts/mcp-tools.md §2) ----


class GoogleSearchNativeArgs(_StrictPlanPart):
    """IDENTICO al de hoy (data-model.md): mismas claves, `bidding_strategy`
    sigue siendo la cadena `MANUAL_CPC`, nunca el objeto etiquetado -- es la
    unica excepcion documentada a "el esquema publica solo la forma de
    objeto" (contracts/mcp-tools.md §2, nota de compatibilidad), congelada
    por `test_search_native_args_wire_shape_unchanged.py`."""

    advertising_channel_type: Literal[GoogleAdvertisingChannelType.SEARCH]
    bidding_strategy: Literal[GoogleBiddingStrategy.MANUAL_CPC]
    contains_eu_political_advertising: _EuPoliticalDeclarationArgs
    network_settings: GoogleNetworksArgs
    geographic_targeting: GoogleGeographicTargetingArgs | None = None
    conversion_goals: _ConversionGoalsArgs | None = None


class GoogleDisplayNativeArgs(_StrictPlanPart):
    advertising_channel_type: Literal[GoogleAdvertisingChannelType.DISPLAY]
    bidding_strategy: GoogleBiddingArgs
    contains_eu_political_advertising: _EuPoliticalDeclarationArgs
    geographic_targeting: GoogleGeographicTargetingArgs | None = None
    conversion_goals: _ConversionGoalsArgs | None = None


class GoogleDemandGenNativeArgs(_StrictPlanPart):
    advertising_channel_type: Literal[GoogleAdvertisingChannelType.DEMAND_GEN]
    bidding_strategy: GoogleConversionBiddingArgs
    contains_eu_political_advertising: _EuPoliticalDeclarationArgs
    geographic_targeting: GoogleGeographicTargetingArgs | None = None
    conversion_goals: _ConversionGoalsArgs


class GooglePerformanceMaxNativeArgs(_StrictPlanPart):
    advertising_channel_type: Literal[GoogleAdvertisingChannelType.PERFORMANCE_MAX]
    bidding_strategy: GoogleConversionBiddingArgs
    contains_eu_political_advertising: _EuPoliticalDeclarationArgs
    geographic_targeting: GoogleGeographicTargetingArgs | None = None
    conversion_goals: _ConversionGoalsArgs


GoogleCampaignNativeArgs = Annotated[
    GoogleSearchNativeArgs
    | GoogleDisplayNativeArgs
    | GoogleDemandGenNativeArgs
    | GooglePerformanceMaxNativeArgs,
    Field(discriminator="advertising_channel_type"),
]


class MetaNativeCreationArgs(_StrictPlanPart):
    objective: Literal[
        "OUTCOME_AWARENESS",
        "OUTCOME_ENGAGEMENT",
        "OUTCOME_LEADS",
        "OUTCOME_SALES",
        "OUTCOME_TRAFFIC",
        "OUTCOME_APP_PROMOTION",
    ]
    buying_type: Literal["AUCTION"]
    bid_strategy: Literal["LOWEST_COST_WITHOUT_CAP"]
    special_ad_categories: list[
        Literal[
            "CREDIT",
            "EMPLOYMENT",
            "HOUSING",
            "ISSUES_ELECTIONS_POLITICS",
            "FINANCIAL_PRODUCTS_SERVICES",
        ]
    ]
    special_ad_category_country: list[Annotated[str, Field(pattern=r"^[A-Z]{2}$")]]


def _with_forced_literals(value: object) -> object:
    """BL-1 capa 3->1 (data-model.md §`GoogleChannelSpec.forced_literals`)
    for this explicit, signed plan: augments a COPY passed only to
    `creation_budget`, never the value Pydantic parses, so the schema below
    keeps these keys out of reach while the domain's exact-JSON check --
    which already requires them for DEMAND_GEN/PERFORMANCE_MAX -- still
    sees them."""
    if not isinstance(value, dict) or value.get("platform") != "google":
        return value
    native = value.get("native")
    if not isinstance(native, dict) or not isinstance(
        native.get("advertising_channel_type"), str
    ):
        return value
    try:
        forced_literals = spec_for(native["advertising_channel_type"]).forced_literals
    except ChannelSpecError:
        return value
    if not forced_literals:
        return value
    return {**value, "native": {**native, **forced_literals}}


class _CreationPlanArgs(_StrictPlanPart):
    schema_version: Literal[1]
    name: Annotated[str, Field(min_length=1, max_length=128)]
    status: Literal["PAUSED"]
    daily_budget: CreationBudgetArgs

    @model_validator(mode="before")
    @classmethod
    def validate_original_json(cls, value: object) -> object:
        # Validate before Pydantic can coerce a literal such as True into 1.
        # No defaults, normalization or inferred political choices.
        creation_budget({"creation_plan": _with_forced_literals(value)})
        return value


class GoogleCampaignCreationArgs(_CreationPlanArgs):
    platform: Literal["google"]
    native: GoogleCampaignNativeArgs


class MetaCampaignCreationArgs(_CreationPlanArgs):
    platform: Literal["meta"]
    native: MetaNativeCreationArgs


CampaignCreationPlanArgs = Annotated[
    GoogleCampaignCreationArgs | MetaCampaignCreationArgs, Field(discriminator="platform")
]


# --- tasks.md T076 (POLISH) -----------------
#
# The discriminator above already closes the shape to the four rows of
# `GoogleAdvertisingChannelType` -- a channel outside that table never
# reaches here (`packages.application.errors`: "CHANNEL_TYPE_NOT_SUPPORTED
# no vive aqui"). This is a different, later gate: the shape IS one of the
# four rows, but this installation has not turned it on yet
# (`ADS_GOOGLE_CHANNELS_ENABLED`, settings.py) -- the T035 security gate
# ships DISPLAY/DEMAND_GEN/PERFORMANCE_MAX OFF by default. A single, shared
# check so `propose_campaign_draft` (`campaign_draft_tools.py`) and
# `propose_campaign_package` (`package_tools.py`) reject it identically,
# before either one touches a port.


class GoogleChannelNotEnabledError(ValueError):
    """Raised by `require_enabled_google_channel`, never by a pydantic
    validator: presentation call sites catch this explicitly and translate
    it to their own typed error, the same way `_translate_channel_minimum_
    error`/`_translate_asset_group_error` translate other cross-field
    signals at their boundary (never swallowed into the generic
    `VALIDATION_ERROR` a raw pydantic `ValueError` would produce)."""

    code = "CHANNEL_TYPE_NOT_ENABLED"

    def __init__(self, channel: GoogleAdvertisingChannelType) -> None:
        super().__init__(f"{self.code}: canal no habilitado en esta instalacion: {channel.value}")
        self.channel = channel


def require_enabled_google_channel(
    native: GoogleCampaignNativeArgs | MetaNativeCreationArgs,
    enabled_channels: frozenset[GoogleAdvertisingChannelType],
) -> None:
    """Meta has no channel concept to gate. Call this BEFORE any port I/O --
    it is pure, it only inspects the already-validated args shape."""
    if isinstance(native, MetaNativeCreationArgs):
        return
    if native.advertising_channel_type not in enabled_channels:
        raise GoogleChannelNotEnabledError(native.advertising_channel_type)

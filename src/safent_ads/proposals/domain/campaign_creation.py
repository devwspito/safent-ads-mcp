"""Explicit, signed native campaign configuration; a prose brief is not executable.

Version 1 deliberately supports only paused Search/manual-CPC and paused Meta
auction/CBO campaigns. It neither generates targeting nor activates delivery.

`_google` (tasks.md T014) queries
`GoogleChannelSpec` instead of comparing channel/bidding literals: it is the
single reader of the Google native block shared by MCP, `packages` and the
broker (BL-5). `ChannelSpecError` from `spec_for` is translated here into
`campaign_creation_channel_unsupported` -- `google_channel_spec.py` cannot
import `CampaignCreationError` without creating a cycle.
"""

import re
from collections.abc import Callable, Mapping
from decimal import Decimal, InvalidOperation
from typing import Final

from safent_ads.proposals.domain.conversion_goal import ConversionGoal, ConversionGoalError
from safent_ads.proposals.domain.google_bidding import (
    GoogleBidding,
    GoogleBiddingError,
    ManualCpc,
    MaximizeClicks,
    MaximizeConversions,
    MaximizeConversionValue,
)
from safent_ads.proposals.domain.google_channel_spec import (
    ChannelSpecError,
    FieldRule,
    GoogleAdvertisingChannelType,
    GoogleBiddingStrategy,
    GoogleChannelSpec,
    spec_for,
)
from safent_ads.proposals.domain.google_search_targeting import valid_geography
from safent_ads.proposals.domain.money import Money
from safent_ads.shared.ids import EntityLevel, EntityRef

_MAX_NAME_LENGTH = 128
_COUNTRY_CODE_LENGTH = 2
_MIN_CONVERSION_GOALS = 1
_MAX_CONVERSION_GOALS = 10
_MONEY_AMOUNT_PATTERN = re.compile(r"[0-9]{1,12}(?:\.[0-9]{1,2})?")

_ALWAYS_PRESENT_NATIVE_KEYS: Final[frozenset[str]] = frozenset(
    {"advertising_channel_type", "bidding_strategy", "contains_eu_political_advertising"}
)


class CampaignCreationError(ValueError):
    """A stable code, never provider input or a credential."""


def google_channel_from_creation_plan(
    creation_plan: object,
) -> GoogleAdvertisingChannelType | None:
    """The Google channel a `creation_plan` resolves to
    (`{"platform": "google", "native": {"advertising_channel_type": ...}}`)
    -- `None` for Meta, a malformed shape, or an absent plan. Pure, no I/O.

    Single implementation of the channel-enablement gate (T035 security
    re-check 2026-09-15, CWE-284): `mcp.presentation.campaign_creation_args.
    require_enabled_google_channel`, `opportunities.application.
    propose_campaign.ProposeCampaign`, `packages.application.
    propose_campaign_package.ProposeCampaignPackage`, `proposals.
    presentation.rest._edited_value`, `proposals.application.
    submit_approval.SubmitApproval` and `execution.application.chokepoint.
    ExecutionChokepoint` all read it from here instead of re-deriving the
    channel on their own -- `opportunities.domain.campaign_brief`
    re-exports this under the same name for its own callers, never
    duplicates the logic (same criterion as the shared SSRF guard: one
    security-critical reader, never copied)."""
    if not isinstance(creation_plan, dict) or creation_plan.get("platform") != "google":
        return None
    native = creation_plan.get("native")
    if not isinstance(native, dict):
        return None
    channel = native.get("advertising_channel_type")
    if not isinstance(channel, str):
        return None
    try:
        return GoogleAdvertisingChannelType(channel)
    except ValueError:
        return None


def creation_budget(value: object, entity_ref: EntityRef | None = None) -> Money:  # noqa: PLR0912 - strict boundary validation, no inferred fields
    if not isinstance(value, Mapping) or not isinstance(value.get("creation_plan"), Mapping):
        raise CampaignCreationError("campaign_creation_plan_required")
    plan = value["creation_plan"]
    if set(plan) != {"schema_version", "platform", "name", "status", "daily_budget", "native"}:
        raise CampaignCreationError("campaign_creation_plan_invalid")
    if type(plan["schema_version"]) is not int or plan["schema_version"] != 1:
        raise CampaignCreationError("campaign_creation_version_unsupported")
    if plan["status"] != "PAUSED":
        raise CampaignCreationError("campaign_creation_requires_paused")
    name = plan["name"]
    if not isinstance(name, str) or not name.strip() or len(name) > _MAX_NAME_LENGTH:
        raise CampaignCreationError("campaign_creation_name_invalid")
    if entity_ref is not None and (
        entity_ref.level != EntityLevel.ACCOUNT or plan["platform"] != entity_ref.platform.value
    ):
        raise CampaignCreationError("campaign_creation_scope_mismatch")
    raw = plan["daily_budget"]
    if not isinstance(raw, Mapping) or set(raw) != {"amount", "currency"}:
        raise CampaignCreationError("campaign_creation_budget_invalid")
    # This first vertical is EUR only: never assume two minor decimals for JPY
    # or convert a currency without a separately approved conversion.
    try:
        if not isinstance(raw["amount"], str) or not re.fullmatch(
            r"[0-9]{1,12}(?:\.[0-9]{1,2})?", raw["amount"]
        ):
            raise CampaignCreationError("campaign_creation_budget_invalid")
        amount = Decimal(raw["amount"]) if isinstance(raw["amount"], str) else Decimal("NaN")
        valid = (
            amount.is_finite() and amount > 0 and amount * 100 == (amount * 100).to_integral_value()
        )
    except (InvalidOperation, ValueError):
        valid = False
    if not valid or raw["currency"] != "EUR":
        raise CampaignCreationError("campaign_creation_budget_invalid")
    native = plan["native"]
    if not isinstance(native, Mapping):
        raise CampaignCreationError("campaign_creation_native_invalid")
    if plan["platform"] == "google":
        _google(native)
    elif plan["platform"] == "meta":
        _meta(native)
    else:
        raise CampaignCreationError("campaign_creation_platform_unsupported")
    budget = Money(amount=amount, currency="EUR")
    # Existing briefs have a budget too. Never sign one amount and reserve another.
    if "daily_budget_amount" in value:
        try:
            matches = Decimal(str(value["daily_budget_amount"])) == budget.amount
        except InvalidOperation:
            matches = False
        if not matches or value.get("daily_budget_currency") != budget.currency:
            raise CampaignCreationError("campaign_creation_budget_mismatch")
    return budget


def _google(native: Mapping[str, object]) -> None:
    channel_value = native.get("advertising_channel_type")
    if not isinstance(channel_value, str):
        raise CampaignCreationError("campaign_creation_channel_unsupported")
    try:
        spec = spec_for(channel_value)
    except ChannelSpecError as error:
        raise CampaignCreationError("campaign_creation_channel_unsupported") from error

    required_keys, optional_keys = _native_key_rules(spec)
    if set(native) - optional_keys != required_keys:
        raise CampaignCreationError("campaign_creation_native_invalid")

    _require_forced_literals(native, spec.forced_literals)
    _require_eu_political_declaration(native)
    if "geographic_targeting" in native and not valid_geography(native["geographic_targeting"]):
        raise CampaignCreationError("campaign_creation_geography_invalid")
    if spec.network_settings is FieldRule.REQUIRED:
        _require_network_settings(native["network_settings"])

    bidding, is_conversion_based = _resolve_bidding(native["bidding_strategy"], spec)
    if bidding not in spec.allowed_bidding:
        raise CampaignCreationError("campaign_creation_bidding_not_allowed")

    _require_conversion_goals(
        native.get("conversion_goals"),
        required=spec.requires_conversion_goals or is_conversion_based,
    )


def _native_key_rules(spec: GoogleChannelSpec) -> tuple[frozenset[str], frozenset[str]]:
    """Exact key set for the row: base fields, plus the row's forced
    literals (BL-1 capa 2) and `network_settings` when the row requires it.

    `conversion_goals` is always optional-by-absence at the structural
    level, even on rows where it is mandatory: its presence/count has a
    dedicated error (`campaign_creation_conversion_goal_required`,
    `_require_conversion_goals`), never the generic
    `campaign_creation_native_invalid` a structural mismatch would raise."""
    required = _ALWAYS_PRESENT_NATIVE_KEYS | frozenset(spec.forced_literals)
    optional = {"geographic_targeting", "conversion_goals"}
    if spec.network_settings is FieldRule.REQUIRED:
        required = required | {"network_settings"}
    return required, frozenset(optional)


def _require_forced_literals(
    native: Mapping[str, object], forced_literals: Mapping[str, object]
) -> None:
    for key, expected in forced_literals.items():
        actual = native.get(key)
        if type(actual) is not type(expected) or actual != expected:
            raise CampaignCreationError("campaign_creation_native_invalid")


def _require_eu_political_declaration(native: Mapping[str, object]) -> None:
    declaration = native["contains_eu_political_advertising"]
    if not isinstance(declaration, str) or declaration not in {
        "CONTAINS_EU_POLITICAL_ADVERTISING",
        "DOES_NOT_CONTAIN_EU_POLITICAL_ADVERTISING",
    }:
        raise CampaignCreationError("campaign_creation_eu_policy_required")


def _require_network_settings(networks: object) -> None:
    if (
        not isinstance(networks, Mapping)
        or set(networks)
        != {
            "target_google_search",
            "target_search_network",
            "target_content_network",
            "target_partner_search_network",
        }
        or any(type(flag) is not bool for flag in networks.values())
    ):
        raise CampaignCreationError("campaign_creation_networks_required")


def _resolve_bidding(value: object, spec: GoogleChannelSpec) -> tuple[GoogleBiddingStrategy, bool]:
    """Returns the requested kind and whether it is conversion-based.

    The bare string form is legacy compatibility for `SEARCH` only (already
    stored `creation_plan`s): every other channel, and every new plan, uses
    the tagged object form `{"kind": ...}`."""
    if value == GoogleBiddingStrategy.MANUAL_CPC:
        if spec.channel_type is not GoogleAdvertisingChannelType.SEARCH:
            raise CampaignCreationError("campaign_creation_native_invalid")
        return GoogleBiddingStrategy.MANUAL_CPC, False
    if not isinstance(value, Mapping) or not isinstance(value.get("kind"), str):
        raise CampaignCreationError("campaign_creation_native_invalid")
    try:
        kind = GoogleBiddingStrategy(value["kind"])
    except ValueError as error:
        raise CampaignCreationError("campaign_creation_native_invalid") from error
    bidding = _build_bidding(kind, value)
    return kind, bidding.is_conversion_based


def _require_exact_keys(
    value: Mapping[str, object], required: frozenset[str], optional: frozenset[str] = frozenset()
) -> None:
    if set(value) - optional != required:
        raise CampaignCreationError("campaign_creation_native_invalid")


def _manual_cpc(value: Mapping[str, object]) -> GoogleBidding:
    _require_exact_keys(value, frozenset({"kind"}))
    return ManualCpc()


def _maximize_clicks(value: Mapping[str, object]) -> GoogleBidding:
    _require_exact_keys(value, frozenset({"kind"}), frozenset({"cpc_bid_ceiling"}))
    return MaximizeClicks(cpc_bid_ceiling=_optional_money(value.get("cpc_bid_ceiling")))


def _maximize_conversions(value: Mapping[str, object]) -> GoogleBidding:
    _require_exact_keys(value, frozenset({"kind"}), frozenset({"target_cpa"}))
    return MaximizeConversions(target_cpa=_optional_money(value.get("target_cpa")))


def _maximize_conversion_value(value: Mapping[str, object]) -> GoogleBidding:
    _require_exact_keys(value, frozenset({"kind"}), frozenset({"target_roas"}))
    return MaximizeConversionValue(target_roas=_optional_ratio(value.get("target_roas")))


_BIDDING_CONSTRUCTORS: Final[
    Mapping[GoogleBiddingStrategy, Callable[[Mapping[str, object]], GoogleBidding]]
] = {
    GoogleBiddingStrategy.MANUAL_CPC: _manual_cpc,
    GoogleBiddingStrategy.MAXIMIZE_CLICKS: _maximize_clicks,
    GoogleBiddingStrategy.MAXIMIZE_CONVERSIONS: _maximize_conversions,
    GoogleBiddingStrategy.MAXIMIZE_CONVERSION_VALUE: _maximize_conversion_value,
}


def _build_bidding(kind: GoogleBiddingStrategy, value: Mapping[str, object]) -> GoogleBidding:
    try:
        return _BIDDING_CONSTRUCTORS[kind](value)
    except GoogleBiddingError as error:
        raise CampaignCreationError("campaign_creation_native_invalid") from error


def _optional_money(raw: object) -> Money | None:
    if raw is None:
        return None
    if (
        not isinstance(raw, Mapping)
        or set(raw) != {"amount", "currency"}
        or not isinstance(raw["amount"], str)
        or not _MONEY_AMOUNT_PATTERN.fullmatch(raw["amount"])
        or raw["currency"] != "EUR"
    ):
        raise CampaignCreationError("campaign_creation_native_invalid")
    try:
        return Money(amount=Decimal(raw["amount"]), currency="EUR")
    except InvalidOperation as error:
        raise CampaignCreationError("campaign_creation_native_invalid") from error


def _optional_ratio(raw: object) -> Decimal | None:
    if raw is None:
        return None
    if not isinstance(raw, str):
        raise CampaignCreationError("campaign_creation_native_invalid")
    try:
        return Decimal(raw)
    except InvalidOperation as error:
        raise CampaignCreationError("campaign_creation_native_invalid") from error


def _require_conversion_goals(raw: object, *, required: bool) -> None:
    if raw is None:
        if required:
            raise CampaignCreationError("campaign_creation_conversion_goal_required")
        return
    if not isinstance(raw, list) or not (
        _MIN_CONVERSION_GOALS <= len(raw) <= _MAX_CONVERSION_GOALS
    ):
        raise CampaignCreationError("campaign_creation_conversion_goal_required")
    for item in raw:
        _require_conversion_goal_shape(item)


def _require_conversion_goal_shape(item: object) -> None:
    if not isinstance(item, Mapping) or set(item) != {"resource_name"}:
        raise CampaignCreationError("campaign_creation_native_invalid")
    resource_name = item["resource_name"]
    if not isinstance(resource_name, str):
        raise CampaignCreationError("campaign_creation_native_invalid")
    try:
        ConversionGoal(resource_name)
    except ConversionGoalError as error:
        raise CampaignCreationError("campaign_creation_native_invalid") from error


def _meta(native: Mapping[str, object]) -> None:
    if set(native) != {
        "objective",
        "special_ad_categories",
        "special_ad_category_country",
        "buying_type",
        "bid_strategy",
    }:
        raise CampaignCreationError("campaign_creation_native_invalid")
    if (
        not isinstance(native["objective"], str)
        or native["objective"]
        not in {
            "OUTCOME_AWARENESS",
            "OUTCOME_ENGAGEMENT",
            "OUTCOME_LEADS",
            "OUTCOME_SALES",
            "OUTCOME_TRAFFIC",
            "OUTCOME_APP_PROMOTION",
        }
        or native["buying_type"] != "AUCTION"
        or native["bid_strategy"] != "LOWEST_COST_WITHOUT_CAP"
    ):
        raise CampaignCreationError("campaign_creation_mode_unsupported")
    categories = native["special_ad_categories"]
    if (
        not isinstance(categories, list)
        or any(
            not isinstance(item, str)
            or item
            not in {
                "CREDIT",
                "EMPLOYMENT",
                "HOUSING",
                "ISSUES_ELECTIONS_POLITICS",
                "FINANCIAL_PRODUCTS_SERVICES",
            }
            for item in categories
        )
        or len(set(categories)) != len(categories)
    ):
        raise CampaignCreationError("campaign_creation_categories_required")
    countries = native["special_ad_category_country"]
    if (
        not isinstance(countries, list)
        or any(
            not isinstance(item, str)
            or len(item) != _COUNTRY_CODE_LENGTH
            or not item.isascii()
            or not item.isalpha()
            or not item.isupper()
            for item in countries
        )
        or len(set(countries)) != len(countries)
        or (categories and not countries)
    ):
        raise CampaignCreationError("campaign_creation_category_countries_required")

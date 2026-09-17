"""`GoogleChannelSpec` (data-model.md §`GoogleChannelSpec`; contracts/mcp-tools.md
§2, §4, §8). One row per `advertising_channel_type`: what that channel
admits and requires -- legal biddings, whether the child node is an ad
group or an asset group, ad format, budget/duration floors. This is **the**
source of truth (INV-15): no channel or bidding literal survives outside
this module (`tests/architecture/test_no_channel_literals_outside_spec.py`).

`spec_for` is fail-closed (INV-16): an unknown channel is rejected before
any other field is read. `ChannelSpecError` is defined here, never in
`campaign_creation.py` -- importing `CampaignCreationError` from there would
create a cycle (`campaign_creation._google()` is the caller that resolves
the row and translates this error into `campaign_creation_channel_unsupported`).
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from enum import StrEnum
from types import MappingProxyType
from typing import Final

from safent_ads.proposals.domain.money import Money

# Locally scoped, mirroring `shared/crypto/ed25519.py::JsonValue`: `proposals`
# has no authorized arrow to `accounts.domain.json_value` (data-model.md
# §Bounded contexts), so the alias is duplicated here rather than imported
# across a bounded context boundary.
type _JsonValue = None | bool | int | float | str | Sequence["_JsonValue"] | Mapping[
    str, "_JsonValue"
]


class GoogleAdvertisingChannelType(StrEnum):
    SEARCH = "SEARCH"
    DISPLAY = "DISPLAY"
    DEMAND_GEN = "DEMAND_GEN"
    PERFORMANCE_MAX = "PERFORMANCE_MAX"


class GoogleBiddingStrategy(StrEnum):
    MANUAL_CPC = "MANUAL_CPC"
    MAXIMIZE_CLICKS = "MAXIMIZE_CLICKS"
    MAXIMIZE_CONVERSIONS = "MAXIMIZE_CONVERSIONS"
    MAXIMIZE_CONVERSION_VALUE = "MAXIMIZE_CONVERSION_VALUE"


class FieldRule(StrEnum):
    REQUIRED = "REQUIRED"
    FORBIDDEN = "FORBIDDEN"
    OPTIONAL = "OPTIONAL"


class GoogleChildNodeKind(StrEnum):
    AD_GROUP = "AD_GROUP"
    ASSET_GROUP = "ASSET_GROUP"


class ChannelSpecError(ValueError):
    """A stable code, never provider input or a credential.

    Raised by `spec_for` for any `advertising_channel_type` without a row in
    `CHANNEL_SPECS`, before any other field of the native block is read.
    """

    def __init__(self, attempted_channel: str) -> None:
        super().__init__("channel_spec_unsupported_channel")
        self.attempted_channel = attempted_channel
        self.supported_channels: frozenset[str] = frozenset(GoogleAdvertisingChannelType)


@dataclass(frozen=True, slots=True)
class GoogleChannelSpec:
    """One row of the channel table (data-model.md §`GoogleChannelSpec`)."""

    channel_type: GoogleAdvertisingChannelType
    allowed_bidding: frozenset[GoogleBiddingStrategy]
    requires_conversion_goals: bool
    network_settings: FieldRule
    geographic_targeting: FieldRule
    child_node: GoogleChildNodeKind
    allowed_child_types: frozenset[str]
    allowed_ad_types: frozenset[str]
    keywords: FieldRule
    cpc_bid: FieldRule
    ads_per_node: tuple[int, int]
    min_daily_budget: Money
    min_duration_days: int
    forced_literals: Mapping[str, _JsonValue]
    api_version: str


def _row(
    *,
    channel_type: GoogleAdvertisingChannelType,
    allowed_bidding: frozenset[GoogleBiddingStrategy],
    requires_conversion_goals: bool,
    network_settings: FieldRule,
    child_node: GoogleChildNodeKind,
    allowed_child_types: frozenset[str],
    allowed_ad_types: frozenset[str],
    keywords: FieldRule,
    cpc_bid: FieldRule,
    ads_per_node: tuple[int, int],
    min_daily_budget: str,
    min_duration_days: int,
    forced_literals: Mapping[str, _JsonValue],
) -> GoogleChannelSpec:
    return GoogleChannelSpec(
        channel_type=channel_type,
        allowed_bidding=allowed_bidding,
        requires_conversion_goals=requires_conversion_goals,
        network_settings=network_settings,
        geographic_targeting=FieldRule.OPTIONAL,
        child_node=child_node,
        allowed_child_types=allowed_child_types,
        allowed_ad_types=allowed_ad_types,
        keywords=keywords,
        cpc_bid=cpc_bid,
        ads_per_node=ads_per_node,
        min_daily_budget=Money.of(min_daily_budget),
        min_duration_days=min_duration_days,
        forced_literals=MappingProxyType(dict(forced_literals)),
        api_version="v25",
    )


# Automation flags off (contracts/mcp-tools.md §4): named positively (does
# text automation stay enabled?) so a forced `False` reads as "opted out",
# never as a double negative.
_AUTOMATION_OFF: Final[Mapping[str, _JsonValue]] = MappingProxyType(
    {"url_expansion_opt_out": True, "text_asset_automation_enabled": False}
)

CHANNEL_SPECS: Final[Mapping[GoogleAdvertisingChannelType, GoogleChannelSpec]] = (
    MappingProxyType(
        {
            GoogleAdvertisingChannelType.SEARCH: _row(
                channel_type=GoogleAdvertisingChannelType.SEARCH,
                allowed_bidding=frozenset(
                    {
                        GoogleBiddingStrategy.MANUAL_CPC,
                        GoogleBiddingStrategy.MAXIMIZE_CLICKS,
                        GoogleBiddingStrategy.MAXIMIZE_CONVERSIONS,
                    }
                ),
                requires_conversion_goals=False,
                network_settings=FieldRule.REQUIRED,
                child_node=GoogleChildNodeKind.AD_GROUP,
                allowed_child_types=frozenset({"SEARCH_STANDARD"}),
                allowed_ad_types=frozenset({"RESPONSIVE_SEARCH_AD"}),
                keywords=FieldRule.REQUIRED,
                cpc_bid=FieldRule.REQUIRED,
                ads_per_node=(1, 4),
                min_daily_budget="5.00",
                min_duration_days=7,
                forced_literals={},
            ),
            GoogleAdvertisingChannelType.DISPLAY: _row(
                channel_type=GoogleAdvertisingChannelType.DISPLAY,
                allowed_bidding=frozenset(
                    {
                        GoogleBiddingStrategy.MANUAL_CPC,
                        GoogleBiddingStrategy.MAXIMIZE_CLICKS,
                        GoogleBiddingStrategy.MAXIMIZE_CONVERSIONS,
                    }
                ),
                requires_conversion_goals=False,
                network_settings=FieldRule.FORBIDDEN,
                child_node=GoogleChildNodeKind.AD_GROUP,
                allowed_child_types=frozenset({"DISPLAY_STANDARD"}),
                allowed_ad_types=frozenset({"RESPONSIVE_DISPLAY_AD"}),
                keywords=FieldRule.FORBIDDEN,
                # Mandatory only with MANUAL_CPC: that condition depends on the
                # chosen bidding, not on the channel alone, so the table can
                # only say "not universal" (OPTIONAL) -- the conditional rule
                # is enforced where the bidding is known (T007/T060).
                cpc_bid=FieldRule.OPTIONAL,
                ads_per_node=(1, 4),
                min_daily_budget="5.00",
                min_duration_days=7,
                forced_literals={},
            ),
            GoogleAdvertisingChannelType.DEMAND_GEN: _row(
                channel_type=GoogleAdvertisingChannelType.DEMAND_GEN,
                allowed_bidding=frozenset(
                    {
                        GoogleBiddingStrategy.MAXIMIZE_CONVERSIONS,
                        GoogleBiddingStrategy.MAXIMIZE_CONVERSION_VALUE,
                    }
                ),
                requires_conversion_goals=True,
                network_settings=FieldRule.FORBIDDEN,
                child_node=GoogleChildNodeKind.AD_GROUP,
                allowed_child_types=frozenset({"DEMAND_GEN_STANDARD"}),
                allowed_ad_types=frozenset({"DEMAND_GEN_MULTI_ASSET_AD"}),
                keywords=FieldRule.FORBIDDEN,
                cpc_bid=FieldRule.FORBIDDEN,
                ads_per_node=(1, 4),
                min_daily_budget="15.00",
                min_duration_days=14,
                forced_literals=_AUTOMATION_OFF,
            ),
            GoogleAdvertisingChannelType.PERFORMANCE_MAX: _row(
                channel_type=GoogleAdvertisingChannelType.PERFORMANCE_MAX,
                allowed_bidding=frozenset(
                    {
                        GoogleBiddingStrategy.MAXIMIZE_CONVERSIONS,
                        GoogleBiddingStrategy.MAXIMIZE_CONVERSION_VALUE,
                    }
                ),
                requires_conversion_goals=True,
                network_settings=FieldRule.FORBIDDEN,
                child_node=GoogleChildNodeKind.ASSET_GROUP,
                allowed_child_types=frozenset(),
                allowed_ad_types=frozenset(),
                keywords=FieldRule.FORBIDDEN,
                cpc_bid=FieldRule.FORBIDDEN,
                ads_per_node=(0, 0),
                min_daily_budget="20.00",
                min_duration_days=14,
                forced_literals=_AUTOMATION_OFF,
            ),
        }
    )
)


def spec_for(channel: str) -> GoogleChannelSpec:
    """Fail-closed lookup (INV-16): raises before reading anything else."""
    try:
        channel_type = GoogleAdvertisingChannelType(channel)
    except ValueError as error:
        raise ChannelSpecError(channel) from error
    spec = CHANNEL_SPECS.get(channel_type)
    if spec is None:
        raise ChannelSpecError(channel)
    return spec


_ASSET_GROUP_MARKER: Final[str] = "ASSET_GROUP"


def spec_for_child_type(child_type: str) -> GoogleChannelSpec:
    """Reverse lookup (tasks.md T015): which row admits this second-level
    node -- an ad-group `type` (`SEARCH_STANDARD`, ...) or the asset-group
    discriminator (`kind: "ASSET_GROUP"`, `GoogleAssetGroupNativeArgs`).
    Fail-closed like `spec_for`: no row for the value raises before the
    caller reads any other field of the node."""
    for spec in CHANNEL_SPECS.values():
        if child_type in spec.allowed_child_types:
            return spec
    if child_type == _ASSET_GROUP_MARKER:
        for spec in CHANNEL_SPECS.values():
            if spec.child_node is GoogleChildNodeKind.ASSET_GROUP:
                return spec
    raise ChannelSpecError(child_type)

"""Discoverable schema; domain validator remains the authority for exact JSON."""

from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from safent_ads.mcp.presentation.google_search_args import GoogleKeywordArgs
from safent_ads.proposals.domain.ad_child_creation import validate_child_payload
from safent_ads.proposals.domain.google_channel_spec import GoogleBiddingStrategy


class _Part(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True, frozen=True)


class CpcBidArgs(_Part):
    amount: Annotated[str, Field(pattern=r"^[0-9]{1,6}(\.[0-9]{1,2})?$")]
    currency: Literal["EUR"]


class GoogleAdGroupNativeArgs(_Part):
    name: Annotated[str, Field(min_length=1, max_length=128)]
    type: Literal["SEARCH_STANDARD"]
    bidding_strategy: Literal[GoogleBiddingStrategy.MANUAL_CPC]
    cpc_bid: CpcBidArgs
    targeting_mode: Literal["INHERIT_CAMPAIGN"]


class GoogleAdGroupWithKeywordsArgs(GoogleAdGroupNativeArgs):
    keywords: Annotated[list[GoogleKeywordArgs], Field(min_length=1, max_length=50)]


class GoogleRsaNativeArgs(_Part):
    type: Literal["RESPONSIVE_SEARCH_AD"]
    headlines: Annotated[
        list[Annotated[str, Field(min_length=1, max_length=30)]], Field(min_length=3, max_length=15)
    ]
    descriptions: Annotated[
        list[Annotated[str, Field(min_length=1, max_length=90)]], Field(min_length=2, max_length=4)
    ]
    final_url: Annotated[
        str,
        Field(
            min_length=1,
            max_length=2048,
            description="Explicit HTTPS landing page, never fetched by Safent; "
            "no credential-bearing URL or private IP.",
        ),
    ]


class CountryTargetingArgs(_Part):
    countries: Annotated[
        list[Annotated[str, Field(pattern=r"^[A-Z]{2}$")]], Field(min_length=1, max_length=25)
    ]


class TargetingAutomationArgs(_Part):
    advantage_audience: Literal[0]


class MetaChildTargetingArgs(_Part):
    geo_locations: CountryTargetingArgs
    age_min: Annotated[int, Field(ge=18, le=65)]
    age_max: Annotated[int, Field(ge=18, le=65)]
    targeting_automation: TargetingAutomationArgs


class MetaAdSetNativeArgs(_Part):
    name: Annotated[str, Field(min_length=1, max_length=128)]
    budget_mode: Literal["CAMPAIGN"]
    billing_event: Literal["IMPRESSIONS"]
    optimization_goal: Literal["LINK_CLICKS"]
    destination_type: Literal["WEBSITE"]
    targeting: MetaChildTargetingArgs
    dsa_beneficiary: Annotated[str, Field(min_length=1, max_length=128)]
    dsa_payor: Annotated[str, Field(min_length=1, max_length=128)]


class MetaAdNativeArgs(_Part):
    name: Annotated[str, Field(min_length=1, max_length=128)]
    creative_id: Annotated[str, Field(pattern=r"^[0-9]{1,32}$")]


class MetaLinkTargetArgs(_Part):
    link: Annotated[str, Field(min_length=1, max_length=2048)]


class MetaCallToActionArgs(_Part):
    type: Literal["LEARN_MORE", "SHOP_NOW", "SIGN_UP", "CONTACT_US", "BOOK_TRAVEL"]
    value: MetaLinkTargetArgs


class MetaLinkDataArgs(_Part):
    link: Annotated[str, Field(min_length=1, max_length=2048)]
    picture: Annotated[
        str,
        Field(
            min_length=1,
            max_length=2048,
            description="Public HTTPS image URL. Meta fetches it; Safent never downloads it.",
        ),
    ]
    message: Annotated[str, Field(min_length=1, max_length=2000)]
    name: Annotated[str, Field(min_length=1, max_length=128)]
    description: Annotated[str, Field(min_length=1, max_length=256)]
    call_to_action: MetaCallToActionArgs


class MetaStoryArgs(_Part):
    page_id: Annotated[
        str,
        Field(
            pattern=r"^[0-9]{1,32}$",
            description=(
                "Explicit Facebook Page ID; verified against this account's "
                "promote_pages before any write."
            ),
        ),
    ]
    link_data: MetaLinkDataArgs


class MetaInlineCreativeArgs(_Part):
    object_story_spec: MetaStoryArgs


class MetaInlineAdNativeArgs(_Part):
    name: Annotated[str, Field(min_length=1, max_length=128)]
    creative_inline: MetaInlineCreativeArgs


class _Plan(_Part):
    schema_version: Literal[1]
    status: Literal["PAUSED"]

    @model_validator(mode="before")
    @classmethod
    def exact_original_json(cls, value: object) -> object:
        validate_child_payload({"child_plan": value})
        return value


class GoogleAdGroupPlanArgs(_Plan):
    platform: Literal["google"]
    kind: Literal["ad_set"]
    native: GoogleAdGroupNativeArgs | GoogleAdGroupWithKeywordsArgs


class GoogleRsaPlanArgs(_Plan):
    platform: Literal["google"]
    kind: Literal["ad"]
    native: GoogleRsaNativeArgs


class MetaAdSetPlanArgs(_Plan):
    platform: Literal["meta"]
    kind: Literal["ad_set"]
    native: MetaAdSetNativeArgs


class MetaAdPlanArgs(_Plan):
    platform: Literal["meta"]
    kind: Literal["ad"]
    native: MetaAdNativeArgs | MetaInlineAdNativeArgs


ChildPlanArgs = GoogleAdGroupPlanArgs | GoogleRsaPlanArgs | MetaAdSetPlanArgs | MetaAdPlanArgs

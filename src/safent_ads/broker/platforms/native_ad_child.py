"""SDK v25/v26 child creation. Each create is one mutation, then exact acknowledgement.

Only existing parent budgets are inherited. No uploads, pixels, attribution,
audiences, political declarations or bid strategy are guessed here.
"""

import re
from collections.abc import Mapping
from copy import deepcopy
from decimal import Decimal
from typing import Any

from safent_ads.broker.platforms.meta_inline_creative import (
    verify_inline_page,
    verify_inline_readback,
)
from safent_ads.broker.platforms.meta_scope import split_meta_scope
from safent_ads.broker.platforms.native_google_targeting import confirm_keywords, keyword_operations
from safent_ads.proposals.domain.google_channel_spec import (
    CHANNEL_SPECS,
    ChannelSpecError,
    GoogleChannelSpec,
    spec_for_child_type,
)

_ASSET_GROUP_KIND = "ASSET_GROUP"


def _google_parent(parent: str, kind: str) -> tuple[str, str]:
    collection = "campaigns" if kind == "ad_set" else "adGroups"
    match = re.fullmatch(rf"customers/([0-9]+)/{collection}/[0-9]+", parent)
    if match is None:
        raise ValueError("ad_child_parent_invalid")
    return match[1], "campaign" if kind == "ad_set" else "ad_group"


def _expected_spec(plan: Mapping[str, Any]) -> GoogleChannelSpec:
    """The row the LIVE parent must match (tasks.md T033: "el canal y la
    puja vivos del padre coinciden con los del plan firmado", not a fixed
    SEARCH/MANUAL_CPC constant). For `ad_set` creation the child's own
    native names its row (`type` for an ad group, `kind: "ASSET_GROUP"`
    for an asset group). For `ad` creation there is no channel field on
    the ad itself -- `allowed_ad_types` is the reverse index, the same
    public table `spec_for_child_type` already reads from."""
    native = plan["native"]
    if plan["kind"] == "ad_set":
        is_asset_group = native.get("kind") == _ASSET_GROUP_KIND
        child_type = _ASSET_GROUP_KIND if is_asset_group else native["type"]
        try:
            return spec_for_child_type(str(child_type))
        except ChannelSpecError as error:
            raise ValueError("ad_child_parent_unsupported") from error
    ad_type = native.get("type")
    for spec in CHANNEL_SPECS.values():
        if ad_type in spec.allowed_ad_types:
            return spec
    raise ValueError("ad_child_parent_unsupported")


def google_prepare(client: Any, parent: str, plan: Mapping[str, Any]) -> None:
    customer, resource = _google_parent(parent, plan["kind"])
    fields = [
        f"{resource}.resource_name",
        "campaign.advertising_channel_type",
        "campaign.bidding_strategy_type",
        "customer.currency_code",
    ]
    if resource == "ad_group":
        fields.append("ad_group.type")
    query = f"SELECT {','.join(fields)} FROM {resource} WHERE {resource}.resource_name = '{parent}'"  # noqa: S608 - parent fullmatched before interpolation; resource fixed enum
    rows = list(client.search_stream(customer, query))
    if len(rows) != 1:
        raise ValueError("ad_child_parent_unverified")
    row = rows[0]
    spec = _expected_spec(plan)
    allowed_bidding = {bidding.value for bidding in spec.allowed_bidding}
    if (
        row.get(f"{resource}.resource_name") != parent
        or row.get("customer.currency_code") != "EUR"
        or row.get("campaign.advertising_channel_type") != spec.channel_type.value
        or row.get("campaign.bidding_strategy_type") not in allowed_bidding
        or (resource == "ad_group" and row.get("ad_group.type") not in spec.allowed_child_types)
    ):
        raise ValueError("ad_child_parent_unsupported")


# tasks.md T022/T034; threat-model.md D-1/AL-6. Same arithmetic as
# `packages.domain.values.MAX_ASSET_GROUP_OPERATIONS` -- duplicated, never
# imported: `broker -> packages` is a forbidden dependency direction
# (data-model.md §Bounded contexts: "packages -> {proposals, execution,
# creative, accounts, shared}"). 15 headlines + 5 long headlines +
# 5 descriptions + business_name + logo + marketing_image + square_image
# = 29 resources; one `asset.create` (or an existing image reference) and
# one `asset_group_asset.create` per resource, plus the `asset_group`
# itself.
_MAX_ASSET_GROUP_RESOURCES = 15 + 5 + 5 + 1 + 3
_MAX_ASSET_GROUP_OPERATIONS = 2 * _MAX_ASSET_GROUP_RESOURCES + 1

_TEXT_ASSET_FIELD_TYPES = (
    ("headlines", "HEADLINE"),
    ("long_headlines", "LONG_HEADLINE"),
    ("descriptions", "DESCRIPTION"),
)
_IMAGE_ASSET_FIELD_TYPES = (
    ("logo", "LOGO"),
    ("marketing_image", "MARKETING_IMAGE"),
    ("square_image", "SQUARE_MARKETING_IMAGE"),
)


def _asset_group_text_values(assets: Mapping[str, Any]) -> list[tuple[str, str]]:
    values = [
        (text, field_type)
        for list_key, field_type in _TEXT_ASSET_FIELD_TYPES
        for text in assets[list_key]
    ]
    values.append((assets["business_name"], "BUSINESS_NAME"))
    return values


def _google_create_asset_group(
    sdk: Any, customer: str, parent: str, native: Mapping[str, Any]
) -> Mapping[str, Any]:
    """One mutation: `assetGroups/-1` + `assets/-2..-n` (text) +
    `asset_group_asset` linking each text and each already-uploaded image
    with its `field_type` (tasks.md T034; contracts/mcp-tools.md §8). No
    `CREATE_AD` exists for Performance Max -- the asset group IS the ad.

    `assets["name"]` is the name the owner signed
    (`platform_completeness._google_asset_group_wire`, commit 63dbce6) --
    it must reach Google verbatim, never be re-derived here."""
    assets = native["assets"]
    name = assets.get("name")
    if not isinstance(name, str) or not name:
        raise ValueError("asset_group_name_missing")
    asset_group_op = sdk.get_type("MutateOperation")
    asset_group = asset_group_op.asset_group_operation.create
    asset_group_temp = f"customers/{customer}/assetGroups/-1"
    asset_group.resource_name = asset_group_temp
    asset_group.campaign = parent
    asset_group.final_urls.append(native["final_url"])
    asset_group.name = name
    asset_group.status = "PAUSED"

    text_values = _asset_group_text_values(assets)
    image_links = [(assets[key], field_type) for key, field_type in _IMAGE_ASSET_FIELD_TYPES]
    if _asset_group_operation_count(len(text_values) + len(image_links)) > (
        _MAX_ASSET_GROUP_OPERATIONS
    ):
        raise ValueError("asset_group_operation_limit")

    operations: list[Any] = [asset_group_op]
    text_ops: list[Any] = []
    for offset, (text, _field_type) in enumerate(text_values, start=2):
        text_op = sdk.get_type("MutateOperation")
        asset = text_op.asset_operation.create
        asset.resource_name = f"customers/{customer}/assets/-{offset}"
        asset.text_asset.text = text
        operations.append(text_op)
        text_ops.append(text_op)

    link_ops: list[Any] = []
    for text_op, (_text, field_type) in zip(text_ops, text_values, strict=True):
        link_ops.append(
            _asset_group_link_operation(
                sdk, asset_group_temp, text_op.asset_operation.create.resource_name, field_type
            )
        )
    for asset_resource, field_type in image_links:
        link_ops.append(
            _asset_group_link_operation(sdk, asset_group_temp, asset_resource, field_type)
        )
    operations.extend(link_ops)

    response = sdk.get_service("GoogleAdsService").mutate(
        customer_id=customer,
        mutate_operations=operations,
        partial_failure=False,
        response_content_type="MUTABLE_RESOURCE",
        retry=None,
    )
    if response.partial_failure_error.code or len(response.mutate_operation_responses) != len(
        operations
    ):
        raise ValueError("asset_group_partial_response")
    results = list(response.mutate_operation_responses)
    asset_group_resource = str(results[0].asset_group_result.resource_name)
    if not re.fullmatch(rf"customers/{customer}/assetGroups/[0-9]+", asset_group_resource):
        raise ValueError("asset_group_confirmation_mismatch")
    text_results = results[1 : 1 + len(text_ops)]
    resolved_text_resources = [str(result.asset_result.resource_name) for result in text_results]
    expected_links = {
        (resolved, field_type)
        for resolved, (_text, field_type) in zip(resolved_text_resources, text_values, strict=True)
    } | set(image_links)
    _confirm_asset_group_links(sdk, customer, asset_group_resource, expected_links)
    return {"child_resource": asset_group_resource, "parent_resource": parent, "status": "PAUSED"}


def _asset_group_operation_count(resource_count: int) -> int:
    return 2 * resource_count + 1


def _asset_group_link_operation(
    sdk: Any, asset_group_temp: str, asset_resource: str, field_type: str
) -> Any:
    operation = sdk.get_type("MutateOperation")
    link = operation.asset_group_asset_operation.create
    link.asset_group = asset_group_temp
    link.asset = asset_resource
    link.field_type = field_type
    return operation


def _confirm_asset_group_links(
    sdk: Any, customer: str, asset_group_resource: str, expected: set[tuple[str, str]]
) -> None:
    """AL-3/T-9: compares the FULL `{(resource_name, field_type)}` set,
    not just the asset group itself -- one resource linked in excess, or
    a cross-linked role (the logo linked as the marketing image), fails
    the step even though the signed bytes matched. `asset_group_asset`
    only echoes `resource_name` on the mutate response, never the linked
    entity, so this rereads by GAQL -- same pattern as
    `live_google_ads_client._confirm_conversion_goals`."""
    query = (
        "SELECT asset_group_asset.asset, asset_group_asset.field_type "  # noqa: S608
        "FROM asset_group_asset "
        f"WHERE asset_group_asset.asset_group = '{asset_group_resource}'"
    )
    actual: set[tuple[str, str]] = set()
    for batch in sdk.get_service("GoogleAdsService").search_stream(
        customer_id=customer, query=query
    ):
        for row in batch.results:
            actual.add((str(row.asset_group_asset.asset), row.asset_group_asset.field_type.name))
    if actual != expected:
        raise ValueError("asset_group_confirmation_mismatch")


def google_create(sdk: Any, parent: str, plan: Mapping[str, Any]) -> Mapping[str, Any]:
    customer, _ = _google_parent(parent, plan["kind"])
    native = plan["native"]
    if plan["kind"] == "ad_set" and native.get("kind") == _ASSET_GROUP_KIND:
        return _google_create_asset_group(sdk, customer, parent, native)
    operation = sdk.get_type("MutateOperation")
    operations = [operation]
    if plan["kind"] == "ad_set":
        child = operation.ad_group_operation.create
        child.name, child.status, child.type = native["name"], "PAUSED", "SEARCH_STANDARD"
        child.campaign = parent
        child.cpc_bid_micros = int(Decimal(native["cpc_bid"]["amount"]) * 1_000_000)
        if "keywords" in native:
            child.resource_name = f"customers/{customer}/adGroups/-1"
            operations.extend(keyword_operations(sdk, child.resource_name, native["keywords"]))
        result_field, payload_field, collection = "ad_group_result", "ad_group", "adGroups"
    else:
        child = operation.ad_group_ad_operation.create
        child.ad_group, child.status = parent, "PAUSED"
        child.ad.final_urls.append(native["final_url"])
        for field in ("headlines", "descriptions"):
            for value in native[field]:
                asset = sdk.get_type("AdTextAsset")
                asset.text = value
                getattr(child.ad.responsive_search_ad, field).append(asset)
        result_field, payload_field, collection = "ad_group_ad_result", "ad_group_ad", "adGroupAds"
    response = sdk.get_service("GoogleAdsService").mutate(
        customer_id=customer,
        mutate_operations=operations,
        partial_failure=False,
        response_content_type="MUTABLE_RESOURCE",
        retry=None,
    )
    if response.partial_failure_error.code or len(response.mutate_operation_responses) != len(
        operations
    ):
        raise ValueError("ad_child_partial_response")
    result = getattr(response.mutate_operation_responses[0], result_field)
    actual = getattr(result, payload_field)
    resource = str(result.resource_name)
    identifier = r"[0-9]+" if plan["kind"] == "ad_set" else rf"{parent.rsplit('/', 1)[1]}~[0-9]+"
    if (
        not re.fullmatch(rf"customers/{customer}/{collection}/{identifier}", resource)
        or actual.status.name != "PAUSED"
    ):
        raise ValueError("ad_child_confirmation_mismatch")
    if plan["kind"] == "ad_set":
        if (
            actual.campaign != parent
            or actual.name != child.name
            or actual.type != child.type
            or actual.cpc_bid_micros != child.cpc_bid_micros
        ):
            raise ValueError("ad_child_confirmation_mismatch")
        if "keywords" in native:
            # proto-plus slices expose raw protobufs (enum fields become ints).
            confirm_keywords(
                list(response.mutate_operation_responses)[1:], native["keywords"], resource
            )
    elif (
        actual.ad_group != parent
        or actual.ad.responsive_search_ad != child.ad.responsive_search_ad
        or list(actual.ad.final_urls) != [native["final_url"]]
    ):
        raise ValueError("ad_child_confirmation_mismatch")
    return {"child_resource": resource, "parent_resource": parent, "status": "PAUSED"}


def _meta_campaign(client: Any, account: str, campaign_id: str) -> None:
    if not re.fullmatch(r"[0-9]+", campaign_id):
        raise ValueError("ad_child_parent_invalid")
    campaign = client.get_node(
        f"{account}/{campaign_id}",
        (
            "objective",
            "buying_type",
            "bid_strategy",
            "daily_budget",
            "lifetime_budget",
            "special_ad_categories",
        ),
    )
    if (
        campaign.get("objective") != "OUTCOME_TRAFFIC"
        or campaign.get("buying_type") != "AUCTION"
        or campaign.get("bid_strategy") != "LOWEST_COST_WITHOUT_CAP"
        or int(campaign.get("daily_budget", 0)) <= 0
        or int(campaign.get("lifetime_budget", 0)) != 0
        or campaign.get("special_ad_categories") not in ([], ["NONE"])
    ):
        raise ValueError("ad_child_parent_unsupported")


def meta_prepare(client: Any, parent: str, plan: Mapping[str, Any]) -> None:
    account, node = split_meta_scope(parent)
    if node == account or client.campaign_creation_currency(account) != "EUR":
        raise ValueError("ad_child_parent_invalid")
    if plan["kind"] == "ad_set":
        _meta_campaign(client, account, node)
    else:
        adset = client.get_node(parent, ("campaign_id", "optimization_goal", "destination_type"))
        if (
            adset.get("optimization_goal") != "LINK_CLICKS"
            or adset.get("destination_type") != "WEBSITE"
        ):
            raise ValueError("ad_child_parent_unsupported")
        _meta_campaign(client, account, str(adset.get("campaign_id")))
        # get_node performs an independent provider account ownership check.
        if "creative_inline" in plan["native"]:
            verify_inline_page(client, account, plan["native"]["creative_inline"])
        else:
            creative = client.get_node(f"{account}/{plan['native']['creative_id']}", ("id",))
            if creative.get("id") != plan["native"]["creative_id"]:
                raise ValueError("ad_child_creative_unverified")


def meta_create(client: Any, api: Any, parent: str, plan: Mapping[str, Any]) -> Mapping[str, Any]:
    account, node = split_meta_scope(parent)
    native = plan["native"]
    fields = {"name": native["name"], "status": "PAUSED"}
    if plan["kind"] == "ad_set":
        edge = "adsets"
        fields.update({key: value for key, value in native.items() if key != "budget_mode"})
        fields["campaign_id"] = node
    else:
        edge = "ads"
        fields.update(
            adset_id=node,
            creative=deepcopy(native["creative_inline"])
            if "creative_inline" in native
            else {"creative_id": native["creative_id"]},
        )
    created = api.call("POST", [account, edge], params=fields).json()
    child_id = created.get("id")
    if not isinstance(child_id, str) or not re.fullmatch(r"[0-9]+", child_id):
        raise ValueError("ad_child_partial_response")
    actual = api.call(
        "GET", [child_id], params={"fields": ",".join((*fields, "account_id"))}
    ).json()
    client._check_owner(account, actual)
    for key, value in fields.items():
        if key == "creative":
            if "creative_inline" in native:
                verify_inline_readback(
                    client, api, account, actual.get(key), native["creative_inline"]
                )
            elif (
                not isinstance(actual.get(key), dict)
                or actual[key].get("id") != native["creative_id"]
            ):
                raise ValueError("ad_child_confirmation_mismatch")
        elif key == "targeting":
            # Reject undocumented/default broadening rather than infer equivalence.
            confirmed, expected = deepcopy(actual.get(key)), deepcopy(value)
            if not isinstance(confirmed, dict):
                raise ValueError("ad_child_confirmation_mismatch")
            try:
                confirmed["geo_locations"]["countries"] = sorted(
                    confirmed["geo_locations"]["countries"]
                )
                expected["geo_locations"]["countries"] = sorted(
                    expected["geo_locations"]["countries"]
                )
            except (KeyError, TypeError):
                raise ValueError("ad_child_confirmation_mismatch") from None
            if confirmed != expected:
                raise ValueError("ad_child_confirmation_mismatch")
        elif str(actual.get(key)) != str(value):
            raise ValueError("ad_child_confirmation_mismatch")
    return {
        "child_resource": f"{account}/{child_id}",
        "parent_resource": parent,
        "status": "PAUSED",
    }

"""Criteria within the same approved, atomic Google mutation; no separate writes."""

import re
from collections.abc import Mapping, Sequence
from typing import Any


def keyword_operations(
    sdk: Any, temporary_group: str, keywords: Sequence[Mapping[str, str]]
) -> list[Any]:
    operations = []
    for keyword in keywords:
        operation = sdk.get_type("MutateOperation")
        criterion = operation.ad_group_criterion_operation.create
        criterion.ad_group = temporary_group
        criterion.status = "PAUSED"
        criterion.negative = False
        criterion.keyword.text = keyword["text"]
        criterion.keyword.match_type = keyword["match_type"]
        operations.append(operation)
    return operations


def confirm_keywords(
    results: Sequence[Any],
    keywords: Sequence[Mapping[str, str]],
    group: str,
) -> list[str]:
    if len(results) != len(keywords):
        raise ValueError("ad_child_keyword_confirmation_mismatch")
    prefix = group.replace("/adGroups/", "/adGroupCriteria/")
    resources = []
    for response, keyword in zip(results, keywords, strict=True):
        result = response.ad_group_criterion_result
        actual = result.ad_group_criterion
        resource = str(result.resource_name)
        if (
            not re.fullmatch(re.escape(prefix) + r"~[0-9]+", resource)
            or actual.ad_group != group
            or actual.status.name != "PAUSED"
            or actual.negative
            or actual.keyword.text != keyword["text"]
            or actual.keyword.match_type.name != keyword["match_type"]
            or resource in resources
        ):
            raise ValueError("ad_child_keyword_confirmation_mismatch")
        resources.append(resource)
    return resources


def geography_operations(
    sdk: Any,
    campaign: Any,
    geography: Mapping[str, Any],
    temporary_campaign: str,
) -> list[Any]:
    campaign.resource_name = temporary_campaign
    campaign.geo_target_type_setting.positive_geo_target_type = geography[
        "positive_geo_target_type"
    ]
    operations = []
    for location in geography["geo_target_constants"]:
        operation = sdk.get_type("MutateOperation")
        criterion = operation.campaign_criterion_operation.create
        criterion.campaign = temporary_campaign
        criterion.negative = False
        criterion.location.geo_target_constant = location
        operations.append(operation)
    return operations


def confirm_geography(
    results: Sequence[Any],
    geography: Mapping[str, Any],
    campaign: str,
    actual_campaign: Any,
) -> list[str]:
    locations = geography["geo_target_constants"]
    if (
        len(results) != len(locations)
        or actual_campaign.geo_target_type_setting.positive_geo_target_type.name != "PRESENCE"
    ):
        raise ValueError("campaign_creation_geography_confirmation_mismatch")
    prefix = campaign.replace("/campaigns/", "/campaignCriteria/")
    resources = []
    for response, location in zip(results, locations, strict=True):
        result = response.campaign_criterion_result
        actual = result.campaign_criterion
        resource = str(result.resource_name)
        if (
            not re.fullmatch(re.escape(prefix) + r"~[0-9]+", resource)
            or actual.campaign != campaign
            or actual.negative
            or actual.location.geo_target_constant != location
            or resource in resources
        ):
            raise ValueError("campaign_creation_geography_confirmation_mismatch")
        resources.append(resource)
    return resources

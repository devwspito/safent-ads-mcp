"""Local allowlist, independent of upstream instructions and annotations.

No arbitrary forwarding: account selection belongs to Safent, and the advertised
schema is validated before each call. New upstream tools require code review.
"""

import re
from datetime import date
from typing import Any

from jsonschema import Draft202012Validator

from safent_ads.accounts.domain.refs import AccountRef
from safent_ads.broker.platforms.gaql_validator import validate_gaql
from safent_ads.shared.ids import PlatformCode

GOOGLE_TOOLS = frozenset({"search_search", "metadata_get_resource_metadata"})
META_TOOLS = frozenset({"ads_get_ad_entities", "ads_get_opportunity_score"})
_META_ARGUMENTS = frozenset(
    {
        "level",
        "fields",
        "date_preset",
        "time_range",
        "breakdowns",
        "limit",
    }
)
MAX_RESULT_BYTES = 256 * 1024
_MAX_ROWS = 500
_MAX_WINDOW_DAYS = 366


class NativeMcpDeniedError(ValueError):
    """Deliberately contains no credentials or upstream response bodies."""


def allowed_tools(platform: PlatformCode) -> frozenset[str]:
    return GOOGLE_TOOLS if platform == PlatformCode.GOOGLE else META_TOOLS


def validate_schema(schema: dict[str, Any]) -> None:
    # Never resolve an external JSON Schema reference (SSRF / token disclosure).
    def walk(value: Any) -> None:
        if isinstance(value, dict):
            for key, child in value.items():
                if key in {"$ref", "$dynamicRef"} and (
                    not isinstance(child, str) or not child.startswith("#")
                ):
                    raise NativeMcpDeniedError("native_mcp_external_schema_reference")
                walk(child)
        elif isinstance(value, list):
            for child in value:
                walk(child)

    walk(schema)
    Draft202012Validator.check_schema(schema)


def account_field(platform: PlatformCode, tool: str, schema: dict[str, Any]) -> str | None:
    if platform == PlatformCode.GOOGLE:
        return _google_account_field(tool, schema)
    properties = schema.get("properties", {})
    candidates = [key for key in ("ad_account_id", "account_id") if key in properties]
    if len(candidates) != 1:
        raise NativeMcpDeniedError("native_mcp_unrecognized_account_schema")
    return candidates[0]


def _google_account_field(tool: str, schema: dict[str, Any]) -> str | None:
    if tool != "search_search":
        return None
    # Same strictness as the Meta path (never hard-code a name blind): a
    # pinned upstream that renames/drops `customer_id` or stops declaring
    # `additionalProperties: false` must deny, not silently fall back to
    # whatever account the ADC/GOOGLE_ADS_LOGIN_CUSTOMER_ID defaults to.
    if (
        "customer_id" not in schema.get("properties", {})
        or "customer_id" not in schema.get("required", [])
        or schema.get("additionalProperties") is not False
    ):
        raise NativeMcpDeniedError("native_mcp_unrecognized_account_schema")
    return "customer_id"


def build_arguments(
    account: AccountRef, tool: str, supplied: dict[str, Any], schema: dict[str, Any]
) -> dict[str, Any]:
    if tool not in allowed_tools(account.platform):
        raise NativeMcpDeniedError("native_mcp_tool_not_allowed")
    validate_schema(schema)
    arguments = dict(supplied)
    if account.platform == PlatformCode.GOOGLE:
        _google_arguments(tool, arguments)
    else:
        if set(arguments) - _META_ARGUMENTS:
            raise NativeMcpDeniedError("native_mcp_arguments_not_allowed")
        if tool == "ads_get_opportunity_score" and arguments:
            raise NativeMcpDeniedError("native_mcp_arguments_not_allowed")
        if tool == "ads_get_ad_entities":
            _limit(arguments)
            _meta_arguments(arguments)
    field = account_field(account.platform, tool, schema)
    if field is not None:
        # The caller cannot pick a different account, even with an otherwise
        # valid provider token which happens to cover several businesses.
        arguments[field] = account.external_account_id
    Draft202012Validator(schema).validate(arguments)
    return arguments


def _limit(arguments: dict[str, Any]) -> None:
    limit = arguments.setdefault("limit", 100)
    if type(limit) is not int or not 1 <= limit <= _MAX_ROWS:
        raise NativeMcpDeniedError("native_mcp_invalid_limit")


def _google_arguments(tool: str, arguments: dict[str, Any]) -> None:
    if tool == "metadata_get_resource_metadata":
        if set(arguments) != {"resource_name"}:
            raise NativeMcpDeniedError("native_mcp_arguments_not_allowed")
        resource = arguments["resource_name"]
        if not isinstance(resource, str) or not re.fullmatch(r"[a-z_]+", resource):
            raise NativeMcpDeniedError("native_mcp_invalid_resource")
        validate_gaql(f"SELECT campaign.id FROM {resource}")  # noqa: S608 - validated, never executed
        return
    if set(arguments) - {"fields", "resource", "conditions", "orderings", "limit"}:
        raise NativeMcpDeniedError("native_mcp_arguments_not_allowed")
    _limit(arguments)
    fields = arguments.get("fields")
    if not isinstance(fields, list) or not fields or any(not isinstance(f, str) for f in fields):
        raise NativeMcpDeniedError("native_mcp_invalid_fields")
    if any(not re.fullmatch(r"[a-z_][a-z0-9_.]*", field) for field in fields):
        raise NativeMcpDeniedError("native_mcp_invalid_fields")
    if not isinstance(arguments.get("resource"), str) or not re.fullmatch(
        r"[a-z_]+", arguments["resource"]
    ):
        raise NativeMcpDeniedError("native_mcp_invalid_resource")
    query = f"SELECT {','.join(fields)} FROM {arguments.get('resource', '')}"  # noqa: S608 - validate_gaql below
    for key, clause, separator in (
        ("conditions", " WHERE ", " AND "),
        ("orderings", " ORDER BY ", ","),
    ):
        values = arguments.get(key, [])
        if not isinstance(values, list) or any(not isinstance(v, str) for v in values):
            raise NativeMcpDeniedError("native_mcp_invalid_query")
        if values:
            query += clause + separator.join(values)
    validate_gaql(query + f" LIMIT {arguments['limit']}")


def _meta_arguments(arguments: dict[str, Any]) -> None:
    for key in ("fields", "breakdowns"):
        values = arguments.get(key, [])
        if not isinstance(values, list) or any(
            not isinstance(v, str) or not re.fullmatch(r"[a-z_]+", v) for v in values
        ):
            raise NativeMcpDeniedError("native_mcp_invalid_fields")
    window = arguments.get("time_range")
    if window is not None:
        if not isinstance(window, dict) or set(window) != {"since", "until"}:
            raise NativeMcpDeniedError("native_mcp_invalid_window")
        start, stop = date.fromisoformat(window["since"]), date.fromisoformat(window["until"])
        if start > stop or (stop - start).days > _MAX_WINDOW_DAYS:
            raise NativeMcpDeniedError("native_mcp_invalid_window")

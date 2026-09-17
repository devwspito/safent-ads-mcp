"""Re-reads signed conversion actions by GAQL, before any Google Ads
`mutate` call for campaign creation (threat-model.md S-1/S-2/S-3;
tasks.md T032). The model already saw these resource names via
`list_google_conversion_actions`, but nothing binds that earlier read to
what it signs afterwards -- this module is that binding, and the last
guard before the broker spends money optimizing against them.

`customer_id` always arrives as an explicit parameter derived from the
write's destination (`platform_account_id_from_google_resource_name`, or
`intent.entity_ref.external_id` -- see `broker/domain/write_authorization.py`),
**never** parsed back out of the resource names being verified: comparing
a value against a copy of itself would make S-1 a tautology, the same
mistake the campaign-package design (sec. 0.1) documents for `diff_hash`.

One stable error, one stable reason, for the three cases the threat model
groups together (S-3): a conversion action that does not exist, one that
is paused, and one that belongs to another account all raise the same
`ConversionGoalVerificationError` -- never the resource name in the
message, never a `reason` that would let a caller tell the three apart."""

from __future__ import annotations

from collections.abc import Iterator, Mapping, Sequence
from dataclasses import dataclass
from typing import Any, Protocol

from safent_ads.proposals.domain.conversion_goal import ConversionGoal, ConversionGoalError

_MIN_CONVERSION_GOALS = 1
_MAX_CONVERSION_GOALS = 10
_ENABLED_STATUS = "ENABLED"


class ConversionGoalVerificationError(ValueError):
    """A single stable code -- never a resource name, never which of the
    three cases (inexistent/paused/foreign) it was."""

    def __init__(self) -> None:
        super().__init__("campaign_creation_conversion_goal_unverified")


@dataclass(frozen=True, slots=True)
class VerifiedConversionGoal:
    """A conversion action confirmed, by a live GAQL read, to belong to
    the destination customer and to be `ENABLED` -- plus the `category`/
    `origin` pair the broker needs to opt the campaign into it via
    `CampaignConversionGoal` (that resource is keyed by category+origin,
    not by an individual conversion action)."""

    resource_name: str
    category: str
    origin: str


class GaqlSearchStream(Protocol):
    """Same shape as `LiveGoogleAdsSearchClient.search_stream`."""

    def search_stream(self, customer_id: str, query: str) -> Iterator[Mapping[str, Any]]: ...


def verify_conversion_goals(
    search_client: GaqlSearchStream,
    customer_id: str,
    resource_names: Sequence[str],
) -> tuple[VerifiedConversionGoal, ...]:
    """Raises `ConversionGoalVerificationError` unless every name is well
    formed, prefixed by `customer_id` (the destination -- never a value
    read back out of the names themselves), `ENABLED`, and the set has
    1..10 entries without duplicates. Checked in that order, before the
    caller may build a single `MutateOperation` (S-1): a denial here
    means zero calls to `GoogleAdsService.mutate`."""
    goals = _require_shape(resource_names)
    _require_owned_prefix(goals, customer_id)
    rows = _read_conversion_actions(search_client, customer_id, resource_names)
    return _require_all_enabled(goals, rows)


def _require_shape(resource_names: Sequence[str]) -> tuple[ConversionGoal, ...]:
    if not (_MIN_CONVERSION_GOALS <= len(resource_names) <= _MAX_CONVERSION_GOALS):
        raise ConversionGoalVerificationError
    if len(set(resource_names)) != len(resource_names):
        raise ConversionGoalVerificationError
    try:
        return tuple(ConversionGoal(name) for name in resource_names)
    except ConversionGoalError as error:
        raise ConversionGoalVerificationError from error


def _require_owned_prefix(goals: Sequence[ConversionGoal], customer_id: str) -> None:
    if any(goal.customer_id != customer_id for goal in goals):
        raise ConversionGoalVerificationError


def _read_conversion_actions(
    search_client: GaqlSearchStream, customer_id: str, resource_names: Sequence[str]
) -> Mapping[str, Mapping[str, Any]]:
    # `resource_names` already passed `ConversionGoal`'s fullmatch regex
    # (digits and the fixed `customers/.../conversionActions/...` literal
    # only) in `_require_shape`, so interpolating them into the GAQL
    # string literal here cannot escape the literal (GAQL has no bind
    # parameters, same defense already applied in `google_ads_adapter.py`).
    quoted = ", ".join(f"'{name}'" for name in resource_names)
    query = (
        "SELECT conversion_action.resource_name, conversion_action.status, "  # noqa: S608
        "conversion_action.category, conversion_action.origin "
        f"FROM conversion_action WHERE conversion_action.resource_name IN ({quoted})"
    )
    return {
        str(row["conversion_action.resource_name"]): row
        for row in search_client.search_stream(customer_id, query)
    }


def _require_all_enabled(
    goals: Sequence[ConversionGoal], rows: Mapping[str, Mapping[str, Any]]
) -> tuple[VerifiedConversionGoal, ...]:
    verified: list[VerifiedConversionGoal] = []
    for goal in goals:
        row = rows.get(goal.resource_name)
        if row is None or row.get("conversion_action.status") != _ENABLED_STATUS:
            raise ConversionGoalVerificationError
        verified.append(
            VerifiedConversionGoal(
                resource_name=goal.resource_name,
                category=str(row["conversion_action.category"]),
                origin=str(row["conversion_action.origin"]),
            )
        )
    return tuple(verified)

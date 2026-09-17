"""One durable receipt around one native create call; never retry an uncertain POST."""

import asyncio
import re
from collections.abc import Callable, Mapping, Sequence
from datetime import datetime
from typing import Any, cast

from safent_ads.accounts.application.ports import (
    IdempotencyKey,
    SignedAuthorization,
    WriteIntent,
    WriteOutcome,
)
from safent_ads.accounts.domain.platform_state_hash import PlatformStateHash
from safent_ads.broker.platforms.google_conversion_goal_reader import (
    ConversionGoalVerificationError,
)
from safent_ads.broker.platforms.write_pipeline import WriteAuthorizationPipeline
from safent_ads.proposals.domain.campaign_creation import CampaignCreationError, creation_budget


def _signed_conversion_goal_resource_names(intent: WriteIntent) -> Sequence[str]:
    """Only Google native plans ever carry `conversion_goals` (data-model.md
    §`GoogleCampaignNative`); a Meta plan, or a Google plan whose channel
    does not require them, has no such key -- returning an empty sequence
    there is a no-op for the caller, never a platform `if`."""
    plan = intent.valor_propuesto
    if not isinstance(plan, Mapping):
        return ()
    creation_plan = plan.get("creation_plan")
    if not isinstance(creation_plan, Mapping):
        return ()
    native = creation_plan.get("native")
    if not isinstance(native, Mapping):
        return ()
    goals = native.get("conversion_goals")
    if not isinstance(goals, list):
        return ()
    return tuple(
        goal["resource_name"]
        for goal in goals
        if isinstance(goal, Mapping) and isinstance(goal.get("resource_name"), str)
    )


def _operation_count(intent: WriteIntent, resource_names: Sequence[str]) -> int:
    """T035 finding 2 (threat-model.md D-2/AL-5): the real SDK operation
    count for the single mutate/call this function is about to attempt --
    never the implicit `1` `try_consume()` defaults to. Google batches
    `campaign_budget` + `campaign` in ONE `GoogleAdsService.mutate`
    (`live_google_ads_client.py::create_paused_campaign`), plus one
    `campaign_conversion_goal` operation per already-verified goal; Meta's
    Graph API call creates a single native object."""
    if intent.entity_ref.platform.value != "google":
        return 1
    return 2 + len(resource_names)


async def create_paused_campaign(  # noqa: PLR0911 - ordered fail-closed gates before one write
    *,
    pipeline: WriteAuthorizationPipeline,
    intent: WriteIntent,
    authorization: SignedAuthorization,
    idempotency_key: IdempotencyKey,
    now: datetime,
    currency: Callable[[str], str],
    create: Callable[[str, Mapping[str, Any]], Mapping[str, Any]],
    consume_rate: Callable[[int], bool],
    verify_conversion_goals: Callable[[str, Sequence[str]], object] | None = None,
) -> WriteOutcome:
    try:
        creation_budget(intent.valor_propuesto, intent.entity_ref)
    except (CampaignCreationError, TypeError) as exc:
        return WriteOutcome(
            "DENIED",
            None,
            None,
            str(exc)
            if isinstance(exc, CampaignCreationError)
            else "campaign_creation_plan_invalid",
            None,
        )
    account = intent.entity_ref.external_id
    pattern = r"[0-9]+" if intent.entity_ref.platform.value == "google" else r"act_[0-9]+"
    if not re.fullmatch(pattern, account):
        return WriteOutcome("DENIED", None, None, "campaign_creation_account_invalid", None)
    gate = pipeline.authorize(
        intent, authorization, platform_account_id=account, remote_state_hash="", now=now
    )
    if gate is not None:
        return gate
    try:
        if await asyncio.to_thread(currency, account) != "EUR":
            return WriteOutcome("DENIED", None, None, "campaign_creation_currency_mismatch", None)
    except Exception:  # noqa: BLE001 - read only, no provider body crosses boundary
        return WriteOutcome("DENIED", None, None, "campaign_creation_currency_unverified", None)
    # S-1/S-2/S-3 (threat-model.md, T032):
    # re-verify signed conversion goals BEFORE reserving the idempotency key
    # -- a denial here must be clean (never an `UNKNOWN` receipt needing
    # reconciliation) and the double of the SDK must see zero `mutate` calls.
    resource_names = _signed_conversion_goal_resource_names(intent)
    if resource_names and verify_conversion_goals is not None:
        try:
            await asyncio.to_thread(verify_conversion_goals, account, resource_names)
        except ConversionGoalVerificationError as exc:
            return WriteOutcome("DENIED", None, None, str(exc), None)
    if not consume_rate(_operation_count(intent, resource_names)):
        return WriteOutcome("DENIED", None, None, "rate_limited", None)
    key = str(idempotency_key)
    replay = await pipeline.begin_admitted_write(key, intent, authorization, account, now)
    if replay is not None:
        return replay
    try:
        # SDK implementation checks native acknowledgement fields, including
        # PAUSED/budget, before returning. No automatic retry here or in SDK.
        confirmed = await asyncio.to_thread(
            create, account, cast(Mapping[str, Any], intent.valor_propuesto)
        )
        resource = confirmed.get("campaign_resource")
        if not isinstance(resource, str) or not resource:
            raise ValueError("campaign_creation_confirmation_missing")
        outcome = WriteOutcome(
            "SUCCEEDED",
            intent.valor_propuesto,
            PlatformStateHash.compute(confirmed).value,
            None,
            resource,
        )
    except Exception:  # noqa: BLE001 - even a partial native result retains protection
        outcome = WriteOutcome("UNKNOWN", None, None, "campaign_creation_outcome_unknown", None)
    return pipeline.finalize(key, account, intent, outcome, now=now)

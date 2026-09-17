"""One admitted receipt for one paused child creation. Unknown is never replayed."""

import asyncio
from collections.abc import Callable, Mapping
from datetime import datetime
from typing import Any

from safent_ads.accounts.application.ports import SignedAuthorization, WriteIntent, WriteOutcome
from safent_ads.accounts.domain.platform_state_hash import PlatformStateHash
from safent_ads.broker.platforms.write_pipeline import WriteAuthorizationPipeline
from safent_ads.proposals.domain.ad_child_creation import validate_child_diff

_ASSET_GROUP_KIND = "ASSET_GROUP"
# Logo/marketing/square image: already-uploaded assets (`upload_asset`)
# referenced by id, never created here -- only linked.
_ASSET_GROUP_IMAGE_LINK_COUNT = 3


def _google_asset_group_operation_count(assets: Mapping[str, Any]) -> int:
    """T035 finding 2 (threat-model.md D-2/AL-5): a Performance Max asset
    group is one `AssetGroupService` mutate with far more than one
    operation -- each new text field (headline/long_headline/description/
    business_name) is a CREATE **and** a LINK, plus one CREATE for the
    asset group itself. `assets` already passed `validate_child_diff`
    (proposals.domain.ad_child_creation._validate_google_asset_group_native),
    so its key set is guaranteed."""
    text_fields = (
        len(assets["headlines"])
        + len(assets["long_headlines"])
        + len(assets["descriptions"])
        + 1  # business_name
    )
    return 1 + (text_fields * 2) + _ASSET_GROUP_IMAGE_LINK_COUNT


def _operation_count(plan: Mapping[str, Any]) -> int:
    """Real SDK operation count for the single `create` call about to be
    attempted -- never the implicit `1` `try_consume()` defaults to. A
    Search/Display/Demand Gen ad group, or any Meta child, is one native
    object; a Google asset group (Performance Max) is not."""
    native = plan.get("native")
    if plan.get("platform") == "google" and isinstance(native, Mapping):
        if native.get("kind") == _ASSET_GROUP_KIND:
            assets = native["assets"]
            return _google_asset_group_operation_count(assets)
    return 1


async def create_paused_child(
    *,
    pipeline: WriteAuthorizationPipeline,
    intent: WriteIntent,
    authorization: SignedAuthorization,
    key: str,
    account: str,
    now: datetime,
    prepare: Callable[[str, Mapping[str, Any]], None],
    create: Callable[[str, Mapping[str, Any]], Mapping[str, Any]],
    consume_rate: Callable[[int], bool],
) -> WriteOutcome:
    # Adapter already verified signature, exact scoped parent and current state.
    # Preflight can only read, never reserve or perform a mutation.
    try:
        plan = validate_child_diff(
            intent.parametro,
            intent.valor_actual,
            intent.valor_propuesto,
            intent.entity_ref,
            intent.expected_state_hash,
        )
        await asyncio.to_thread(prepare, intent.entity_ref.external_id, plan)
    except Exception:  # noqa: BLE001 - sanitized, no effect attempted
        return WriteOutcome("DENIED", None, None, "ad_child_precondition_unverified", None)
    if not consume_rate(_operation_count(plan)):
        return WriteOutcome("DENIED", None, None, "rate_limited", None)
    replay = await pipeline.begin_admitted_write(key, intent, authorization, account, now)
    if replay is not None:
        return replay
    try:
        confirmed = await asyncio.to_thread(create, intent.entity_ref.external_id, plan)
        resource = confirmed.get("child_resource")
        if not isinstance(resource, str) or not resource or confirmed.get("status") != "PAUSED":
            raise ValueError("ad_child_confirmation_missing")
        outcome = WriteOutcome(
            "SUCCEEDED",
            intent.valor_propuesto,
            PlatformStateHash.compute(confirmed).value,
            None,
            resource,
        )
    except Exception:  # noqa: BLE001 - includes partial mutation / lost acknowledgement
        outcome = WriteOutcome("UNKNOWN", None, None, "ad_child_outcome_unknown", None)
    return pipeline.finalize(key, account, intent, outcome, now=now)

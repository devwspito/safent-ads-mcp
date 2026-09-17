"""Fresh panel brake state, including physical-account sibling connections."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import Any

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

_ACCOUNTS = text("""
    SELECT id, business_id, platform, external_account_id, account_ref
      FROM platform_accounts
     WHERE business_id = :business_id
     ORDER BY platform, external_account_id, id
""")
_BRAKES = text("""
    SELECT brake.id, brake.scope_kind, brake.business_id,
           brake.platform_account_id, brake.mode, brake.reason,
           brake.engaged_at, brake.engaged_by,
           business.name AS business_name,
           account.business_id AS account_business_id, account.platform,
           account.external_account_id, account.account_ref
      FROM emergency_brakes brake
      LEFT JOIN businesses business ON business.id = brake.business_id
      LEFT JOIN platform_accounts account ON account.id = brake.platform_account_id
     WHERE brake.released_at IS NULL
       AND (brake.scope_kind = 'global'
            OR (brake.scope_kind = 'business' AND brake.business_id = :business_id)
            OR (brake.scope_kind = 'platform_account' AND account.business_id = :business_id))
     ORDER BY brake.engaged_at DESC, brake.id
""")


async def read_kill_switch_view(session: AsyncSession, business_id: str) -> dict[str, Any]:
    """Caller must authorize/existence-check business_id before this read."""
    params = {"business_id": business_id}
    accounts = (await session.execute(_ACCOUNTS, params)).mappings().all()
    brakes = (await session.execute(_BRAKES, params)).mappings().all()
    return serialize_kill_switch_view(
        [dict(row) for row in accounts], [dict(row) for row in brakes]
    )


def _item(brake: Mapping[str, Any]) -> dict[str, Any]:
    kind = str(brake["scope_kind"])
    if kind == "global":
        ref, label = None, "Todo el panel"
    elif kind == "business":
        ref, label = str(brake["business_id"]), str(brake["business_name"])
    else:
        ref = str(brake["account_ref"])
        label = f"{brake['platform']} · {brake['external_account_id']}"
    return {
        "brake_id": str(brake["id"]),
        "scope_kind": kind,
        "scope_id": ref,
        "scope_label": label,
        "mode": str(brake["mode"]).upper(),
        "engaged_at": brake["engaged_at"].isoformat(),
        "engaged_by": str(brake["engaged_by"]),
        "reason": brake["reason"],
    }


def _effective(items: Sequence[dict[str, Any]]) -> dict[str, Any]:
    if not items:
        return {
            "engaged": False,
            "mode": None,
            "scope_kind": None,
            "scope_id": None,
            "engaged_at": None,
            "reason": None,
        }
    # ALL always wins over AUTONOMOUS, even if the latter is global. Stable
    # ties prefer broadest scope, then newest event and its actual stored ID.
    rank = {"global": 2, "business": 1, "platform_account": 0}
    winner = max(
        items,
        key=lambda item: (
            item["mode"] == "ALL",
            rank[item["scope_kind"]],
            item["engaged_at"],
            item["brake_id"],
        ),
    )
    return {
        "engaged": True,
        **{
            key: winner[key]
            for key in (
                "mode",
                "scope_kind",
                "scope_id",
                "engaged_at",
                "reason",
            )
        },
    }


def _applies(brake: Mapping[str, Any], account: Mapping[str, Any]) -> bool:
    if brake["scope_kind"] == "global":
        return True
    if brake["scope_kind"] == "business":
        return bool(brake["business_id"] == account["business_id"])
    # Match safety identity, not credential ID or ACTIVE status. A brake on a
    # retired route still governs its sibling route to the same real account.
    return (brake["account_business_id"], brake["platform"], brake["external_account_id"]) == (
        account["business_id"],
        account["platform"],
        account["external_account_id"],
    )


def serialize_kill_switch_view(
    accounts: Sequence[Mapping[str, Any]],
    brakes: Sequence[Mapping[str, Any]],
) -> dict[str, Any]:
    items = [_item(brake) for brake in brakes]
    by_account = []
    for account in accounts:
        effective = _effective(
            [item for brake, item in zip(brakes, items, strict=True) if _applies(brake, account)]
        )
        by_account.append(
            {
                "platform_account_id": str(account["id"]),
                "engaged": effective["engaged"],
                "mode": effective["mode"],
                "source_scope_kind": effective["scope_kind"],
            }
        )
    return {"items": items, "effective": _effective(items), "by_account": by_account}

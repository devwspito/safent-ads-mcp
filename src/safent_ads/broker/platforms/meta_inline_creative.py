"""Bounded, account-authorized Page preflight and exact inline creative readback.

Creation itself stays in the one admitted POST /act_ID/ads. There is no upload,
URL fetch, free proxy or separately replayable creative mutation here.
"""

import re
from collections.abc import Mapping
from typing import Any

_MAX_PAGE_BATCHES = 10
_PAGE_LIMIT = 100
_MAX_CURSOR_LENGTH = 2048


def verify_inline_page(client: Any, account: str, creative: Mapping[str, Any]) -> None:
    page_id = creative["object_story_spec"]["page_id"]
    api = client._api_for(account)
    params: dict[str, Any] = {"fields": "id", "limit": _PAGE_LIMIT}
    seen: set[str] = set()
    for _ in range(_MAX_PAGE_BATCHES):
        body = api.call("GET", [account, "promote_pages"], params=params).json()
        rows = body.get("data")
        if not isinstance(rows, list) or len(rows) > _PAGE_LIMIT:
            raise ValueError("ad_child_page_unverified")
        if any(not isinstance(row, dict) or not isinstance(row.get("id"), str) for row in rows):
            raise ValueError("ad_child_page_unverified")
        if any(row["id"] == page_id for row in rows):
            return
        paging = body.get("paging", {})
        if not isinstance(paging, dict) or not paging.get("next"):
            break
        cursor = paging.get("cursors", {}).get("after")
        if (
            not isinstance(cursor, str)
            or not cursor
            or len(cursor) > _MAX_CURSOR_LENGTH
            or cursor in seen
        ):
            break
        seen.add(cursor)
        # Use only the opaque cursor on the same account edge, never paging.next.
        params = {**params, "after": cursor}
    raise ValueError("ad_child_page_unverified")


def verify_inline_readback(
    client: Any, api: Any, account: str, actual: object, creative: Mapping[str, Any]
) -> None:
    if not isinstance(actual, dict) or not isinstance(actual.get("id"), str):
        raise ValueError("ad_child_confirmation_mismatch")
    creative_id = actual["id"]
    if not re.fullmatch(r"[0-9]+", creative_id):
        raise ValueError("ad_child_confirmation_mismatch")
    body = api.call(
        "GET", [creative_id], params={"fields": "id,account_id,object_story_spec"}
    ).json()
    client._check_owner(account, body)
    if (
        body.get("id") != creative_id
        or body.get("object_story_spec") != creative["object_story_spec"]
    ):
        # Masked/missing/re-written content cannot prove the signed creative.
        # The caller turns this into UNKNOWN and never retries the POST.
        raise ValueError("ad_child_confirmation_mismatch")

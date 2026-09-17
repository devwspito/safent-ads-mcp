"""propose_campaign_draft accepts what the model actually types for the account.

2026-09-14: the model passed the bare Google customer id (`1000000001`), then
`platform: "GOOGLE_SEARCH"` and a top-level `title`; every attempt was rejected
by the strict schema and the model wandered off creating skills. The closed
schema stays closed — only these obvious shapes are resolved.
"""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from safent_ads.mcp.application.dto import AccountStatus, PlatformAccountSummary, PlatformCode
from safent_ads.mcp.application.errors import ToolValidationError
from safent_ads.mcp.presentation.campaign_draft_tools import (
    DraftSaveArgs,
    build_draft_tools,
    resolve_account_ref,
)

BUSINESS = "0609e9cf-e861-4c9b-94cf-4611e527fc69"
CONNECTION = "7c1f3a2e-6b1d-4c1e-9a0f-2d3e4f5a6b7c"
GOOGLE_REF = f"google:account:{BUSINESS}:{CONNECTION}:1000000001"
META_REF = f"meta:account:{BUSINESS}:{CONNECTION}:100000000000002"


def _summary(ref: str, platform: PlatformCode) -> PlatformAccountSummary:
    return PlatformAccountSummary(
        ref, platform, "EUR", "Europe/Madrid", AccountStatus.ACTIVE, "standard"
    )


class _Accounts:
    def __init__(self, *refs: tuple[str, PlatformCode]) -> None:
        self._items = [_summary(ref, platform) for ref, platform in refs]

    async def list_platform_accounts(self, business_id: str) -> list[PlatformAccountSummary]:
        assert business_id == BUSINESS
        return list(self._items)


class _Store:
    def __init__(self) -> None:
        self.saved: list[tuple] = []

    async def save(self, business_id, draft_key, expected_revision, changes):
        self.saved.append((business_id, draft_key, expected_revision, changes))
        return {"draft_id": "d1", "revision": 1}


ACCOUNTS = _Accounts((GOOGLE_REF, PlatformCode.GOOGLE), (META_REF, PlatformCode.META))


@pytest.mark.parametrize("raw", ["1000000001", "100-000-0001", "google:1000000001", GOOGLE_REF])
async def test_bare_dashed_prefixed_and_canonical_ids_resolve_to_the_canonical_ref(
    raw: str,
) -> None:
    assert await resolve_account_ref(ACCOUNTS, BUSINESS, raw, None) == GOOGLE_REF


async def test_platform_narrows_the_candidates() -> None:
    assert await resolve_account_ref(ACCOUNTS, BUSINESS, "100000000000002", "meta") == META_REF


async def test_unknown_id_lists_the_connected_accounts() -> None:
    with pytest.raises(ToolValidationError, match="ACCOUNT_REF_UNKNOWN") as info:
        await resolve_account_ref(ACCOUNTS, BUSINESS, "999", None)
    assert GOOGLE_REF in str(info.value) and META_REF in str(info.value)


async def test_ambiguous_id_is_refused_with_the_candidates() -> None:
    twice = _Accounts(
        (GOOGLE_REF, PlatformCode.GOOGLE),
        (GOOGLE_REF.replace(CONNECTION, CONNECTION[:-1] + "d"), PlatformCode.GOOGLE),
    )
    with pytest.raises(ToolValidationError, match="ACCOUNT_REF_AMBIGUOUS"):
        await resolve_account_ref(twice, BUSINESS, "1000000001", None)


def test_platform_alias_and_top_level_title_are_normalised_but_the_schema_stays_closed() -> None:
    args = DraftSaveArgs.model_validate(
        {
            "business_id": BUSINESS,
            "draft_key": "acme-google-consulta-2026-09",
            "title": "Consulta veterinaria",
            "changes": {"platform": "GOOGLE_SEARCH", "account_ref": "1000000001"},
        }
    )
    assert args.changes.platform == "google"
    assert args.changes.title == "Consulta veterinaria"
    with pytest.raises(ValidationError):
        DraftSaveArgs.model_validate(
            {
                "business_id": BUSINESS,
                "draft_key": "k",
                "changes": {"platform": "google", "campaign_name": "x"},
            }
        )


async def test_save_stores_the_canonical_reference() -> None:
    store = _Store()
    tools = {tool.name: tool for tool in build_draft_tools(store, accounts=ACCOUNTS)}  # type: ignore[arg-type]
    args = DraftSaveArgs.model_validate(
        {
            "business_id": BUSINESS,
            "draft_key": "acme-google-consulta-2026-09",
            "changes": {"title": "Consulta", "platform": "google", "account_ref": "1000000001"},
        }
    )
    await tools["propose_campaign_draft"].handler(args, None)
    assert store.saved[0][3].account_ref == GOOGLE_REF


async def test_save_without_an_account_port_still_requires_a_canonical_ref() -> None:
    store = _Store()
    tools = {tool.name: tool for tool in build_draft_tools(store)}  # type: ignore[arg-type]
    args = DraftSaveArgs.model_validate(
        {
            "business_id": BUSINESS,
            "draft_key": "k",
            "changes": {"title": "t", "account_ref": "1000000001"},
        }
    )
    with pytest.raises(ToolValidationError, match="CAMPAIGN_DRAFT_INVALID"):
        await tools["propose_campaign_draft"].handler(args, None)
    assert store.saved == []

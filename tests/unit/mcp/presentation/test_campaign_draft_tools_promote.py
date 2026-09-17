"""Bug 2 (hotfix 0.2.20): `propose_campaign_from_draft` (the `promote`
handler in `campaign_draft_tools.py`) never threaded `caller_scope` into
`CampaignDraftStore.promote`, unlike `write_handlers.py`'s
`propose_budget_change`/`propose_pause` -- `proposals.proposed_by` was
always `NULL` for a campaign-package proposal, regardless of who proposed
it. Fake `CampaignDraftStore.promote` records what it received; no
Postgres needed to pin the threading itself (the SQL side is covered by
`tests/e2e/journeys/test_journey_campaign.py::test_promoted_draft_appears_
in_the_panel_read_api`)."""

from __future__ import annotations

from typing import Any

from safent_ads.mcp.application.caller_scope import CallerScope, Permission
from safent_ads.mcp.presentation.campaign_draft_tools import DraftPromoteArgs, build_draft_tools

BUSINESS = "0609e9cf-e861-4c9b-94cf-4611e527fc69"
DRAFT_ID = "9c1f3a2e-6b1d-4c1e-9a0f-2d3e4f5a6b7c"


class _Store:
    def __init__(self) -> None:
        self.promoted: list[tuple[str, str, int, str | None]] = []

    async def promote(
        self, business_id: str, draft_id: str, revision: int, *, proposed_by: str | None = None
    ) -> dict[str, Any]:
        self.promoted.append((business_id, draft_id, revision, proposed_by))
        return {"draft_id": draft_id, "revision": revision}


def _args() -> DraftPromoteArgs:
    return DraftPromoteArgs.model_validate(
        {"business_id": BUSINESS, "draft_id": DRAFT_ID, "expected_revision": 1}
    )


async def test_promote_threads_the_person_callers_id_as_proposed_by() -> None:
    store = _Store()
    tools = {tool.name: tool for tool in build_draft_tools(store)}  # type: ignore[arg-type]
    caller_scope = CallerScope("person:ana", frozenset({BUSINESS}), Permission.PROPOSE, "Ana")

    await tools["propose_campaign_from_draft"].handler(_args(), caller_scope)

    assert store.promoted == [(BUSINESS, DRAFT_ID, 1, "person:ana")]


async def test_promote_leaves_proposed_by_none_for_a_non_person_caller() -> None:
    store = _Store()
    tools = {tool.name: tool for tool in build_draft_tools(store)}  # type: ignore[arg-type]
    caller_scope = CallerScope("rule_engine", frozenset({BUSINESS}), Permission.PROPOSE, "Motor")

    await tools["propose_campaign_from_draft"].handler(_args(), caller_scope)

    assert store.promoted == [(BUSINESS, DRAFT_ID, 1, None)]

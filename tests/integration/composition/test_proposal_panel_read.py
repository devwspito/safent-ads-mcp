"""The actual SQL read model must match the same fixture parsed by the panel."""

from __future__ import annotations

import json
from pathlib import Path
from uuid import uuid4

import pytest
from sqlalchemy.ext.asyncio import AsyncSession
from tests.contracts.sql_fixtures import campaign_ref, seed_entity
from tests.unit.proposals.conftest import budget_diff, make_proposal

from safent_ads.iam.presentation.errors import ApiError
from safent_ads.proposals.domain.identifiers import ProposalId
from safent_ads.proposals.domain.money import Money
from safent_ads.proposals.infrastructure.sql_proposal_repository import SqlProposalRepository
from safent_ads.proposals.presentation.panel_read import inbox_page, proposal_detail
from safent_ads.shared.ids import BusinessId

pytestmark = pytest.mark.integration
CONTRACT = Path(__file__).parents[2] / "contracts" / "proposal-panel.json"


async def test_persisted_inbox_and_detail_match_typescript_contract(
    db_session: AsyncSession,
) -> None:
    ref = campaign_ref("panel-contract")
    business = await seed_entity(db_session, ref)
    proposal = make_proposal(diff=budget_diff(ref=ref))
    proposal.business_id = BusinessId(business)
    proposal.proposal_id = ProposalId.parse("10000000-0000-4000-8000-000000000001")
    await SqlProposalRepository(db_session).save(proposal)
    expected = json.loads(CONTRACT.read_text())
    inbox = await inbox_page(
        db_session, str(business), lens="urgency", state="pending", limit=50, offset=0
    )
    detail = await proposal_detail(db_session, str(business), str(proposal.proposal_id))
    assert inbox == expected["inbox"]
    assert detail == expected["detail"]
    # A read does not approve, schedule, or generate a new hash.
    reloaded = await SqlProposalRepository(db_session).get(proposal.proposal_id)
    assert reloaded is not None
    assert reloaded.state.value == "pending"
    assert reloaded.diff.diff_hash == proposal.diff.diff_hash


async def test_pagination_and_currency_totals_do_not_invent_or_mix_values(
    db_session: AsyncSession,
) -> None:
    ref = campaign_ref("panel-pagination")
    business = await seed_entity(db_session, ref)
    proposals = []
    for index, currency in enumerate(("EUR", "USD")):
        proposal = make_proposal(diff=budget_diff(ref=ref))
        proposal.business_id = BusinessId(business)
        proposal.diff = type(proposal.diff).build(ref, f"budget_{index}", 100, 70)
        proposal.estimated_impact = Money.of("310", currency)
        await SqlProposalRepository(db_session).save(proposal)
        proposals.append(proposal)
    first = await inbox_page(
        db_session, str(business), lens="urgency", state="pending", limit=1, offset=0
    )
    second = await inbox_page(
        db_session, str(business), lens="urgency", state="pending", limit=1, offset=1
    )
    assert first["next_cursor"] == "1"
    assert second["next_cursor"] is None
    assert first["pending_count"] == second["pending_count"] == 2
    assert first["total_impact"] is None
    actual_ids = {page["groups"][0]["proposals"][0]["proposal_id"] for page in (first, second)}
    assert actual_ids == {str(proposal.proposal_id) for proposal in proposals}
    all_items = await inbox_page(
        db_session, str(business), lens="calendar_event", state="pending", limit=50, offset=0
    )
    assert len(all_items["groups"]) == 2
    assert {group["total_impact"]["currency"] for group in all_items["groups"]} == {"EUR", "USD"}
    assert all(not group["batch_eligible"] for group in all_items["groups"])


async def test_detail_is_scoped_to_business_and_invalid_id_is_not_500(
    db_session: AsyncSession,
) -> None:
    ref = campaign_ref("panel-scope")
    business = await seed_entity(db_session, ref)
    proposal = make_proposal(diff=budget_diff(ref=ref))
    proposal.business_id = BusinessId(business)
    await SqlProposalRepository(db_session).save(proposal)
    for business_id, proposal_id in (
        (str(uuid4()), str(proposal.proposal_id)),
        (str(business), "bad-id"),
    ):
        with pytest.raises(ApiError) as denied:
            await proposal_detail(db_session, business_id, proposal_id)
        assert denied.value.status_code == 404

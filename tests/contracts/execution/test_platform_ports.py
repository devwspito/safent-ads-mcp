"""Contrato de `PlatformReaderPort` y `AdsPlatformWritePort`: identico para
los dobles en memoria y para los adaptadores sobre el broker."""

from __future__ import annotations

from datetime import timedelta

import pytest

from safent_ads.execution.application.ports import WriteCommand
from safent_ads.execution.infrastructure.errors import PlatformWriteDeniedError
from safent_ads.proposals.domain.authorization import (
    Authorization,
    AuthorizationChannel,
    AuthorizationKind,
    sign_authorization,
)
from safent_ads.proposals.domain.cause import Cause, CauseKey, Evidence
from safent_ads.proposals.domain.classification import Classification
from safent_ads.proposals.domain.identifiers import AuthorizationId
from safent_ads.proposals.domain.money import Money
from safent_ads.proposals.domain.priority import Priority, Urgency
from safent_ads.proposals.domain.proposal import Proposal, ProposedDiff, new_proposal_id
from safent_ads.proposals.testing.fakes import FakeProposalRepository, FakeSignerPort
from safent_ads.shared.ids import BusinessId, EntityRef
from tests.contracts.execution.conftest import (
    NOW,
    PlatformFixture,
    campaign_ref,
    digest,
    remote_state_hash,
)

CONFIRMED_STATE_HASH = digest("estado-remoto-tras-aplicar")


def budget_proposal(entity_ref: EntityRef) -> Proposal:
    diff = ProposedDiff.build(
        entity_ref=entity_ref,
        parameter="daily_budget",
        before=Money.of("100"),
        after=Money.of("70"),
    )
    proposal = Proposal.raise_proposal(
        proposal_id=new_proposal_id(),
        business_id=BusinessId.new(),
        diff=diff,
        classification=Classification.ROUTINE,
        cause=Cause(text="ROAS bajo objetivo en 7D", rule_id="M05"),
        cause_key=CauseKey(entity_ref=entity_ref, rule_id="M05", cause_type="roas_low"),
        evidence=(Evidence(metric="roas", actual=1.4, target=2.0, window_preset="7D"),),
        estimated_impact=Money.of("310"),
        priority=Priority(urgency=Urgency.RECOMMENDED),
        now=NOW,
        expires_at=NOW + timedelta(hours=24),
        expected_state_hash=digest("estado-remoto-antes"),
    )
    proposal.pull_events()
    return proposal


def authorization_for(proposal: Proposal) -> Authorization:
    return sign_authorization(
        authorization_id=AuthorizationId.new(),
        proposal_id=proposal.proposal_id,
        kind=AuthorizationKind.HUMAN_APPROVAL,
        proposal_classification=proposal.classification,
        diff_hash=proposal.diff.diff_hash,
        guardrail_verdict_hash=digest("veredicto"),
        issued_by="owner-de-contrato",
        channel=AuthorizationChannel.PANEL,
        decided_at=NOW,
        expires_at=NOW + timedelta(hours=1),
        signer=FakeSignerPort(),
    )


async def test_reader_returns_the_state_hash_of_the_entity(platform: PlatformFixture) -> None:
    entity_ref = campaign_ref("c-lectura")
    expected = remote_state_hash(entity_ref)

    reader = platform.reader_reporting(entity_ref, expected)

    assert await reader.fetch_state_hash(entity_ref) == expected


async def test_an_unreachable_platform_raises_instead_of_answering(
    platform: PlatformFixture,
) -> None:
    """El revalidador trata un fallo de lectura como deriva; para eso el
    puerto tiene que fallar, no devolver un hash inventado."""
    reader = platform.unreachable_reader()

    with pytest.raises(platform.failure_error):
        await reader.fetch_state_hash(campaign_ref("c-inalcanzable"))


async def test_a_confirmed_write_returns_the_state_after(platform: PlatformFixture) -> None:
    proposal = budget_proposal(campaign_ref("c-escritura"))
    proposals = FakeProposalRepository()
    await proposals.save(proposal)
    writer = platform.writer_confirming(proposals, CONFIRMED_STATE_HASH)
    authorization = authorization_for(proposal)

    result = await writer.execute_write(
        WriteCommand(
            entity_ref=proposal.diff.entity_ref,
            parameter=proposal.diff.parameter,
            before=proposal.diff.before,
            value=proposal.diff.after,
        ),
        authorization,
        f"exec-{proposal.proposal_id}-{proposal.diff.diff_hash[:12]}",
    )

    assert result.confirmed_state_hash == CONFIRMED_STATE_HASH


async def test_a_denied_write_is_never_a_success(platform: PlatformFixture) -> None:
    """Denegar por defecto (C-1): el veredicto del broker sale como error
    tipado, jamas como un `WriteResult` a medias. Hoy TODA escritura se
    deniega, asi que este es el camino que corre en produccion."""
    proposal = budget_proposal(campaign_ref("c-denegada"))
    proposals = FakeProposalRepository()
    await proposals.save(proposal)
    writer = platform.writer_denying(proposals)

    with pytest.raises(PlatformWriteDeniedError):
        await writer.execute_write(
            WriteCommand(
                entity_ref=proposal.diff.entity_ref,
                parameter=proposal.diff.parameter,
                before=proposal.diff.before,
                value=proposal.diff.after,
            ),
            authorization_for(proposal),
            f"exec-{proposal.proposal_id}-{proposal.diff.diff_hash[:12]}",
        )

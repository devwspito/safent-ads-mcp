"""Contrato de `AuthorizationRepository`: identico para el doble en memoria y
para `SqlAuthorizationRepository`."""

from __future__ import annotations

from dataclasses import replace
from datetime import timedelta

from safent_ads.proposals.domain.authorization import (
    Authorization,
    AuthorizationDecision,
    AuthorizationKind,
)
from tests.contracts.execution.conftest import NOW
from tests.contracts.proposals.conftest import AuthorizationFixture, approval_for


async def test_a_signed_authorization_is_read_back_whole(
    authorizations: AuthorizationFixture,
) -> None:
    proposal = await authorizations.given_proposal()
    authorization = approval_for(proposal)

    await authorizations.authorizations.save(authorization)
    stored = await authorizations.authorizations.get(authorization.authorization_id)

    assert stored is not None
    assert stored.proposal_id == proposal.proposal_id
    assert stored.kind is AuthorizationKind.HUMAN_APPROVAL
    assert stored.decision is AuthorizationDecision.APPROVED
    assert stored.diff_hash == authorization.diff_hash
    assert stored.guardrail_verdict_hash == authorization.guardrail_verdict_hash
    assert stored.issued_by == "owner-de-contrato"
    assert stored.signature == authorization.signature
    assert stored.expires_at == authorization.expires_at
    assert stored.comment == "firmado en el banco de contrato"


async def test_an_unknown_authorization_is_none(authorizations: AuthorizationFixture) -> None:
    proposal = await authorizations.given_proposal()
    other = approval_for(proposal)

    assert await authorizations.authorizations.get(other.authorization_id) is None


async def test_the_active_authorization_of_a_proposal_is_the_last_approved(
    authorizations: AuthorizationFixture,
) -> None:
    proposal = await authorizations.given_proposal()
    first = approval_for(proposal)
    await authorizations.authorizations.save(first)
    second = approval_for(proposal, diff_hash=first.diff_hash[::-1])
    second = _decided_later(second)
    await authorizations.authorizations.save(second)

    active = await authorizations.authorizations.get_active_for_proposal(proposal.proposal_id)

    assert active is not None
    assert active.authorization_id == second.authorization_id


async def test_a_proposal_without_decisions_has_no_active_authorization(
    authorizations: AuthorizationFixture,
) -> None:
    proposal = await authorizations.given_proposal()

    assert await authorizations.authorizations.get_active_for_proposal(proposal.proposal_id) is None


def _decided_later(authorization: Authorization) -> Authorization:
    """La segunda decision se firma un minuto despues: el orden por tiempo
    es el que decide cual esta viva."""
    return replace(authorization, decided_at=NOW + timedelta(minutes=1))

"""`SqlAuthorizationRepository` contra Postgres real: lo que solo prueba el
esquema solo-anexable de `approvals` (0008_proposals).

Basta la base compartida: todo lo que se consulta aqui cuelga de una
propuesta propia, ninguna consulta es global."""

from __future__ import annotations

from dataclasses import replace
from datetime import timedelta

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from safent_ads.proposals.domain.authorization import (
    AuthorizationDecision,
    AuthorizationKind,
)
from safent_ads.proposals.infrastructure.errors import (
    DuplicateAuthorizationError,
    UnknownRuleCodeError,
)
from safent_ads.proposals.infrastructure.sql_authorization_repository import (
    SqlAuthorizationRepository,
)
from tests.contracts.execution.conftest import (
    FIRING_RULE_CODE,
    NOW,
    calibrate_rule,
    seed_proposal,
)
from tests.contracts.proposals.conftest import ProposalUnderApproval, approval_for

pytestmark = pytest.mark.integration


async def given_proposal(session: AsyncSession) -> ProposalUnderApproval:
    context = await seed_proposal(session)
    return ProposalUnderApproval(
        proposal_id=context.proposal_id, diff_hash=context.diff_hash
    )


async def test_a_rule_authorization_points_at_its_rule(
    db_session: AsyncSession,
) -> None:
    """`approvals_rule_kind_check` exige la regla; el dominio solo lleva su
    codigo en `issued_by`. El adaptador lo resuelve, no lo inventa."""
    proposal = await given_proposal(db_session)
    await calibrate_rule(db_session, enabled=True)

    await SqlAuthorizationRepository(db_session).save(
        approval_for(
            proposal,
            kind=AuthorizationKind.RULE_AUTHORIZATION,
            issued_by=FIRING_RULE_CODE,
        )
    )

    rule_code = await db_session.scalar(
        text(
            """
            SELECT rule.code FROM approvals
              JOIN rules AS rule ON rule.id = approvals.rule_id
             WHERE approvals.proposal_id = :proposal_id
            """
        ),
        {"proposal_id": str(proposal.proposal_id)},
    )
    assert rule_code == FIRING_RULE_CODE


async def test_a_rule_authorization_citing_an_unknown_rule_is_refused(
    db_session: AsyncSession,
) -> None:
    proposal = await given_proposal(db_session)

    with pytest.raises(UnknownRuleCodeError):
        await SqlAuthorizationRepository(db_session).save(
            approval_for(
                proposal, kind=AuthorizationKind.RULE_AUTHORIZATION, issued_by="Z99"
            )
        )


async def test_approving_the_same_diff_twice_is_refused(
    db_session: AsyncSession,
) -> None:
    """`ix_approvals_live_decision`: aprobar dos veces el mismo diff no crea
    dos autorizaciones. Reaprobar exige rotar el diff."""
    proposal = await given_proposal(db_session)
    repository = SqlAuthorizationRepository(db_session)
    await repository.save(approval_for(proposal))

    with pytest.raises(DuplicateAuthorizationError):
        await repository.save(approval_for(proposal))


async def test_revoking_is_appending_a_new_decision(
    db_session: AsyncSession,
) -> None:
    """Revocar no toca la fila anterior: anexa otra. La tabla no admite
    UPDATE ni DELETE (C-19)."""
    proposal = await given_proposal(db_session)
    repository = SqlAuthorizationRepository(db_session)
    approved = approval_for(proposal)
    await repository.save(approved)

    revoked = replace(
        approved,
        authorization_id=approval_for(proposal).authorization_id,
        decision=AuthorizationDecision.REVOKED,
        decided_at=NOW + timedelta(minutes=1),
    )
    await repository.save(revoked)

    decisions = await db_session.execute(
        text(
            """
            SELECT decision FROM approvals WHERE proposal_id = :proposal_id
             ORDER BY decided_at
            """
        ),
        {"proposal_id": str(proposal.proposal_id)},
    )
    assert [row.decision for row in decisions.all()] == ["approved", "revoked"]
    stored = await repository.get(approved.authorization_id)
    assert stored is not None
    assert stored.decision is AuthorizationDecision.APPROVED

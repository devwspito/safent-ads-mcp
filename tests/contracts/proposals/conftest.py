"""Banco de pruebas de contrato de `AuthorizationRepository`: los mismos
casos contra el doble en memoria y contra `SqlAuthorizationRepository`
(marcado `integration`).

`approvals` es solo-anexable y cuelga de una propuesta real, asi que la
variante SQL siembra negocio, cuenta, entidad y propuesta antes de firmar
nada; en memoria no hay integridad referencial que preparar."""

from __future__ import annotations

import uuid
from collections.abc import AsyncIterator
from dataclasses import dataclass
from datetime import timedelta
from typing import Protocol

import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from safent_ads.proposals.application.ports import AuthorizationRepository
from safent_ads.proposals.domain.authorization import (
    Authorization,
    AuthorizationChannel,
    AuthorizationKind,
    sign_authorization,
)
from safent_ads.proposals.domain.classification import Classification
from safent_ads.proposals.domain.identifiers import AuthorizationId, ProposalId
from safent_ads.proposals.infrastructure.sql_authorization_repository import (
    SqlAuthorizationRepository,
)
from safent_ads.proposals.testing.fakes import FakeAuthorizationRepository, FakeSignerPort
from tests.conftest import rolled_back_session
from tests.contracts.execution.conftest import NOW, digest, seed_proposal


@dataclass(frozen=True, slots=True)
class ProposalUnderApproval:
    proposal_id: ProposalId
    diff_hash: str


class AuthorizationFixture(Protocol):
    authorizations: AuthorizationRepository

    async def given_proposal(self) -> ProposalUnderApproval:
        """Propuesta viva a la que se le pueden anexar decisiones."""


@dataclass(slots=True)
class InMemoryAuthorizationFixture:
    authorizations: FakeAuthorizationRepository

    async def given_proposal(self) -> ProposalUnderApproval:
        proposal_id = ProposalId.new()
        return ProposalUnderApproval(
            proposal_id=proposal_id, diff_hash=digest(f"diff-{proposal_id}")
        )


@dataclass(slots=True)
class SqlAuthorizationFixture:
    authorizations: SqlAuthorizationRepository
    session: AsyncSession

    async def given_proposal(self) -> ProposalUnderApproval:
        context = await seed_proposal(self.session)
        return ProposalUnderApproval(
            proposal_id=context.proposal_id, diff_hash=digest(f"otro-diff-{uuid.uuid4()}")
        )


def approval_for(
    proposal: ProposalUnderApproval,
    *,
    kind: AuthorizationKind = AuthorizationKind.HUMAN_APPROVAL,
    issued_by: str = "owner-de-contrato",
    diff_hash: str | None = None,
) -> Authorization:
    return sign_authorization(
        authorization_id=AuthorizationId.new(),
        proposal_id=proposal.proposal_id,
        kind=kind,
        proposal_classification=Classification.ROUTINE,
        diff_hash=diff_hash or proposal.diff_hash,
        guardrail_verdict_hash=digest(f"verdict-{proposal.proposal_id}"),
        issued_by=issued_by,
        channel=(
            AuthorizationChannel.PANEL
            if kind is AuthorizationKind.HUMAN_APPROVAL
            else AuthorizationChannel.RULE_ENGINE
        ),
        decided_at=NOW,
        expires_at=NOW + timedelta(hours=1),
        signer=FakeSignerPort(),
        comment="firmado en el banco de contrato",
    )


@pytest.fixture(
    params=[
        pytest.param("in_memory", id="in_memory"),
        pytest.param("sql", id="sql", marks=pytest.mark.integration),
    ]
)
async def authorizations(request: pytest.FixtureRequest) -> AsyncIterator[AuthorizationFixture]:
    if request.param == "in_memory":
        yield InMemoryAuthorizationFixture(authorizations=FakeAuthorizationRepository())
        return
    database_url: str = request.getfixturevalue("isolated_database_url")
    async with rolled_back_session(database_url) as session:
        yield SqlAuthorizationFixture(
            authorizations=SqlAuthorizationRepository(session), session=session
        )

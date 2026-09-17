"""Dobles en memoria de `proposals/application/ports.py` y de
`proposals/domain/authorization.py::SignerPort`/`VerifierPort`."""

from __future__ import annotations

import hashlib
import hmac

from safent_ads.proposals.domain.authorization import Authorization, AuthorizationDecision
from safent_ads.proposals.domain.identifiers import AuthorizationId, ProposalId
from safent_ads.proposals.domain.proposal import Proposal, ProposalState
from safent_ads.shared.ids import EntityRef

# Mismos estados que `SqlProposalRepository.LIVE_STATES`
# (proposals/infrastructure): mientras la propuesta este en uno de ellos,
# una equivalente la actualiza en vez de crear otra (FR-20).
_LIVE_STATES = frozenset(
    {
        ProposalState.PENDING,
        ProposalState.POSTPONED,
        ProposalState.APPROVED,
        ProposalState.SCHEDULED,
    }
)


class FakeSignerPort:
    """HMAC-SHA256 simetrico: sustituye a la Ed25519 real de `shared/crypto`
    hasta que esa rama la publique. Misma forma (`sign(bytes) -> bytes`)."""

    def __init__(self, key: bytes = b"fake-signing-key") -> None:
        self._key = key

    def sign(self, payload: bytes) -> bytes:
        return hmac.new(self._key, payload, hashlib.sha256).digest()


class FakeVerifierPort:
    """Verifica contra la misma clave simetrica que `FakeSignerPort` — en
    produccion el broker usaria la clave publica Ed25519, no una compartida."""

    def __init__(self, key: bytes = b"fake-signing-key") -> None:
        self._key = key

    def verify(self, payload: bytes, signature: bytes) -> bool:
        expected = hmac.new(self._key, payload, hashlib.sha256).digest()
        return hmac.compare_digest(expected, signature)


class FakeProposalRepository:
    """Repositorio en memoria: valido para tests y como valor por defecto en
    `composition/container.py` hasta que exista `0008_proposals`."""

    def __init__(self) -> None:
        self._store: dict[str, Proposal] = {}

    async def get(self, proposal_id: ProposalId) -> Proposal | None:
        return self._store.get(str(proposal_id))

    async def save(self, proposal: Proposal) -> None:
        self._store[str(proposal.proposal_id)] = proposal

    async def find_live_equivalent(
        self, entity_ref: EntityRef, parameter: str
    ) -> Proposal | None:
        for proposal in self._store.values():
            if (
                proposal.diff.entity_ref == entity_ref
                and proposal.diff.parameter == parameter
                and proposal.state in _LIVE_STATES
            ):
                return proposal
        return None

    def all(self) -> list[Proposal]:
        """Solo para tests: inspeccionar todo lo guardado sin tocar `_store`."""
        return list(self._store.values())

    def clear(self) -> None:
        """Solo para tests: simula "la propuesta ya no existe"."""
        self._store.clear()


class FakeAuthorizationRepository:
    """`approvals` es solo-anexable: guarda todas las versiones y expone la
    ultima `approved` para una propuesta (data-model.md: "revocar = anexar
    una nueva con decision = revoked")."""

    def __init__(self) -> None:
        self._by_id: dict[str, Authorization] = {}
        self._history_by_proposal: dict[str, list[Authorization]] = {}

    async def get(self, authorization_id: AuthorizationId) -> Authorization | None:
        return self._by_id.get(str(authorization_id))

    async def get_active_for_proposal(self, proposal_id: ProposalId) -> Authorization | None:
        history = self._history_by_proposal.get(str(proposal_id), [])
        for authorization in reversed(history):
            if authorization.decision is AuthorizationDecision.APPROVED:
                return authorization
        return None

    async def save(self, authorization: Authorization) -> None:
        self._by_id[str(authorization.authorization_id)] = authorization
        self._history_by_proposal.setdefault(str(authorization.proposal_id), []).append(
            authorization
        )

    def clear(self) -> None:
        """Solo para tests: simula "la autorizacion ya no existe"."""
        self._by_id.clear()
        self._history_by_proposal.clear()

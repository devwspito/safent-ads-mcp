"""Identificadores opacos del contexto `proposals` (data-model.md: tablas
`proposals`, `approvals`)."""

from __future__ import annotations

import uuid
from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class ProposalId:
    """Identidad de una `Proposal` (tabla `proposals`)."""

    value: uuid.UUID

    @classmethod
    def new(cls) -> ProposalId:
        return cls(uuid.uuid4())

    @classmethod
    def parse(cls, raw: str) -> ProposalId:
        return cls(uuid.UUID(raw))

    def __str__(self) -> str:
        return str(self.value)


@dataclass(frozen=True, slots=True)
class AuthorizationId:
    """Identidad de una `Authorization` (tabla `approvals`)."""

    value: uuid.UUID

    @classmethod
    def new(cls) -> AuthorizationId:
        return cls(uuid.uuid4())

    @classmethod
    def parse(cls, raw: str) -> AuthorizationId:
        return cls(uuid.UUID(raw))

    def __str__(self) -> str:
        return str(self.value)

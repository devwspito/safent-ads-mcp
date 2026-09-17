"""Identificador opaco del contexto `execution` (data-model.md tabla
`executions`)."""

from __future__ import annotations

import uuid
from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class ExecutionId:
    value: uuid.UUID

    @classmethod
    def new(cls) -> ExecutionId:
        return cls(uuid.uuid4())

    def __str__(self) -> str:
        return str(self.value)

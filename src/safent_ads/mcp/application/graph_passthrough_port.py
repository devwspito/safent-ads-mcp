"""004 tasks-2.md R5 (historia 19): `get_meta_graph`, el paso a traves de
lectura de Meta. Modulo autonomo: su propio DTO, sin tocar `ports.py`/
`dto.py` compartidos. Puro: sin I/O, sin framework."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any, Protocol

__all__ = ["GraphPassthroughPort", "GraphPassthroughResult"]


@dataclass(frozen=True, slots=True)
class GraphPassthroughResult:
    rows: tuple[Mapping[str, Any], ...]
    truncated: bool


class GraphPassthroughPort(Protocol):
    async def get_meta_graph(
        self,
        business_id: str,
        account_ref: str,
        *,
        node: str,
        edge: str,
        fields: tuple[str, ...],
        params: Mapping[str, Any],
    ) -> GraphPassthroughResult: ...

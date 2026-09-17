"""`ToolSpec`: forma minima y autonoma de una herramienta MCP de `optimization`
(mismo criterio que `economics.presentation.tool_spec`, duplicado a
proposito -- `optimization` no importa `mcp`, plan.md §4).

`propose_reallocation_plan` es `verb_kind="proposal"`: crea dos
`PropuestaDeAccion` ligadas via `ReallocationProposalPort`, nunca escribe en
plataforma (la unica escritura sigue siendo `apply_defensive_action`, el
chokepoint de `execution`, FR-38)."""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Any, Literal

from pydantic import BaseModel

VerbKind = Literal["read", "proposal"]


@dataclass(frozen=True, slots=True)
class ToolSpec:
    name: str
    description: str
    args_model: type[BaseModel]
    handler: Callable[[Any], Awaitable[dict[str, Any]]]
    verb_kind: VerbKind = "read"

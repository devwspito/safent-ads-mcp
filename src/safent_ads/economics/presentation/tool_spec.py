"""`ToolSpec`: forma minima y autonoma de una herramienta MCP de lectura
(profitability-engine.md §8), deliberadamente mas pequena que
`mcp.presentation.registry.ToolDefinition` -- `economics` no importa `mcp`
(contextos hermanos, sin import cruzado, plan.md §4). La lane que cablea
`mcp/` adapta cada `ToolSpec` a su `ToolDefinition` en una linea (ver
`contracts/mcp-tools.md` §Integracion): mismo `name`/`description`/
`args_model`, `tool_class=ToolClass.READ`, `handler=lambda args, _scope:
spec.handler(args)`, `business_id_of=lambda args: str(args.business_id)`.

Todos los tools de esta lane son `verb_kind="read"` (P1: solo lectura o
propuesta absorbida en `propose_reallocation_plan`, que crea una
`PropuestaDeAccion` -- ver `proposals` -- nunca escribe en plataforma)."""

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

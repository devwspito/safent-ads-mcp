"""Regla pura del contrato MCP (contracts/mcp-tools.md regla 2: "Verbo
primero") y del invariante INV-2 (regla 1: "ninguna herramienta escribe en
una plataforma" / threat-model.md C-2: "ningun verbo de decision en MCP").

Puro y sin estado: el `ToolRegistry` (presentation) usa esto para negarse a
registrar una herramienta mal nombrada en tiempo de construccion, no solo en
un test."""

from __future__ import annotations

_READ_PREFIXES = (
    "list_",
    "get_",
    "search_",
    "run_",
    "explain_",
    "diagnose_",
    "simulate_",
    "design_",
    "build_",
    "validate_",
    "compare_",
)
_PROPOSAL_PREFIXES = ("propose_", "withdraw_", "generate_", "apply_")
_FORBIDDEN_DECISION_PREFIXES = ("approve_", "execute_", "apply_proposal")


def is_read_verb(tool_name: str) -> bool:
    """`list_* / get_* / search_* / run_* / explain_* / diagnose_* /
    simulate_* / design_*` son lecturas auto-ejecutables (contracts/mcp-tools.md
    regla 2). `diagnose_entity` y `simulate_spend_change`
    (profitability-engine.md §8) no escriben nada: el primero devuelve el
    camino de 9 nodos, el segundo un calculo en seco -- misma naturaleza
    que `explain_*`/`run_*`. `design_experiment` (§4/§8, T201) es la misma
    naturaleza: viabilidad y muestra en seco, nunca persiste nada.
    `build_*`/`validate_*`/`compare_*` (§9, T159/T160) son la misma clase:
    computan o comparan en memoria, nunca tocan una plataforma ni crean una
    `PropuestaDeAccion`."""
    return tool_name.startswith(_READ_PREFIXES)


def is_proposal_verb(tool_name: str) -> bool:
    """`propose_* / withdraw_* / generate_* / apply_*` no son lecturas; el
    unico `apply_*` permitido es `apply_defensive_action` (F2, fuera de esta
    lane)."""
    return tool_name.startswith(_PROPOSAL_PREFIXES)


def is_forbidden_decision_verb(tool_name: str) -> bool:
    """Ningun nombre de herramienta puede empezar por un verbo que decida
    o ejecute (threat-model.md C-2: "ningun verbo de decision en MCP")."""
    return tool_name.startswith(_FORBIDDEN_DECISION_PREFIXES)

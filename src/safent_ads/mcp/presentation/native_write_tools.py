"""`propose_native_write` (004 tasks-2.md W3, historia 20): escritura nativa
para lo que aun no tiene herramienta propia. Modulo autonomo, mismo
aislamiento que `connection_tools.py`: declara su propio handler sobre
`ProposalWritePort.propose_native_write`, sin tocar `write_handlers.py` (el
payload y su lista negra son de dominio puro, `mcp/domain/
native_write_payload.py`, ya aplicados en `presentation/args.py`).

Clasificacion: `ProposalKind.NATIVE_WRITE` esta en `_ALWAYS_IMPORTANT`
(`proposals/domain/classification.py`) -- nunca `ROUTINE`, ninguna regla
`AUTO` la autoriza jamas (S-2, security-engineer)."""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from typing import Any

from safent_ads.mcp.application.caller_scope import CallerScope
from safent_ads.mcp.application.proposal_write_port import ProposalWritePort, ProposalWriteResult
from safent_ads.mcp.presentation.args import ProposeNativeWriteArgs
from safent_ads.mcp.presentation.registry import ToolClass, ToolDefinition

__all__ = ["build_native_write_tool_definitions"]

type Handler[ArgsT, ReturnT] = Callable[[ArgsT, CallerScope], Awaitable[ReturnT]]

_PERSON_CALLER_PREFIX = "person:"

_DESCRIPTION = (
    "Propone una escritura nativa en Meta o Google para lo que aun no tiene herramienta "
    "propia: `payload` viaja tal cual al panel, con un aviso de que Safent no interpreta su "
    "contenido. Nunca presupuesto, puja, estado ni credenciales (usa propose_budget_change / "
    "propose_bid_target / propose_pause / propose_resume para eso). Siempre exige aprobacion "
    "humana; ninguna regla automatica puede autorizarla."
)


def _proposed_by(caller_scope: CallerScope) -> str | None:
    return (
        caller_scope.caller_id if caller_scope.caller_id.startswith(_PERSON_CALLER_PREFIX) else None
    )


def build_native_write_tool_definitions(port: ProposalWritePort) -> list[ToolDefinition[Any]]:
    return [
        ToolDefinition(
            name="propose_native_write",
            description=_DESCRIPTION,
            args_model=ProposeNativeWriteArgs,
            tool_class=ToolClass.PROPOSAL,
            handler=_propose_native_write(port),
            business_id_of=_by_business_id,
        )
    ]


def _propose_native_write(
    port: ProposalWritePort,
) -> Handler[ProposeNativeWriteArgs, ProposalWriteResult]:
    async def handler(
        args: ProposeNativeWriteArgs, caller_scope: CallerScope
    ) -> ProposalWriteResult:
        return await port.propose_native_write(
            business_id=args.business_id,
            entity_ref=args.entity_ref,
            platform=args.platform.value,
            operation=args.operation,
            payload=args.payload,
            why=args.why,
            proposed_by=_proposed_by(caller_scope),
        )

    return handler


def _by_business_id(args: Any) -> str:  # noqa: ANN401 - extractor generico por posicion
    return str(args.business_id)

"""Las 5 `ToolDefinition` `PROPOSAL` que faltaban en el mapa `WriteOperation`
<-> herramienta (004 tasks-2.md W1/W2, historia 16-17): `propose_resume`,
`propose_bid_target`, `propose_negative_keywords`, `propose_creative_rotation`
y `propose_delete`. Mismo patron que `connection_tools.py`/`experiment_tools.py`:
modulo autonomo que `catalog.py` engancha con una linea (`I1`), los handlers
reales viven en `write_handlers.py` (`build_write_handlers`) para que las 12
escrituras existentes+nuevas compartan un unico punto de construccion."""

from __future__ import annotations

from typing import Any

from safent_ads.mcp.application.proposal_write_port import ProposalWritePort
from safent_ads.mcp.presentation import args as a
from safent_ads.mcp.presentation.registry import ToolClass, ToolDefinition
from safent_ads.mcp.presentation.write_handlers import build_write_handlers

__all__ = ["build_optimization_write_tool_definitions"]

_CATALOG: tuple[tuple[str, str, type[a.ToolArgs]], ...] = (
    (
        "propose_resume",
        "Propone reanudar una entidad pausada (vuelve a `ACTIVE`). Crea una propuesta "
        "pendiente; el dueno aprueba antes de que reabra el gasto en la plataforma.",
        a.ProposeResumeArgs,
    ),
    (
        "propose_bid_target",
        "Propone cambiar la puja objetivo de una entidad (en la divisa de la cuenta). "
        "Crea una propuesta pendiente de aprobacion del dueno.",
        a.ProposeBidTargetArgs,
    ),
    (
        "propose_negative_keywords",
        "Propone anadir palabras clave negativas (hasta 50) a una entidad de busqueda. "
        "Crea una propuesta pendiente; no las anade hasta que el dueno la aprueba.",
        a.ProposeNegativeKeywordsArgs,
    ),
    (
        "propose_creative_rotation",
        "Propone rotar fuera (pausar) la creatividad de un anuncio concreto. Crea una "
        "propuesta pendiente de aprobacion del dueno.",
        a.ProposeCreativeRotationArgs,
    ),
    (
        "propose_delete",
        "Propone eliminar una entidad (campana/conjunto/anuncio). Irreversible: crea una "
        "propuesta pendiente que exige aprobacion humana, nunca se ejecuta sola.",
        a.ProposeDeleteArgs,
    ),
)


def build_optimization_write_tool_definitions(port: ProposalWritePort) -> list[ToolDefinition[Any]]:
    handlers = build_write_handlers(port)
    return [
        ToolDefinition(
            name=name,
            description=description,
            args_model=args_model,
            tool_class=ToolClass.PROPOSAL,
            handler=handlers[name],
            business_id_of=_by_business_id,
        )
        for name, description, args_model in _CATALOG
    ]


def _by_business_id(args: Any) -> str:  # noqa: ANN401 - extractor generico por posicion
    return str(args.business_id)

"""004 tasks-2.md R5 (historia 19): `get_meta_graph`, nombre verbo-primero
(H-2; `spec.md` lo llama `meta_graph_get`). Modulo autonomo: declara su
propio `Args`, no toca `args.py` compartido.

La politica de la superficie (aristas/campos/params permitidos, node valido,
truncado) vive entera en `mcp.domain.meta_graph_path`
(`BrokerGraphPassthroughPort` la aplica antes de tocar el bróker); aqui solo
se traducen sus excepciones de dominio a `VALIDATION_ERROR`, una sola vez,
en el borde (shared/errors.py: "excepciones de dominio -> mapeador en
presentacion")."""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Annotated, Any

from pydantic import Field

from safent_ads.mcp.application.caller_scope import CallerScope
from safent_ads.mcp.application.errors import ToolValidationError
from safent_ads.mcp.application.graph_passthrough_port import (
    GraphPassthroughPort,
    GraphPassthroughResult,
)
from safent_ads.mcp.domain.meta_graph_path import (
    GraphEdgeDeniedError,
    GraphFieldDeniedError,
    GraphNodeFormatError,
    GraphParamsDeniedError,
)
from safent_ads.mcp.presentation.args import BusinessId, OpaqueId, ToolArgs
from safent_ads.mcp.presentation.registry import ToolClass, ToolDefinition

__all__ = ["PassthroughToolServices", "build_passthrough_tool_definitions"]

type Handler[ArgsT, ReturnT] = Callable[[ArgsT, CallerScope], Awaitable[ReturnT]]

_MAX_FIELDS = 30
_NODE_PATTERN = r"^(act_)?\d+$"
_FIELD_NAME_PATTERN = r"^[a-z_]{1,64}$"

_GraphNode = Annotated[str, Field(pattern=_NODE_PATTERN, max_length=32)]
_GraphField = Annotated[str, Field(pattern=_FIELD_NAME_PATTERN)]
# Bj-3: mismo alfabeto que las aristas de la lista blanca
# (`meta_graph_path._ALLOWED_EDGES`, todas `[a-z_]`, la mas larga 18
# caracteres) mas margen -- corta en el borde cualquier valor con mayusculas,
# digitos o caracteres de control antes de que llegue al mensaje de error.
_GraphEdge = Annotated[str, Field(pattern=r"^[a-z_]{0,40}$")]

_GRAPH_POLICY_ERRORS = (
    GraphEdgeDeniedError,
    GraphFieldDeniedError,
    GraphNodeFormatError,
    GraphParamsDeniedError,
)


@dataclass(frozen=True, slots=True)
class PassthroughToolServices:
    graph: GraphPassthroughPort


class GetMetaGraphArgs(ToolArgs):
    business_id: BusinessId
    account_ref: OpaqueId
    node: _GraphNode
    edge: _GraphEdge = ""
    # M-1: `fields=[]` dejaba la proyeccion en manos de Meta -- al menos un
    # campo explicito, siempre proyectado (`BrokerGraphPassthroughPort`).
    fields: Annotated[list[_GraphField], Field(min_length=1, max_length=_MAX_FIELDS)]
    params: dict[str, Any] = Field(default_factory=dict)


def build_passthrough_tool_definitions(
    services: PassthroughToolServices,
) -> list[ToolDefinition[Any]]:
    return [
        ToolDefinition(
            name="get_meta_graph",
            description=(
                "Paso a traves de lectura de Meta sobre una arista de la lista blanca "
                "(campanas, conjuntos, anuncios, creatividades, imagenes, videos, informes o "
                "reglas). Sin token, facturacion, propiedad ni usuarios asignados: esos campos "
                "se rechazan aunque la arista los permita. Una pagina, sin paginacion automatica."
            ),
            args_model=GetMetaGraphArgs,
            tool_class=ToolClass.READ,
            handler=_get_meta_graph(services.graph),
            business_id_of=_by_business_id,
        )
    ]


def _by_business_id(args: Any) -> str:  # noqa: ANN401 - extractor generico por posicion
    return str(args.business_id)


def _get_meta_graph(
    port: GraphPassthroughPort,
) -> Handler[GetMetaGraphArgs, GraphPassthroughResult]:
    async def handler(
        args: GetMetaGraphArgs, _caller_scope: CallerScope
    ) -> GraphPassthroughResult:
        try:
            return await port.get_meta_graph(
                args.business_id,
                args.account_ref,
                node=args.node,
                edge=args.edge,
                fields=tuple(args.fields),
                params=args.params,
            )
        except _GRAPH_POLICY_ERRORS as exc:
            raise ToolValidationError(str(exc)) from exc

    return handler

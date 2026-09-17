"""`BrokerGraphPassthroughPort` real (R5, historia 19): valida TODO en
cliente (IDOR de la cuenta + politica pura de `meta_graph_path.py`) antes
de tocar el bróker, y vuelve a acotar la respuesta (`truncate_response`) al
volver -- ninguna de las dos comprobaciones confia en que el otro lado ya
la hizo (fail-closed en profundidad, S-1).

**Contrato de la op nueva que cablea I1** (`broker/presentation/
dispatcher.py` + `broker/platforms/meta_ads_adapter.py`, reutilizando
`meta_graph_reader.py` de R3 para el `get_node`/`get_edge` real):
peticion `{"op": "meta_graph_get", "business_id", "connection_id",
"platform": "meta", "external_account_id", "node", "edge", "fields",
"params"}`; respuesta `{"rows": [...]}`. Si `node` es un id numerico que no
pertenece a la cuenta (comprobado en el bróker resolviendo el nodo contra
`account_id`, el unico proceso con credenciales), el bróker responde
`{"ok": false, "error_code": "ENTITY_NOT_FOUND"}` -- mismo codigo que un
nodo inexistente, nunca se distingue (S-1: "inexistente y ajeno
indistinguibles")."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from safent_ads.accounts.application.errors import BrokerRequestDeniedError
from safent_ads.accounts.infrastructure.broker_client import BrokerSocketClient
from safent_ads.accounts.infrastructure.errors import BrokerConnectionError
from safent_ads.mcp.application.errors import (
    EntityNotFoundError,
    broker_denial_error,
    broker_unavailable_error,
)
from safent_ads.mcp.application.graph_passthrough_port import GraphPassthroughResult
from safent_ads.mcp.domain.meta_graph_path import (
    truncate_response,
    validate_edge,
    validate_fields,
    validate_node_shape,
    validate_params,
)
from safent_ads.mcp.infrastructure.account_ownership import resolve_owned_account_ref
from safent_ads.shared.clock import Clock

__all__ = ["BrokerGraphPassthroughPort", "GraphPassthroughBrokerClient"]

_ENTITY_NOT_FOUND_CODE = "ENTITY_NOT_FOUND"


class GraphPassthroughBrokerClient(BrokerSocketClient):
    def __init__(self, socket_path: Path) -> None:
        super().__init__(socket_path)

    async def get_meta_graph(
        self,
        *,
        business_id: str,
        external_account_id: str,
        node: str,
        edge: str,
        fields: tuple[str, ...],
        params: Mapping[str, Any],
    ) -> list[dict[str, Any]]:
        result = await self._request(
            {
                "op": "meta_graph_get",
                "business_id": business_id,
                "platform": "meta",
                "external_account_id": external_account_id,
                "node": node,
                "edge": edge,
                "fields": list(fields),
                "params": dict(params),
            },
            # Lectura pura del Graph (`get_node`/`get_edge`, R5): mismo
            # arranque en frio que `broker_reference_data_port.py`, ver
            # `BrokerSocketClient._request`.
            retryable=True,
        )
        return list(result["rows"])


class BrokerGraphPassthroughPort:
    def __init__(
        self,
        client: GraphPassthroughBrokerClient,
        session_factory: async_sessionmaker[AsyncSession],
        clock: Clock,
    ) -> None:
        self._client = client
        self._session_factory = session_factory
        self._clock = clock

    async def get_meta_graph(
        self,
        business_id: str,
        account_ref: str,
        *,
        node: str,
        edge: str,
        fields: tuple[str, ...],
        params: Mapping[str, Any],
    ) -> GraphPassthroughResult:
        ref = await resolve_owned_account_ref(self._session_factory, business_id, account_ref)
        validate_edge(edge)
        validate_fields(edge, fields)
        validate_node_shape(node)
        validate_params(params, today=self._clock.now().date())
        try:
            rows = await self._client.get_meta_graph(
                business_id=business_id,
                external_account_id=ref.external_account_id,
                node=node,
                edge=edge,
                fields=fields,
                params=params,
            )
        except BrokerRequestDeniedError as exc:
            if exc.error_code == _ENTITY_NOT_FOUND_CODE:
                raise EntityNotFoundError(f"{node} aun no disponible") from None
            # Incidente de produccion (companion 0.2.21): cualquier otro
            # codigo de denegacion (p.ej. `PLATFORM_APP_NOT_CONFIGURED`)
            # escapaba crudo hacia el SDK MCP (`UnexpectedToolError` opaco)
            # en vez del sobre limpio del contrato.
            translated = broker_denial_error(exc.error_code)
            if translated is None:
                raise
            raise translated from None
        except BrokerConnectionError as exc:
            raise broker_unavailable_error() from exc
        capped = truncate_response(_project_to_fields(rows, fields))
        return GraphPassthroughResult(rows=capped.rows, truncated=capped.truncated)


def _project_to_fields(
    rows: Sequence[Mapping[str, Any]], fields: tuple[str, ...]
) -> list[Mapping[str, Any]]:
    """B-1: proyeccion obligatoria en el CLIENTE -- si el broker (u otro
    salto intermedio) devuelve columnas de mas, nunca llegan al llamante,
    aunque `fields` ya se haya validado contra la lista blanca antes."""
    allowed = frozenset(fields)
    return [{key: value for key, value in row.items() if key in allowed} for row in rows]

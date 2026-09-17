"""004 tasks-2.md R3 (historia 18): seis lecturas de referencia de Meta,
cada una sobre una arista concreta ya conocida del Graph
(`MetaGraphClient.get_edge`, que `meta_ads_adapter.py` ya expone -- este
modulo solo importa el `Protocol`, no lo edita). Mismo patron que
`ad_child_creation.py`/`campaign_creation.py`: ayudante en su propio
modulo, el adaptador despacha en pocas lineas (I1).

`search_meta_targeting`/`get_meta_reach_estimate` no tienen un nodo/arista
literal en el Graph API real (son `/search?type=...` y una peticion con
`targeting_spec`, respectivamente) -- se modelan aqui como una arista
sintetica propia (`targeting_search`/`delivery_estimate`) sobre
`get_edge(account_node, edge, fields, params)`, para no ampliar
`MetaGraphClient` con metodos nuevos por cada capacidad: el adaptador real
(`LiveMetaGraphClient`, fuera de este carril) interpreta esas dos aristas
sinteticas y arma la llamada HTTP que corresponda. Contrato declarado aqui
para quien cablee esa traduccion.

Lista blanca de campos por arista: ningun campo de token, facturacion,
propiedad o usuarios asignados sale nunca (R3 regla de seguridad)."""

from __future__ import annotations

import asyncio
import json
from collections.abc import Mapping, Sequence
from typing import Any, Final

from safent_ads.broker.platforms.meta_ads_adapter import MetaGraphClient

__all__ = [
    "fetch_audiences",
    "fetch_pages",
    "fetch_pixels",
    "fetch_reach_estimate",
    "fetch_saved_audiences",
    "search_targeting",
]

_MAX_ROWS: Final = 200
_MAX_RESPONSE_BYTES: Final = 64 * 1024

_PAGE_FIELDS: Final = ("id", "name", "instagram_business_account")
_PIXEL_FIELDS: Final = ("id", "name", "last_fired_time")
_AUDIENCE_FIELDS: Final = ("id", "name", "subtype", "approximate_count_upper_bound")
_SAVED_AUDIENCE_FIELDS: Final = ("id", "name", "approximate_count_upper_bound")
_TARGETING_FIELDS: Final = ("id", "name", "audience_size_lower_bound", "audience_size_upper_bound")
# `delivery_estimate` v26: `estimate_dau` esta obsoleto y `users_*_bound` no
# existen (verificado en vivo 2026-09-15); Meta devuelve los MAU. El contrato
# del puerto (`users_lower_bound`/`users_upper_bound`) se mantiene mapeando.
_REACH_ESTIMATE_FIELDS: Final = (
    "estimate_mau_lower_bound",
    "estimate_mau_upper_bound",
    "estimate_ready",
)
_REACH_ESTIMATE_CONTRACT: Final = {
    "estimate_mau_lower_bound": "users_lower_bound",
    "estimate_mau_upper_bound": "users_upper_bound",
    "estimate_ready": "estimate_ready",
}


async def fetch_pages(client: MetaGraphClient, account_node: str) -> list[dict[str, Any]]:
    """El campo `instagram_business_account` de cada pagina ya trae la
    cuenta de Instagram vinculada (Graph API): no hace falta una segunda
    consulta a la arista `instagram_accounts` para "el id de pagina que
    faltaba + cuentas de Instagram vinculadas" (R3)."""
    return await _read_edge(client, account_node, "promote_pages", _PAGE_FIELDS)


async def fetch_pixels(client: MetaGraphClient, account_node: str) -> list[dict[str, Any]]:
    return await _read_edge(client, account_node, "adspixels", _PIXEL_FIELDS)


async def fetch_audiences(client: MetaGraphClient, account_node: str) -> list[dict[str, Any]]:
    return await _read_edge(client, account_node, "customaudiences", _AUDIENCE_FIELDS)


async def fetch_saved_audiences(
    client: MetaGraphClient, account_node: str
) -> list[dict[str, Any]]:
    return await _read_edge(client, account_node, "saved_audiences", _SAVED_AUDIENCE_FIELDS)


async def search_targeting(
    client: MetaGraphClient, account_node: str, *, kind: str, query: str
) -> list[dict[str, Any]]:
    params = {"type": kind, "q": query, "limit": _MAX_ROWS}
    # Arista real del Graph API v26: `act_{id}/targetingsearch` (verificado en vivo
    # 2026-09-15; `targeting_search` no existe: "Unknown path components").
    return await _read_edge(client, account_node, "targetingsearch", _TARGETING_FIELDS, params)


async def fetch_reach_estimate(
    client: MetaGraphClient,
    account_node: str,
    *,
    optimization_goal: str,
    countries: Sequence[str],
) -> Mapping[str, Any]:
    params = {
        "optimization_goal": optimization_goal,
        "targeting_spec": {"geo_locations": {"countries": list(countries)}},
    }
    rows = await _read_edge(
        client, account_node, "delivery_estimate", _REACH_ESTIMATE_FIELDS, params
    )
    if not rows:
        return {}
    return {_REACH_ESTIMATE_CONTRACT[key]: value for key, value in rows[0].items()}


async def _read_edge(
    client: MetaGraphClient,
    node_id: str,
    edge: str,
    fields: Sequence[str],
    params: Mapping[str, Any] | None = None,
) -> list[dict[str, Any]]:
    rows = await asyncio.to_thread(client.get_edge, node_id, edge, fields, params)
    projected = [_project_fields(row, fields) for row in rows[:_MAX_ROWS]]
    return _cap_by_bytes(projected)


def _project_fields(row: Mapping[str, Any], fields: Sequence[str]) -> dict[str, Any]:
    """Defensa en profundidad: aunque el Graph API respete `fields`, solo
    sale lo que la lista blanca de esta arista declaro pedir."""
    return {key: row[key] for key in fields if key in row}


def _cap_by_bytes(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    while rows and len(json.dumps(rows).encode("utf-8")) > _MAX_RESPONSE_BYTES:
        rows = rows[:-1]
    return rows

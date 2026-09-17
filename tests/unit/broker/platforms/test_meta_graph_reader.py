"""Incidente 2026-09-15 (companion 0.2.27): dos lecturas de referencia de Meta
hablaban con aristas/campos que no existen en el Graph API v26 y morian como
`TOOL_FAILED` opaco. Verificado en vivo: la busqueda de targeting es
`act_{id}/targetingsearch` (no `targeting_search`), y `delivery_estimate`
devuelve `estimate_mau_*` (no `users_*_bound`; `estimate_dau` esta obsoleto)."""

from __future__ import annotations

import asyncio
from collections.abc import Mapping, Sequence
from typing import Any

from safent_ads.broker.platforms import meta_graph_reader as reader


class _FakeGraphClient:
    def __init__(self, rows: list[dict[str, Any]]) -> None:
        self.calls: list[dict[str, Any]] = []
        self._rows = rows

    def get_edge(
        self,
        node_id: str,
        edge: str,
        fields: Sequence[str],
        params: Mapping[str, Any] | None = None,
        *,
        paginate: bool = True,
    ) -> Sequence[Mapping[str, Any]]:
        del paginate  # el lector nunca pagina distinto; firma igual al protocolo
        self.calls.append(
            {"node": node_id, "edge": edge, "fields": tuple(fields), "params": dict(params or {})}
        )
        return self._rows


def test_search_targeting_uses_the_real_targetingsearch_edge() -> None:
    client = _FakeGraphClient(
        [
            {
                "id": "6003332344237",
                "name": "Perros (animales)",
                "audience_size_lower_bound": 1,
                "audience_size_upper_bound": 2,
                "extra": "x",
            }
        ]
    )
    rows = asyncio.run(
        reader.search_targeting(client, "act_123", kind="adinterest", query="perros")
    )
    call = client.calls[0]
    assert call["edge"] == "targetingsearch"
    assert call["node"] == "act_123"
    assert call["params"]["type"] == "adinterest"
    assert call["params"]["q"] == "perros"
    assert "extra" not in rows[0]
    assert rows[0]["name"] == "Perros (animales)"


def test_reach_estimate_requests_mau_fields_and_maps_them_to_the_contract() -> None:
    client = _FakeGraphClient(
        [
            {
                "estimate_mau_lower_bound": 31500000,
                "estimate_mau_upper_bound": 37000000,
                "estimate_ready": True,
            }
        ]
    )
    estimate = asyncio.run(
        reader.fetch_reach_estimate(client, "act_123", optimization_goal="REACH", countries=("ES",))
    )
    call = client.calls[0]
    assert call["edge"] == "delivery_estimate"
    assert call["fields"] == (
        "estimate_mau_lower_bound",
        "estimate_mau_upper_bound",
        "estimate_ready",
    )
    assert call["params"]["optimization_goal"] == "REACH"
    assert call["params"]["targeting_spec"] == {"geo_locations": {"countries": ["ES"]}}
    assert estimate == {
        "users_lower_bound": 31500000,
        "users_upper_bound": 37000000,
        "estimate_ready": True,
    }


def test_reach_estimate_without_rows_is_empty() -> None:
    client = _FakeGraphClient([])
    estimate = asyncio.run(
        reader.fetch_reach_estimate(client, "act_123", optimization_goal="REACH", countries=("ES",))
    )
    assert estimate == {}

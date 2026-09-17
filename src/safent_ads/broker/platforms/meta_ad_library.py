"""004 tasks-2.md R7 (historias 21-23): `search_competitor_ads` sobre la
API oficial de la Biblioteca de Anuncios de Meta (`GET /ads_archive`), con
la app de la empresa ya conectada (razon *a* de la regla de no
interferencia: sin esto, el arnes no podria llamar a esta API en absoluto).

Mismo patron que `meta_graph_reader.py` (R3): funcion pura de mapeo +
`Protocol` estrecho del cliente, para que el adaptador (I1) despache en
pocas lineas. Lista blanca de campos declarada aqui -- nunca facturacion,
propiedad ni tokens, y nunca el error crudo del proveedor (puede llevar
tokens o URL internas)."""

from __future__ import annotations

import asyncio
from collections.abc import Callable, Mapping, Sequence
from datetime import date
from typing import Any, Final, Protocol

from safent_ads.broker.platforms.error_sanitizer import redact_sdk_error
from safent_ads.shared.errors import InfrastructureError

__all__ = [
    "MetaAdLibraryClient",
    "MetaAdLibraryError",
    "MetaAdLibraryIdentityRequiredError",
    "map_ads_archive_row",
    "search_meta_ads_archive",
]

_MAX_ADS: Final = 50
_ACTIVE_STATUS: Final = "ACTIVE"
_ALL_STATUS: Final = "ALL"

# Campos oficiales de `/ads_archive` que se piden y se dejan pasar
# (developers.facebook.com/docs/graph-api/reference/ads_archive/): nunca
# `bylines`/`funding_entity` de facturacion ni ningun campo de token.
_REQUEST_FIELDS: Final = (
    "page_name",
    "ad_creative_bodies",
    "ad_creative_link_titles",
    "ad_snapshot_url",
    "ad_delivery_start_time",
    "ad_delivery_stop_time",
    "publisher_platforms",
    "estimated_audience_size",
)

class MetaAdLibraryError(InfrastructureError):
    """Fallo irrecuperable al hablar con `/ads_archive`, ya saneado."""


class MetaAdLibraryIdentityRequiredError(MetaAdLibraryError):
    """fix/ad-library-identity-reason: Meta's own documented response when
    the Facebook user behind the token has not completed the Ad Library
    identity/location confirmation (facebook.com/ID) -- the ONE upstream
    failure this module distinguishes by a static, already-sanitized
    status/code triplet (never a message), raised by
    `ComposioMetaAdLibraryClient` and left unwrapped by `_call` below so
    `broker/presentation/dispatcher.py` can map it to its own denial code."""


class MetaAdLibraryClient(Protocol):
    """Subconjunto de la API de la Biblioteca de Anuncios que el bróker
    necesita. En produccion nativa, un wrapper fino sobre el Graph API con
    la app de Meta ya conectada (`external_account_id` ignorado, el token
    es de APP); sobre el transporte Composio (companion, sin app nativa),
    un wrapper que SI necesita `external_account_id` -- la cuenta conectada
    de Meta de la empresa autentica la llamada aunque el nodo `ads_archive`
    en si no cuelgue de ninguna cuenta (`composio_sdk_clients.py::
    ComposioMetaAdLibraryClient`). En tests, un doble sencillo."""

    def search_ads_archive(
        self,
        *,
        country: str,
        search_terms: str | None,
        search_page_ids: str | None,
        active_status: str,
        fields: Sequence[str],
        limit: int,
        external_account_id: str | None = None,
    ) -> Sequence[Mapping[str, Any]]: ...


async def search_meta_ads_archive(
    client: MetaAdLibraryClient,
    *,
    country: str,
    search_terms: str | None,
    search_page_ids: str | None,
    active_only: bool,
    external_account_id: str | None = None,
) -> Sequence[dict[str, Any]]:
    """Sin paginacion automatica: una pagina, <= 50 anuncios. Devuelve ya
    mapeado a la forma cerrada del contrato (`map_ads_archive_row`), nunca
    la fila cruda del Graph API."""
    rows = await _call(
        lambda: client.search_ads_archive(
            country=country,
            search_terms=search_terms,
            search_page_ids=search_page_ids,
            active_status=_ACTIVE_STATUS if active_only else _ALL_STATUS,
            fields=_REQUEST_FIELDS,
            limit=_MAX_ADS,
            external_account_id=external_account_id,
        )
    )
    return [map_ads_archive_row(row) for row in rows[:_MAX_ADS]]


async def _call[ResultT](operation: Callable[[], ResultT]) -> ResultT:
    try:
        return await asyncio.to_thread(operation)
    except MetaAdLibraryIdentityRequiredError:
        # Ya es un tipo distinguible y saneado (ningun mensaje del
        # proveedor) -- envolverlo otra vez lo perderia antes de que
        # `broker/presentation/dispatcher.py` pueda mapearlo.
        raise
    except Exception as exc:  # noqa: BLE001 - frontera con el SDK, nunca fail-open
        raise MetaAdLibraryError(redact_sdk_error(exc)) from None


def map_ads_archive_row(row: Mapping[str, Any]) -> dict[str, Any]:
    """Fila cruda del Graph API -> forma cerrada y saneada. Solo los campos
    de `_REQUEST_FIELDS`; ningun otro campo del proveedor sale de aqui."""
    bodies = row.get("ad_creative_bodies") or []
    titles = row.get("ad_creative_link_titles") or []
    return {
        "advertiser_name": str(row.get("page_name", "")),
        "ad_text": str(bodies[0]) if bodies else (str(titles[0]) if titles else None),
        "image_url": None,
        "start_date": _optional_date(row.get("ad_delivery_start_time")),
        "stop_date": _optional_date(row.get("ad_delivery_stop_time")),
        "platforms": tuple(str(p) for p in (row.get("publisher_platforms") or [])),
        "reach_by_country": None,
    }


def _optional_date(value: Any) -> str | None:  # noqa: ANN401 - valor crudo del Graph API
    """ISO `YYYY-MM-DD`, nunca un `datetime.date` crudo: esta fila cruza el
    socket bróker<->api como JSON (`dispatcher._ok_response`), que no tiene
    tipo fecha nativo -- un `date` sin convertir revienta ese `json.dumps`
    con un `TypeError` sin manejar (`broker_competitor_research_port.py::
    _row_to_ad` hace el `date.fromisoformat` de vuelta al otro lado)."""
    if not value:
        return None
    return date.fromisoformat(str(value)[:10]).isoformat()

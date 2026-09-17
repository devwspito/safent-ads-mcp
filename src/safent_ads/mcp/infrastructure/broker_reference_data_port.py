"""`BrokerReferenceDataPort` real (R3/R4, historia 18): implementa a la vez
`MetaReferenceDataPort` y `GoogleReferenceDataPort` -- mismo patron que
`BrokerGaqlReadPort`/`BrokerSearchTermReadPort`, IDOR primero
(`resolve_owned_account_ref`), despues el bróker.

**Contrato de las dos ops nuevas que cablea I1** (`broker/presentation/
dispatcher.py` + `broker/platforms/meta_graph_reader.py` /
`google_reference_reader.py`):

- `meta_reference_read` (Meta, seis herramientas): peticion
  `{"op": "meta_reference_read", "business_id", "connection_id",
  "platform": "meta", "external_account_id", "tool": <nombre de la
  herramienta MCP>, "arguments": {...}}`; respuesta `{"rows": [...]}` ya
  filtrada por la lista blanca de campos de `meta_graph_reader.py`.
  `tool="list_meta_audiences"` fusiona `customaudiences`+`saved_audiences`
  en una sola lista, cada fila con `"kind": "custom"|"saved"`.
- `google_reference_read` (Google, solo `get_google_keyword_ideas`, la
  unica de las tres que no es GAQL): peticion
  `{"op": "google_reference_read", ..., "tool": "get_google_keyword_ideas",
  "arguments": {"seed_keywords", "geo_target", "language"}}`; respuesta
  `{"rows": [{"text", "avg_monthly_searches", "competition"}, ...]}`.
  `list_google_conversion_actions`/`search_google_constants` **no**
  estrenan op: van por `AdsPlatformPort.run_gaql`, ya cableado."""

from __future__ import annotations

import json
import time
from collections.abc import Awaitable, Callable, Mapping, Sequence
from pathlib import Path
from typing import Any

import structlog
from cachetools import TLRUCache
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from safent_ads.accounts.application.ports import AdsPlatformPort
from safent_ads.accounts.infrastructure.broker_client import BrokerSocketClient
from safent_ads.broker.platforms.google_reference_reader import (
    build_conversion_actions_query,
    build_google_constants_query,
)
from safent_ads.mcp.application.reference_data_port import (
    GoogleConstant,
    GoogleConstantKind,
    GoogleConversionAction,
    GoogleKeywordIdea,
    MetaAudience,
    MetaCatalog,
    MetaPage,
    MetaPixel,
    MetaReachEstimate,
    MetaTargetingKind,
    MetaTargetingSuggestion,
)
from safent_ads.mcp.infrastructure.account_ownership import resolve_owned_account_ref
from safent_ads.mcp.infrastructure.broker_call import call_broker

__all__ = ["BrokerReferenceDataPort", "MetaReferenceDataBrokerClient"]

logger = structlog.get_logger(__name__)

_MAX_GAQL_ROWS = 200

# Perf (16-sep, item 3): medido a traves del MCP,
# `search_meta_targeting`/`get_google_keyword_ideas` tardan 2.8-7.1 s por
# llamada y las lecturas de catalogo (paginas/pixeles/audiencias/catalogos)
# una franja similar -- llamadas identicas repetidas pagan el viaje
# completo al proveedor cada vez. `_CACHE_TTL_SECONDS_BY_TOOL` es la unica
# fuente de la politica de cacheo: una herramienta ausente de este dict
# nunca se cachea (`list_google_conversion_actions` entre ellas).
_TEN_MINUTES_SECONDS = 10 * 60.0
_ONE_HOUR_SECONDS = 60 * 60.0
_ONE_DAY_SECONDS = 24 * 60 * 60.0

_CACHE_TTL_SECONDS_BY_TOOL: dict[str, float] = {
    "list_meta_pages": _TEN_MINUTES_SECONDS,
    "list_meta_pixels": _TEN_MINUTES_SECONDS,
    "list_meta_audiences": _TEN_MINUTES_SECONDS,
    "list_meta_catalogs": _TEN_MINUTES_SECONDS,
    "get_meta_reach_estimate": _ONE_HOUR_SECONDS,
    "search_meta_targeting": _ONE_DAY_SECONDS,
    "get_google_keyword_ideas": _ONE_DAY_SECONDS,
    "search_google_constants": _ONE_DAY_SECONDS,
}

_CACHE_MAX_ENTRIES = 2000

_CacheKey = tuple[str, str, str, str]
_Rows = Sequence[Mapping[str, Any]]


def _cache_key(
    business_id: str, account_ref: str, tool: str, arguments: Mapping[str, Any]
) -> _CacheKey:
    canonical_arguments = json.dumps(dict(arguments), sort_keys=True, separators=(",", ":"))
    return (business_id, account_ref, tool, canonical_arguments)


def _cache_ttu(key: _CacheKey, _rows: _Rows, now: float) -> float:
    _business_id, _account_ref, tool, _arguments = key
    return now + _CACHE_TTL_SECONDS_BY_TOOL.get(tool, 0.0)


def _always_cacheable(_rows: _Rows) -> bool:
    return True


def _reach_estimate_rows_are_cacheable(rows: _Rows) -> bool:
    """Nunca cachea una estimacion de alcance sin terminar: la Graph API
    de Meta calcula `get_meta_reach_estimate` de forma asincrona y
    `estimate_ready=False` es una respuesta valida y transitoria, no un
    error -- cachearla dejaria "no listo" congelado durante una hora."""
    return bool(rows) and bool(rows[0].get("estimate_ready", False))


_IS_CACHEABLE_BY_TOOL: dict[str, Callable[[_Rows], bool]] = {
    "get_meta_reach_estimate": _reach_estimate_rows_are_cacheable,
}


def _scope_id(value: object) -> str | None:
    """El dispatcher del broker (`_request_scope`) exige `connection_id`
    para resolver la credencial de produccion; sin el, el scope queda en
    None y el store deniega CREDENTIAL_NOT_CONNECTED aunque la conexion
    de Composio este ACTIVA (incidente 2026-09-15, companion 0.2.25)."""
    return None if value is None else str(value)


class MetaReferenceDataBrokerClient(BrokerSocketClient):
    def __init__(self, socket_path: Path) -> None:
        super().__init__(socket_path)

    async def read(
        self,
        *,
        business_id: str,
        connection_id: str | None,
        external_account_id: str,
        tool: str,
        arguments: dict[str, Any],
    ) -> list[dict[str, Any]]:
        result = await self._request(
            {
                "op": "meta_reference_read",
                "business_id": business_id,
                "connection_id": connection_id,
                "platform": "meta",
                "external_account_id": external_account_id,
                "tool": tool,
                "arguments": arguments,
            },
            retryable=True,
        )
        return list(result["rows"])


class GoogleKeywordIdeaBrokerClient(BrokerSocketClient):
    def __init__(self, socket_path: Path) -> None:
        super().__init__(socket_path)

    async def read(
        self,
        *,
        business_id: str,
        connection_id: str | None,
        external_account_id: str,
        arguments: dict[str, Any],
    ) -> list[dict[str, Any]]:
        result = await self._request(
            {
                "op": "google_reference_read",
                "business_id": business_id,
                "connection_id": connection_id,
                "platform": "google",
                "external_account_id": external_account_id,
                "tool": "get_google_keyword_ideas",
                "arguments": arguments,
            },
            retryable=True,
        )
        return list(result["rows"])


class BrokerReferenceDataPort:
    def __init__(
        self,
        meta_client: MetaReferenceDataBrokerClient,
        google_keyword_client: GoogleKeywordIdeaBrokerClient,
        ads_platform_port: AdsPlatformPort,
        session_factory: async_sessionmaker[AsyncSession],
        *,
        cache_timer: Callable[[], float] = time.monotonic,
    ) -> None:
        self._meta_client = meta_client
        self._google_keyword_client = google_keyword_client
        self._ads_platform_port = ads_platform_port
        self._session_factory = session_factory
        # Un unico proceso construye este puerto una vez (`composition/
        # app.py::_build_reference_data_tool_services`), asi que la cache
        # vive tanto como el proceso -- acotada y con expulsion LRU para
        # no crecer sin limite con negocios/cuentas nuevos. `cache_timer`
        # es un punto de inyeccion para pruebas deterministas de TTL
        # (produccion siempre usa `time.monotonic`, el valor por defecto).
        self._cache: TLRUCache[_CacheKey, _Rows] = TLRUCache(
            maxsize=_CACHE_MAX_ENTRIES, ttu=_cache_ttu, timer=cache_timer
        )

    # --- Meta (R3) --------------------------------------------------------

    async def list_meta_pages(self, business_id: str, account_ref: str) -> tuple[MetaPage, ...]:
        rows = await self._read_meta(business_id, account_ref, "list_meta_pages", {})
        return tuple(
            MetaPage(
                page_id=str(row["id"]),
                name=str(row["name"]),
                instagram_business_account_id=_optional_nested_id(
                    row.get("instagram_business_account")
                ),
            )
            for row in rows
        )

    async def list_meta_pixels(self, business_id: str, account_ref: str) -> tuple[MetaPixel, ...]:
        rows = await self._read_meta(business_id, account_ref, "list_meta_pixels", {})
        return tuple(
            MetaPixel(
                pixel_id=str(row["id"]),
                name=str(row["name"]),
                last_fired_event_names=tuple(row.get("last_fired_event_names") or ()),
            )
            for row in rows
        )

    async def list_meta_audiences(
        self, business_id: str, account_ref: str
    ) -> tuple[MetaAudience, ...]:
        rows = await self._read_meta(business_id, account_ref, "list_meta_audiences", {})
        return tuple(
            MetaAudience(
                audience_id=str(row["id"]),
                name=str(row["name"]),
                kind=str(row["kind"]),
                approximate_count_upper_bound=_optional_int(
                    row.get("approximate_count_upper_bound")
                ),
            )
            for row in rows
        )

    async def list_meta_catalogs(
        self, business_id: str, account_ref: str
    ) -> tuple[MetaCatalog, ...]:
        rows = await self._read_meta(business_id, account_ref, "list_meta_catalogs", {})
        return tuple(
            MetaCatalog(
                catalog_id=str(row["id"]),
                name=str(row["name"]),
                product_count=_optional_int(row.get("product_count")),
            )
            for row in rows
        )

    async def search_meta_targeting(
        self, business_id: str, account_ref: str, *, kind: MetaTargetingKind, query: str
    ) -> tuple[MetaTargetingSuggestion, ...]:
        rows = await self._read_meta(
            business_id,
            account_ref,
            "search_meta_targeting",
            {"kind": kind.value, "query": query},
        )
        return tuple(
            MetaTargetingSuggestion(
                targeting_id=str(row["id"]),
                name=str(row["name"]),
                audience_size_lower_bound=_optional_int(row.get("audience_size_lower_bound")),
                audience_size_upper_bound=_optional_int(row.get("audience_size_upper_bound")),
            )
            for row in rows
        )

    async def get_meta_reach_estimate(
        self,
        business_id: str,
        account_ref: str,
        *,
        optimization_goal: str,
        countries: tuple[str, ...],
    ) -> MetaReachEstimate:
        rows = await self._read_meta(
            business_id,
            account_ref,
            "get_meta_reach_estimate",
            {"optimization_goal": optimization_goal, "countries": list(countries)},
        )
        row = rows[0] if rows else {}
        return MetaReachEstimate(
            users_lower_bound=_optional_int(row.get("users_lower_bound")),
            users_upper_bound=_optional_int(row.get("users_upper_bound")),
            estimate_ready=bool(row.get("estimate_ready", False)),
        )

    async def _read_meta(
        self, business_id: str, account_ref: str, tool: str, arguments: dict[str, Any]
    ) -> _Rows:
        ref = await resolve_owned_account_ref(self._session_factory, business_id, account_ref)

        async def fetch() -> _Rows:
            return await call_broker(
                self._meta_client.read(
                    business_id=business_id,
                    connection_id=_scope_id(ref.connection_id),
                    external_account_id=ref.external_account_id,
                    tool=tool,
                    arguments=arguments,
                )
            )

        return await self._cached_read(
            business_id=business_id,
            account_ref=account_ref,
            tool=tool,
            arguments=arguments,
            fetch=fetch,
            is_cacheable=_IS_CACHEABLE_BY_TOOL.get(tool, _always_cacheable),
        )

    async def _cached_read(
        self,
        *,
        business_id: str,
        account_ref: str,
        tool: str,
        arguments: Mapping[str, Any],
        fetch: Callable[[], Awaitable[_Rows]],
        is_cacheable: Callable[[_Rows], bool] = _always_cacheable,
    ) -> _Rows:
        """Cachea por (negocio, cuenta, herramienta, argumentos canonicos) --
        `_CACHE_TTL_SECONDS_BY_TOOL` es la unica fuente de que se cachea y
        cuanto. Nunca cachea un fallo: una excepcion de `fetch()` se
        propaga antes de llegar a `self._cache[key] = rows`."""
        if tool not in _CACHE_TTL_SECONDS_BY_TOOL:
            return await fetch()
        key = _cache_key(business_id, account_ref, tool, arguments)
        cached = self._cache.get(key)
        if cached is not None:
            logger.debug("reference_cache_hit", tool=tool, business_id=business_id)
            return cached
        rows = await fetch()
        if is_cacheable(rows):
            self._cache[key] = rows
        return rows

    # --- Google (R4) --------------------------------------------------------

    async def list_google_conversion_actions(
        self, business_id: str, account_ref: str
    ) -> tuple[GoogleConversionAction, ...]:
        ref = await resolve_owned_account_ref(self._session_factory, business_id, account_ref)
        rows = await call_broker(
            self._ads_platform_port.run_gaql(
                ref, build_conversion_actions_query(), max_rows=_MAX_GAQL_ROWS
            )
        )
        return tuple(
            GoogleConversionAction(
                resource_name=str(row["conversion_action.resource_name"]),
                name=str(row["conversion_action.name"]),
                category=str(row["conversion_action.category"]),
                status=str(row["conversion_action.status"]),
            )
            for row in rows
        )

    async def search_google_constants(
        self,
        business_id: str,
        account_ref: str,
        *,
        kind: GoogleConstantKind,
        query: str,
        country: str | None,
    ) -> tuple[GoogleConstant, ...]:
        ref = await resolve_owned_account_ref(self._session_factory, business_id, account_ref)
        query_text = build_google_constants_query(kind=kind, query=query, country=country)
        arguments = {"kind": kind.value, "query": query, "country": country}

        async def fetch() -> _Rows:
            return await call_broker(
                self._ads_platform_port.run_gaql(ref, query_text, max_rows=_MAX_GAQL_ROWS)
            )

        rows = await self._cached_read(
            business_id=business_id,
            account_ref=account_ref,
            tool="search_google_constants",
            arguments=arguments,
            fetch=fetch,
        )
        resource, code_field = _CONSTANT_FIELDS_BY_KIND[kind]
        return tuple(
            GoogleConstant(
                resource_name=str(row[f"{resource}.resource_name"]),
                name=str(row[f"{resource}.name"]),
                code=str(row[f"{resource}.{code_field}"]),
            )
            for row in rows
        )

    async def get_google_keyword_ideas(
        self,
        business_id: str,
        account_ref: str,
        *,
        seed_keywords: tuple[str, ...],
        geo_target: str,
        language: str,
    ) -> tuple[GoogleKeywordIdea, ...]:
        ref = await resolve_owned_account_ref(self._session_factory, business_id, account_ref)
        arguments = {
            "seed_keywords": list(seed_keywords),
            "geo_target": geo_target,
            "language": language,
        }

        async def fetch() -> _Rows:
            return await call_broker(
                self._google_keyword_client.read(
                    business_id=business_id,
                    connection_id=_scope_id(ref.connection_id),
                    external_account_id=ref.external_account_id,
                    arguments=arguments,
                )
            )

        rows = await self._cached_read(
            business_id=business_id,
            account_ref=account_ref,
            tool="get_google_keyword_ideas",
            arguments=arguments,
            fetch=fetch,
        )
        return tuple(
            GoogleKeywordIdea(
                text=str(row["text"]),
                avg_monthly_searches=_optional_int(row.get("avg_monthly_searches")),
                competition=(
                    str(row["competition"]) if row.get("competition") is not None else None
                ),
            )
            for row in rows
        )


_CONSTANT_FIELDS_BY_KIND: dict[GoogleConstantKind, tuple[str, str]] = {
    GoogleConstantKind.GEO_TARGET: ("geo_target_constant", "country_code"),
    GoogleConstantKind.LANGUAGE: ("language_constant", "code"),
}


def _optional_int(value: Any) -> int | None:  # noqa: ANN401 - valor crudo del bróker
    return None if value is None else int(value)


def _optional_nested_id(value: Any) -> str | None:  # noqa: ANN401 - valor crudo del Graph API
    if isinstance(value, dict) and "id" in value:
        return str(value["id"])
    return None

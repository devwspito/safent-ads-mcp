"""B-3 (revision de seguridad de las herramientas sensibles del MCP): el
bróker vuelve a aplicar la MISMA política de `get_meta_graph` que
`mcp/domain/meta_graph_path.py` (lado ads-api) antes de tocar el
adaptador -- TB-4 (defensa en profundidad, ningun salto confia en que el
otro ya valido). Copia deliberada, no importada: son dos procesos
distintos y bounded contexts distintos (el bróker nunca depende de
`mcp/`); una prueba de paridad de tablas (`test_reference_and_graph_ops_
dispatch.py`) impide que las dos tablas diverjan sin que salte en CI.

Puro: sin I/O, sin framework."""

from __future__ import annotations

import json
import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import date
from typing import Any, Final

from safent_ads.shared.errors import DomainError

__all__ = [
    "GraphPolicyDeniedError",
    "TruncatedGraphRows",
    "truncate_graph_response",
    "validate_graph_edge",
    "validate_graph_fields",
    "validate_graph_node_shape",
    "validate_graph_params",
]

_MAX_ROWS: Final = 200
_MAX_RESPONSE_BYTES: Final = 64 * 1024
_MAX_PARAM_KEYS: Final = 10
_MAX_PARAM_VALUE_LENGTH: Final = 256
_MAX_SINCE_UNTIL_DAYS: Final = 400

_NODE_PATTERN: Final = re.compile(r"^(act_)?\d+$")
_PARAM_KEY_PATTERN: Final = re.compile(r"^[a-z_]{1,32}$")

_DENY_PARAM_KEYS: Final = frozenset(
    {
        "fields",
        "ids",
        "method",
        "access_token",
        "after",
        "before",
        "limit",
        "format",
        "callback",
        "redirect",
        "appsecret_proof",
    }
)

_NODE_ITSELF_FIELDS: Final = frozenset({"id", "name", "status", "account_id"})

_FIELDS_BY_EDGE: Final[dict[str, frozenset[str]]] = {
    "": _NODE_ITSELF_FIELDS,
    "promote_pages": frozenset({"id", "name", "instagram_business_account"}),
    "instagram_accounts": frozenset({"id", "username"}),
    "adspixels": frozenset({"id", "name", "last_fired_time"}),
    "customaudiences": frozenset({"id", "name", "subtype", "approximate_count_upper_bound"}),
    "saved_audiences": frozenset({"id", "name", "approximate_count_upper_bound"}),
    "product_catalogs": frozenset({"id", "name", "product_count"}),
    "campaigns": frozenset(
        {"id", "name", "status", "objective", "daily_budget", "lifetime_budget"}
    ),
    "adsets": frozenset({"id", "name", "status", "daily_budget", "lifetime_budget"}),
    "ads": frozenset({"id", "name", "status"}),
    "adcreatives": frozenset({"id", "name", "status", "object_story_spec", "thumbnail_url"}),
    "adimages": frozenset({"id", "name", "hash", "url", "width", "height"}),
    "advideos": frozenset({"id", "title", "description", "source", "length"}),
    "insights": frozenset(
        {
            "date_start",
            "date_stop",
            "spend",
            "impressions",
            "clicks",
            "reach",
            "frequency",
            "actions",
            "action_values",
            "ctr",
            "cpc",
            "cpm",
        }
    ),
    "adrules_library": frozenset({"id", "name", "status", "evaluation_spec", "execution_spec"}),
}
_ALLOWED_EDGES: Final = frozenset(_FIELDS_BY_EDGE)

_DENY_EXACT_FIELDS: Final = frozenset(
    {
        "access_token",
        "page_access_token",
        "business",
        "users",
        "assigned_users",
        "agencies",
        "io_number",
        "tos_accepted",
    }
)
_DENY_FIELD_PREFIXES: Final = (
    "owner",
    "funding_source",
    "credit",
    "extended_credit",
    "billing",
    "user_",
)
_DENY_FIELD_SUFFIXES: Final = ("_token",)


class GraphPolicyDeniedError(DomainError):
    """`edge`, `fields`, `node` o `params` no respetan la politica del paso
    a traves de `get_meta_graph` -- se deniega sin detalle (TB-4)."""


@dataclass(frozen=True, slots=True)
class TruncatedGraphRows:
    rows: tuple[Mapping[str, Any], ...]
    truncated: bool


def validate_graph_edge(edge: str) -> None:
    if edge not in _ALLOWED_EDGES:
        raise GraphPolicyDeniedError(f"arista no permitida: {edge!r}")


def validate_graph_fields(edge: str, fields: Sequence[str]) -> None:
    allowed = _FIELDS_BY_EDGE.get(edge, frozenset())
    for field in fields:
        if _is_field_denied(field):
            raise GraphPolicyDeniedError(f"campo no permitido: {field!r}")
        if field not in allowed:
            raise GraphPolicyDeniedError(f"campo fuera de la lista blanca de {edge!r}: {field!r}")


def _is_field_denied(field: str) -> bool:
    if field in _DENY_EXACT_FIELDS:
        return True
    if field.startswith(_DENY_FIELD_PREFIXES):
        return True
    return field.endswith(_DENY_FIELD_SUFFIXES)


def validate_graph_node_shape(node: str) -> None:
    if not _NODE_PATTERN.match(node):
        raise GraphPolicyDeniedError(f"node debe ser numerico o act_<numerico>: {node!r}")


def validate_graph_params(params: Mapping[str, Any], *, today: date) -> None:
    if len(params) > _MAX_PARAM_KEYS:
        raise GraphPolicyDeniedError(f"demasiadas claves en params: {len(params)}")
    for key, value in params.items():
        _validate_param_key(key)
        _validate_param_value(key, value, today=today)


def _validate_param_key(key: str) -> None:
    if not _PARAM_KEY_PATTERN.match(key):
        raise GraphPolicyDeniedError(f"clave de params invalida: {key!r}")
    if key in _DENY_PARAM_KEYS or "token" in key:
        raise GraphPolicyDeniedError(f"clave de params no permitida: {key!r}")


def _validate_param_value(key: str, value: Any, *, today: date) -> None:  # noqa: ANN401
    # M-2: solo escalares -- un dict/lista sin techo era una via abierta
    # para expansion anidada o payloads sin cota.
    if not isinstance(value, str | int | float | bool):
        raise GraphPolicyDeniedError(f"valor de params no escalar: {key!r}")
    if len(str(value)) > _MAX_PARAM_VALUE_LENGTH:
        raise GraphPolicyDeniedError(f"valor de params demasiado largo: {key!r}")
    if key in {"since", "until"}:
        _validate_since_until(key, value, today=today)


def _validate_since_until(key: str, value: Any, *, today: date) -> None:  # noqa: ANN401
    try:
        parsed = date.fromisoformat(str(value))
    except ValueError as exc:
        raise GraphPolicyDeniedError(f"{key} debe ser una fecha ISO: {value!r}") from exc
    if abs((today - parsed).days) > _MAX_SINCE_UNTIL_DAYS:
        raise GraphPolicyDeniedError(f"{key} fuera de los {_MAX_SINCE_UNTIL_DAYS} dias permitidos")


def truncate_graph_response(rows: Sequence[Mapping[str, Any]]) -> TruncatedGraphRows:
    """A-1: recorte tambien en el bróker, no solo en el cliente MCP -- una
    pagina, <= 200 filas, <= 64 KiB."""
    capped = list(rows[:_MAX_ROWS])
    truncated = len(rows) > _MAX_ROWS
    while capped and len(json.dumps(capped).encode("utf-8")) > _MAX_RESPONSE_BYTES:
        capped = capped[:-1]
        truncated = True
    return TruncatedGraphRows(rows=tuple(capped), truncated=truncated)

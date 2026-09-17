"""004 tasks-2.md R5 (historia 19): unica fuente de verdad de la politica
de `get_meta_graph`, el paso a traves de lectura de Meta -- la superficie
mas ancha del incremento, por eso la mas estrechamente declarada
(S-1, `security-engineer` firma antes de fusionar).

Puro: sin I/O, sin framework. Todo lo que necesita comprobacion contra la
plataforma real (que un `node` numerico pertenezca de verdad a la cuenta)
vive en infraestructura -- este modulo solo decide, a partir de literales,
que esta permitido y que no.

Lista blanca de aristas: las 6 de R3 (mismos campos, declarados de nuevo
aqui a proposito -- este modulo no importa de `broker/platforms`, otro
contexto acotado; DRY cede ante el aislamiento de capas) mas 8 aristas mas
anchas del Graph (campanas, conjuntos, anuncios, creatividades, imagenes,
videos, informes y reglas). Lista negra de campos que gana siempre, sin
importar la arista."""

from __future__ import annotations

import json
import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import date
from typing import Any, Final

from safent_ads.shared.errors import DomainError

__all__ = [
    "GraphEdgeDeniedError",
    "GraphFieldDeniedError",
    "GraphNodeFormatError",
    "GraphParamsDeniedError",
    "TruncatedRows",
    "node_is_account_itself",
    "truncate_response",
    "validate_edge",
    "validate_fields",
    "validate_node_shape",
    "validate_params",
]

_MAX_ROWS: Final = 200
_MAX_RESPONSE_BYTES: Final = 64 * 1024
_MAX_PARAM_KEYS: Final = 10
_MAX_PARAM_VALUE_LENGTH: Final = 256
_MAX_SINCE_UNTIL_DAYS: Final = 400

_NODE_PATTERN: Final = re.compile(r"^(act_)?\d+$")
_PARAM_KEY_PATTERN: Final = re.compile(r"^[a-z_]{1,32}$")

# B-1: lista blanca cerrada para `params` -- ninguna de estas claves puede
# venir del llamante, gane o no la forma general de la clave. `fields` es la
# mas critica: sin esto, `params={"fields": "id,access_token"}` pisaba la
# lista blanca de campos de la arista (ver `LiveMetaGraphClient.get_edge`).
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

# El propio nodo (arista vacia): campos genericos, suficientes para
# identificarlo y para la comprobacion de propiedad (`account_id`).
_NODE_ITSELF_FIELDS: Final = frozenset({"id", "name", "status", "account_id"})

# Las 6 aristas de R3, mismos campos que `meta_graph_reader.py` (duplicado
# a proposito, ver docstring del modulo).
_FIELDS_BY_EDGE: Final[dict[str, frozenset[str]]] = {
    "": _NODE_ITSELF_FIELDS,
    "promote_pages": frozenset({"id", "name", "instagram_business_account"}),
    "instagram_accounts": frozenset({"id", "username"}),
    "adspixels": frozenset({"id", "name", "last_fired_time"}),
    "customaudiences": frozenset({"id", "name", "subtype", "approximate_count_upper_bound"}),
    "saved_audiences": frozenset({"id", "name", "approximate_count_upper_bound"}),
    "product_catalogs": frozenset({"id", "name", "product_count"}),
    # 8 aristas mas anchas (R5): campana -> anuncio, creatividad, informe, regla.
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

# Lista negra que gana siempre (R5): ningun campo de token, facturacion,
# propiedad o usuarios asignados sale nunca, aunque la arista lo permita.
# `customer.test_account`-style excepciones no aplican aqui (eso es GAQL,
# R6); esta lista es de campos del Graph API de Meta.
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


class GraphEdgeDeniedError(DomainError):
    """`edge` no esta en la lista blanca cerrada de `get_meta_graph`."""


class GraphFieldDeniedError(DomainError):
    """Un campo pedido esta en la lista negra, o no esta en la lista
    blanca de la arista pedida."""


class GraphNodeFormatError(DomainError):
    """`node` no es un id numerico ni `act_<numerico>`."""


class GraphParamsDeniedError(DomainError):
    """`params` no respeta la forma segura (claves, cuenta, longitud,
    rango de fechas)."""


@dataclass(frozen=True, slots=True)
class TruncatedRows:
    rows: tuple[Mapping[str, Any], ...]
    truncated: bool


def validate_edge(edge: str) -> None:
    # Bj-3: nunca se repite `edge` en el mensaje -- llega ya acotado a
    # `[a-z_]{0,40}` por el borde pydantic, pero el mensaje de error no debe
    # depender de esa cota para ser seguro de mostrar.
    if edge not in _ALLOWED_EDGES:
        raise GraphEdgeDeniedError("arista no permitida")


def validate_fields(edge: str, fields: Sequence[str]) -> None:
    """`edge` ya debe haber pasado `validate_edge`. La lista negra gana
    siempre, incluso si la arista la permitiera."""
    allowed = _FIELDS_BY_EDGE[edge]
    for field in fields:
        if _is_field_denied(field):
            raise GraphFieldDeniedError(f"campo no permitido: {field!r}")
        if field not in allowed:
            raise GraphFieldDeniedError(f"campo fuera de la lista blanca de {edge!r}: {field!r}")


def _is_field_denied(field: str) -> bool:
    if field in _DENY_EXACT_FIELDS:
        return True
    if field.startswith(_DENY_FIELD_PREFIXES):
        return True
    return field.endswith(_DENY_FIELD_SUFFIXES)


def validate_node_shape(node: str) -> None:
    if not _NODE_PATTERN.match(node):
        raise GraphNodeFormatError(f"node debe ser numerico o act_<numerico>: {node!r}")


def node_is_account_itself(node: str, account_external_id: str) -> bool:
    """Comparacion pura y sindical -- la comprobacion de propiedad real de
    un `node` que NO es la cuenta (un id numerico suelto) exige resolverlo
    contra la plataforma (I/O, fuera de este modulo): ver docstring del
    modulo, "un nodo que no pertenezca a la cuenta => ENTITY_NOT_FOUND"."""
    return node == account_external_id


def validate_params(params: Mapping[str, Any], *, today: date) -> None:
    if len(params) > _MAX_PARAM_KEYS:
        raise GraphParamsDeniedError(f"demasiadas claves en params: {len(params)}")
    for key, value in params.items():
        _validate_param_key(key)
        _validate_param_value(key, value, today=today)


def _validate_param_key(key: str) -> None:
    if not _PARAM_KEY_PATTERN.match(key):
        raise GraphParamsDeniedError(f"clave de params invalida: {key!r}")
    if key in _DENY_PARAM_KEYS or "token" in key:
        raise GraphParamsDeniedError(f"clave de params no permitida: {key!r}")


def _validate_param_value(key: str, value: Any, *, today: date) -> None:  # noqa: ANN401
    # M-2: solo escalares -- un dict/lista sin techo era una via abierta
    # para expansion anidada (`campo{subcampo}`-style) o payloads sin cota.
    if not isinstance(value, str | int | float | bool):
        raise GraphParamsDeniedError(f"valor de params no escalar: {key!r}")
    if len(str(value)) > _MAX_PARAM_VALUE_LENGTH:
        raise GraphParamsDeniedError(f"valor de params demasiado largo: {key!r}")
    if key in {"since", "until"}:
        _validate_since_until(key, value, today=today)


def _validate_since_until(key: str, value: Any, *, today: date) -> None:  # noqa: ANN401
    try:
        parsed = date.fromisoformat(str(value))
    except ValueError as exc:
        raise GraphParamsDeniedError(f"{key} debe ser una fecha ISO: {value!r}") from exc
    if abs((today - parsed).days) > _MAX_SINCE_UNTIL_DAYS:
        raise GraphParamsDeniedError(f"{key} fuera de los {_MAX_SINCE_UNTIL_DAYS} dias permitidos")


def truncate_response(rows: Sequence[Mapping[str, Any]]) -> TruncatedRows:
    """Sin paginacion automatica: una pagina, <= 200 filas, <= 64 KiB.
    Ninguna de las dos comprobaciones se salta a la otra."""
    capped = list(rows[:_MAX_ROWS])
    truncated = len(rows) > _MAX_ROWS
    while capped and len(json.dumps(capped).encode("utf-8")) > _MAX_RESPONSE_BYTES:
        capped = capped[:-1]
        truncated = True
    return TruncatedRows(rows=tuple(capped), truncated=truncated)

"""Validador de `run_gaql` (contracts/platform-port.md: "debe empezar por
`SELECT`, prohibidos `mutate`, `INSERT`, `UPDATE`, `REMOVE`, y hay limite de
filas. Es lectura, no una via de escritura encubierta.").

Defensa en profundidad contra prompt injection (threat-model.md T-1): un
termino de busqueda o comentario hostil que intente colar una mutacion via
la cadena GAQL se rechaza aqui, antes de tocar el SDK.

004 tasks-2.md R6: `run_gaql` es el unico acceso de lectura libre a Google
Ads (H-3, no se registra `google_ads_query`) -- este es el ultimo guardian
antes del SDK (`ads-api` puede estar comprometido), por eso el rechazo vive
aqui y no solo en `presentation/args.py::RunGaqlArgs`. Se anaden recursos de
lectura de referencia (`conversion_action`, `language_constant`,
`customer_client`, `ad_group_ad_asset_view`, R4/R18) y una lista negra de
campos de facturacion/token que gana siempre, sin importar el recurso.

tasks.md T032 (ME-2/I-1): la lista blanca
gana `asset_group`, `asset_group_asset`, `campaign_asset`, `ad_group_asset`
y `campaign_conversion_goal` -- los recursos que el broker relee para
confirmar el grupo de recursos y las metas de conversion de Maximo
Rendimiento/Demand Gen. `audience` se deja fuera **a proposito**: v1 no usa
segmentos, y ese recurso expondria nombres de listas de clientes. La lista
negra de campos gana `user_list`/`audience`/`customer_client` descriptivo
por la misma razon."""

from __future__ import annotations

import re
from typing import Final

from safent_ads.broker.platforms.errors import GaqlValidationError

_MAX_QUERY_LENGTH: Final = 4_000

_FORBIDDEN_KEYWORDS: Final = (
    "mutate",
    "insert",
    "update",
    "remove",
    "delete",
    "drop",
    "alter",
    "create",
    "truncate",
)
_FORBIDDEN_KEYWORD_PATTERN: Final = re.compile(
    r"\b(" + "|".join(_FORBIDDEN_KEYWORDS) + r")\b", re.IGNORECASE
)
_SELECT_PREFIX_PATTERN: Final = re.compile(r"^\s*select\b", re.IGNORECASE)
_FROM_RESOURCE_PATTERN: Final = re.compile(r"(?i)\bfrom\s+([a-z_][a-z0-9_]*)")

_ALLOWED_RESOURCES: Final = frozenset(
    {
        "customer",
        "customer_client",
        "campaign",
        "campaign_budget",
        "campaign_criterion",
        "geo_target_constant",
        "language_constant",
        "conversion_action",
        "ad_group",
        "ad_group_ad",
        "ad_group_ad_asset_view",
        "ad_group_criterion",
        "keyword_view",
        "search_term_view",
        "asset",
        "asset_group",
        "asset_group_asset",
        "campaign_asset",
        "ad_group_asset",
        "campaign_conversion_goal",
    }
)

# Lista negra de campos que gana siempre, sin importar el recurso o si el
# nombre aparece en SELECT/WHERE/ORDER BY (R6): facturacion, medios de pago
# y credenciales nunca deben poder leerse por `run_gaql`, ni siquiera de
# forma indirecta via un alias. `customer.test_account` NO esta en esta
# lista a proposito (R6: "ese si" es un campo de diagnostico inofensivo).
_FORBIDDEN_FIELD_PATTERNS: Final = (
    re.compile(r"customer\.pay\w*", re.IGNORECASE),
    re.compile(r"payments_account", re.IGNORECASE),
    re.compile(r"billing", re.IGNORECASE),
    re.compile(r"[a-z_]*_token\b", re.IGNORECASE),
    re.compile(r"customer_user_access\w*", re.IGNORECASE),
    # T032 (ME-2/I-1): segmentos de publico y listas de clientes no se leen
    # nunca por `run_gaql`, ni siquiera como campo de un recurso permitido.
    re.compile(r"user_list\w*", re.IGNORECASE),
    re.compile(r"audience\w*", re.IGNORECASE),
    re.compile(r"customer_client\.\w*descriptive", re.IGNORECASE),
)


def validate_gaql(query: str) -> None:
    """Lanza `GaqlValidationError` si `query` no es una lectura segura.
    No modifica `query`; el llamante decide como ejecutarla."""
    if len(query) > _MAX_QUERY_LENGTH:
        raise GaqlValidationError(f"consulta demasiado larga: {len(query)} caracteres")
    if not _SELECT_PREFIX_PATTERN.match(query):
        raise GaqlValidationError("la consulta debe empezar por SELECT")
    _reject_multiple_statements(query)
    _reject_forbidden_keywords(query)
    _reject_forbidden_fields(query)
    _require_allowed_resource(query)


def _reject_multiple_statements(query: str) -> None:
    statements = [stmt for stmt in query.split(";") if stmt.strip()]
    if len(statements) > 1:
        raise GaqlValidationError("una sola sentencia por consulta")


def _reject_forbidden_keywords(query: str) -> None:
    match = _FORBIDDEN_KEYWORD_PATTERN.search(query)
    if match is not None:
        raise GaqlValidationError(f"palabra prohibida en la consulta: {match.group(1)!r}")


def _reject_forbidden_fields(query: str) -> None:
    for pattern in _FORBIDDEN_FIELD_PATTERNS:
        match = pattern.search(query)
        if match is not None:
            raise GaqlValidationError(f"campo prohibido en la consulta: {match.group(0)!r}")


def _require_allowed_resource(query: str) -> None:
    match = _FROM_RESOURCE_PATTERN.search(query)
    if match is None:
        raise GaqlValidationError("no se encontro clausula FROM")
    resource = match.group(1).lower()
    if resource not in _ALLOWED_RESOURCES:
        raise GaqlValidationError(f"recurso fuera de la lista blanca: {resource!r}")

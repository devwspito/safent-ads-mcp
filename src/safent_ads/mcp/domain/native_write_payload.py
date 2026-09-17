"""Validador puro del `payload` de `propose_native_write` (004 tasks-2.md
W3, historia 20). Puro y sin estado: ninguna dependencia de framework,
transporte o persistencia -- el mismo criterio que el resto de
`mcp/domain/`.

Tres listas cierran la superficie, en orden de aplicacion:

1. Tamano/forma: ``<= 8 KiB`` serializado, profundidad ``<= 4``, claves
   ``^[a-z_]{1,40}$``, valores escalares o listas de escalares.
2. Lista negra que gana siempre: cualquier clave de token/credencial,
   `special_ad_categories`, `status`, `users`, `owner*`, `funding_source*`,
   `billing*`.
3. Presupuesto y puja se rechazan a proposito (D-4, `contracts/mcp.md`):
   el evaluador de topes del bróker sabe leer un `Money` tipado
   (`propose_budget_change`/`propose_bid_target`), nunca un payload libre.
"""

from __future__ import annotations

import json
import re
from collections.abc import Mapping, Sequence

__all__ = ["NativeWritePayloadError", "validate_native_write_payload"]

_MAX_PAYLOAD_BYTES = 8 * 1024
_MAX_DEPTH = 4
_KEY_PATTERN = re.compile(r"^[a-z_]{1,40}$")
_TOKEN_LIKE_KEY_PATTERN = re.compile(r"token|credential|secret|password")
# A-2: `configured_status`/`effective_status` mutan el estado de la
# entidad igual que `status` (Meta las trata como sinonimos en algunos
# endpoints); `spend_cap` toca dinero igual que `daily_budget` pero no
# termina en `_budget`, por eso va en la lista exacta y no en la de sufijos.
# R-1: `budget_rebalance_flag` reparte presupuesto entre conjuntos de
# anuncios y `start_time`/`end_time` mueven el gasto total de una campana
# activa con presupuesto diario; ninguno lo ve el tope duro ni el ledger.
_DENIED_EXACT_KEYS = frozenset(
    {
        "special_ad_categories",
        "status",
        "users",
        "configured_status",
        "effective_status",
        "spend_cap",
        "budget_rebalance_flag",
        "start_time",
        "end_time",
    }
)
_DENIED_KEY_PREFIXES = ("owner", "funding_source", "billing", "pacing")
_BUDGET_KEYS = frozenset({"daily_budget", "lifetime_budget", "budget"})
# A-2: `daily_spend_cap`/`lifetime_spend_cap` y `daily_min_spend_target`/
# `lifetime_min_spend_target` tocan dinero igual que un presupuesto, aunque
# el tope duro y el ledger del bróker solo saben leer `Money` tipado
# (`propose_budget_change`) -- nunca un payload libre.
_DENIED_KEY_SUFFIXES = ("_budget", "_spend_cap", "_spend_target")
_BID_EXACT_KEY = "bid_amount"
_BID_KEY_PREFIX = "bid_"
_Scalar = str | int | float | bool | None


class NativeWritePayloadError(ValueError):
    """`payload` de `propose_native_write` que no supera la lista blanca de
    dominio. `code` es estable para que la presentacion (pydantic
    `model_validator`) lo traduzca sin volver a inspeccionar el mensaje."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


def validate_native_write_payload(payload: Mapping[str, object]) -> None:
    _require_size_within_limit(payload)
    _require_valid_node(payload, depth=1)


def _require_size_within_limit(payload: Mapping[str, object]) -> None:
    serialized = json.dumps(payload, ensure_ascii=False, separators=(",", ":"))
    size = len(serialized.encode("utf-8"))
    if size > _MAX_PAYLOAD_BYTES:
        raise NativeWritePayloadError(
            "PAYLOAD_TOO_LARGE", f"el payload ({size} bytes) supera el tope de {_MAX_PAYLOAD_BYTES}"
        )


def _require_valid_node(node: Mapping[str, object], *, depth: int) -> None:
    if depth > _MAX_DEPTH:
        raise NativeWritePayloadError("PAYLOAD_TOO_DEEP", f"profundidad > {_MAX_DEPTH}")
    for key, value in node.items():
        _require_valid_key(key)
        _require_valid_value(key, value, depth=depth)


def _require_valid_key(key: str) -> None:
    if not _KEY_PATTERN.fullmatch(key):
        raise NativeWritePayloadError("INVALID_KEY", f"clave invalida: {key!r}")
    if _TOKEN_LIKE_KEY_PATTERN.search(key):
        raise NativeWritePayloadError(
            "FORBIDDEN_FIELD", f"'{key}' parece un token o credencial: no se admite"
        )
    if key in _DENIED_EXACT_KEYS or key.startswith(_DENIED_KEY_PREFIXES):
        raise NativeWritePayloadError(
            "FORBIDDEN_FIELD", f"'{key}' no se admite en una escritura nativa"
        )
    if key in _BUDGET_KEYS:
        raise NativeWritePayloadError(
            "USE_BUDGET_TOOL", f"'{key}' toca presupuesto: usa propose_budget_change"
        )
    if key.endswith(_DENIED_KEY_SUFFIXES):
        raise NativeWritePayloadError(
            "FORBIDDEN_FIELD", f"'{key}' toca presupuesto o ritmo de gasto: no se admite"
        )
    if key == _BID_EXACT_KEY or key.startswith(_BID_KEY_PREFIX):
        raise NativeWritePayloadError(
            "USE_BID_TOOL", f"'{key}' toca puja: usa propose_bid_target"
        )


def _require_valid_value(key: str, value: object, *, depth: int) -> None:
    if isinstance(value, Mapping):
        _require_valid_node(value, depth=depth + 1)
        return
    if isinstance(value, Sequence) and not isinstance(value, str | bytes):
        for item in value:
            _require_scalar(key, item)
        return
    _require_scalar(key, value)


def _require_scalar(key: str, value: object) -> None:
    if isinstance(value, _Scalar):
        return
    raise NativeWritePayloadError(
        "INVALID_VALUE", f"'{key}' debe ser un valor escalar o una lista de escalares"
    )

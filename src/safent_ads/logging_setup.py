"""Configuracion de `structlog` en JSON con redaccion de secretos obligatoria
(threat-model.md C-14: "Logs estructurados con redactor de secretos y PII en
el handler raiz")."""

from __future__ import annotations

import logging
import re
import sys
from typing import Any

import structlog
from structlog.types import EventDict, WrappedLogger

from safent_ads.shared.diagnostic_redaction import redact_diagnostic_text

_REDACTED = "***REDACTED***"

# Spec 002 (mcp_oauth) tasks.md T014+, threat-model.md C-50: nunca `code`,
# `code_verifier`, `txn`/`txn_id`, `assertion`. `code`/`txn` NO se añaden
# como subcadena libre a proposito: `error_code`/`status_code`/`rule_code`
# son claves de negocio reales que ya aparecen en decenas de logs de este
# repo y no son secretos -- solo se redacta "code"/"txn" cuando es la
# palabra completa o el PRIMER segmento de la clave (`code`,
# `code_verifier`, `code_challenge`, `txn`, `txn_id`...), nunca como sufijo.
_SENSITIVE_KEY_PATTERN = re.compile(
    r"(token|secret|key|password|authorization|verifier|assertion|^code(?:_|$)|^txn(?:_|$))",
    re.IGNORECASE,
)
# `state`/`nonce` (OAuth/CSRF, OIDC) no encajan en el patron anterior: se
# enmascaran por clave exacta (002b tasks.md T064/C-78: `nonce` es la
# referencia de un solo uso del salto federado, mismo trato que `state`).
# El resto son redundantes con el patron y se conservan a proposito.
_OAUTH_KEYS = frozenset({"code", "state", "nonce", "code_verifier", "code_challenge"})
_BEARER_PATTERN = re.compile(r"\bBearer\s+\S+", re.IGNORECASE)
_JWT_PATTERN = re.compile(r"\beyJ[A-Za-z0-9_-]+\.eyJ[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+\b")


def _looks_like_secret(value: str) -> bool:
    return bool(_BEARER_PATTERN.search(value) or _JWT_PATTERN.search(value))


def _redact_value(value: Any) -> Any:  # noqa: ANN401 - structlog event values are heterogeneous
    if isinstance(value, str) and _looks_like_secret(value):
        return _REDACTED
    if isinstance(value, dict):
        return _redact_mapping(value)
    if isinstance(value, list):
        return [_redact_value(v) for v in value]
    if isinstance(value, tuple):
        return tuple(_redact_value(v) for v in value)
    if isinstance(value, str):
        return redact_diagnostic_text(value)
    return value


def _redact_mapping(mapping: dict[str, Any]) -> dict[str, Any]:
    """Enmascara por clave sensible o por valor, a cualquier profundidad:
    el bug que este helper evita es solo mirar las claves del nivel raiz y
    dejar pasar un `password` anidado dentro de un `context` (cubierto por
    `test_redacts_nested_values_inside_dicts_and_lists`)."""
    redacted: dict[str, Any] = {}
    for key, value in mapping.items():
        redacted[key] = (
            _REDACTED
            if _SENSITIVE_KEY_PATTERN.search(key) or key.lower() in _OAUTH_KEYS
            else _redact_value(value)
        )
    return redacted


def redact_secrets(_logger: WrappedLogger, _method_name: str, event_dict: EventDict) -> EventDict:
    """Processor de structlog: enmascara valores de claves sensibles y
    cualquier cadena con pinta de bearer token o JWT, en cualquier
    profundidad del event dict. La firma la dicta el protocolo de processor
    de structlog: `logger`/`method_name` son obligatorios aunque no se usen."""
    event_dict.update(_redact_mapping(dict(event_dict)))
    return event_dict


_WIRE_LOGGERS = (
    "uvicorn.access",
    "httpx",
    "httpcore",
    "urllib3",
    "requests",
    "aiohttp",
    "google.auth",
    "google.ads",
    "facebook_business",
    "grpc",
)


class _SafeDiagnosticFilter(logging.Filter):
    """Transport wire logs are not application telemetry, even at DEBUG.

    Filter at handlers, not the root logger: propagated child records bypass
    root logger filters. Own structured operation/status logs remain available.
    """

    def filter(self, record: logging.LogRecord) -> bool:
        if any(record.name == name or record.name.startswith(name + ".") for name in _WIRE_LOGGERS):
            return False
        try:
            record.msg = redact_diagnostic_text(record.getMessage())
        except Exception:  # noqa: BLE001 - untrusted diagnostic __str__ must not break logging
            record.msg = "Diagnostic formatting failed"
        record.args = ()
        # Provider exceptions can contain unlabelled response bodies. Do not
        # format them or their chained traceback; retain only their type.
        if record.exc_info:
            kind = record.exc_info[0]
            record.msg += f" [exception={kind.__name__ if kind else 'unknown'}]"
        record.exc_info = None
        record.exc_text = None
        record.stack_info = None
        return True


def _exception_type_only(
    _logger: WrappedLogger, _method_name: str, event_dict: EventDict
) -> EventDict:
    info = event_dict.pop("exc_info", None)
    event_dict.pop("exception", None)
    if info:
        if isinstance(info, tuple):
            kind = info[0]
        elif isinstance(info, BaseException):
            kind = type(info)
        else:
            kind = sys.exc_info()[0]
        event_dict["exception_type"] = kind.__name__ if isinstance(kind, type) else "unknown"
    return event_dict


def configure_logging(*, level: int = logging.INFO) -> None:
    """Configura structlog en JSON para todo el proceso. Idempotente."""
    logging.basicConfig(format="%(message)s", stream=sys.stdout, level=level)
    for handler in logging.getLogger().handlers:
        if not any(isinstance(item, _SafeDiagnosticFilter) for item in handler.filters):
            handler.addFilter(_SafeDiagnosticFilter())
    # Uvicorn creates non-propagating handlers before create_app. Protect all
    # existing handlers, including uvicorn.error; future propagating children
    # are covered by the root handler. Never reformat Uvicorn access tuples:
    # access records are dropped before AccessFormatter sees them.
    names = set(_WIRE_LOGGERS) | set(logging.Logger.manager.loggerDict)
    for name in names:
        logger = logging.getLogger(name)
        if any(name == prefix or name.startswith(prefix + ".") for prefix in _WIRE_LOGGERS):
            logger.setLevel(logging.CRITICAL + 1)
        for handler in logger.handlers:
            if not any(isinstance(item, _SafeDiagnosticFilter) for item in handler.filters):
                handler.addFilter(_SafeDiagnosticFilter())

    structlog.configure(
        processors=[
            structlog.contextvars.merge_contextvars,
            structlog.stdlib.add_log_level,
            structlog.processors.TimeStamper(fmt="iso", utc=True),
            structlog.processors.StackInfoRenderer(),
            _exception_type_only,
            redact_secrets,
            structlog.processors.JSONRenderer(),
        ],
        wrapper_class=structlog.make_filtering_bound_logger(level),
        logger_factory=structlog.PrintLoggerFactory(file=sys.stdout),
        cache_logger_on_first_use=True,
    )

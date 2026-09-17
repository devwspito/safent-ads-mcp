"""Saneo de errores de SDK antes de devolverlos o registrarlos
(threat-model.md C-13: "Errores de SDK saneados... nunca el mensaje crudo
del SDK", `platform-port.md`: "El adaptador no registra nunca tokens,
`refresh_token`, cabeceras de autorizacion ni cuerpos completos de
respuesta")."""

from __future__ import annotations

import re
from typing import Final

from safent_ads.shared.diagnostic_redaction import redact_diagnostic_text

_REDACTED: Final = "***REDACTED***"
_MAX_MESSAGE_LENGTH: Final = 300

_KEY_VALUE_SECRET_PATTERN = re.compile(
    r"(?i)(refresh[_-]?token|access[_-]?token|client[_-]?secret|"
    r"authorization|api[_-]?key|system[_-]?user[_-]?token)\s*[:=]\s*['\"]?[^\s'\",}]+"
)
_BEARER_PATTERN = re.compile(r"\bBearer\s+\S+", re.IGNORECASE)
_JWT_PATTERN = re.compile(r"\beyJ[A-Za-z0-9_-]+\.eyJ[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+\b")
_GOOGLE_CUSTOMER_ID_PATTERN = re.compile(r"\b\d{3}-\d{3}-\d{4}\b")
_META_TOKEN_PATTERN = re.compile(r"\bEAA[A-Za-z0-9]{10,}\b")

_SENSITIVE_PATTERNS = (
    # Bearer/JWT primero: si el patron clave=valor corriera antes, se
    # comeria solo la palabra "Bearer" y dejaria el token suelto detras.
    _BEARER_PATTERN,
    _JWT_PATTERN,
    _KEY_VALUE_SECRET_PATTERN,
    _GOOGLE_CUSTOMER_ID_PATTERN,
    _META_TOKEN_PATTERN,
)


def redact_sdk_error(exc: BaseException) -> str:
    """Nunca propaga el mensaje crudo del SDK: enmascara credenciales e
    identificadores de cuenta conocidos, y trunca el resultado (nunca el
    cuerpo completo de una respuesta)."""
    message = redact_diagnostic_text(f"{type(exc).__name__}: {exc}")
    for pattern in _SENSITIVE_PATTERNS:
        message = pattern.sub(_REDACTED, message)
    return message[:_MAX_MESSAGE_LENGTH]

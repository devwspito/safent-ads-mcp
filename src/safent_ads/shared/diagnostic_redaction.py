"""Diagnostic text only: never preserve URLs, OAuth credential assignments,
or Google Ads conversion action resource names (threat-model.md
R-2/ME-5: `conversionActions/{id}` is an internal
account identifier, same treatment as a token)."""

from __future__ import annotations

import re

REDACTED = "***REDACTED***"
_URL = re.compile(r"https?://[^\s<>\"']+", re.IGNORECASE)
_CONVERSION_ACTION = re.compile(r"\bconversionActions/[0-9]+\b")
_ASSIGNMENT = re.compile(
    r"(?i)\b(refresh[_-]?token|access[_-]?token|client[_-]?secret|app[_-]?secret|"
    r"authorization|api[_-]?key|system[_-]?user[_-]?token|fb_exchange_token|"
    r"password|code_verifier|code_challenge|code|state)"
    r"[\"']?\s*[:=]\s*(?:\"[^\"]*\"|'[^']*'|[^\s,;&}\]]+)"
)
_BEARER = re.compile(r"\bBearer\s+\S+", re.IGNORECASE)
_JWT = re.compile(r"\beyJ[A-Za-z0-9_-]+\.eyJ[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+\b")
_META_TOKEN = re.compile(r"\bEAA[A-Za-z0-9]{10,}\b")


def redact_diagnostic_text(value: str) -> str:
    # Whole URLs, not a query-key allowlist: providers can add sensitive fields
    # and URI userinfo/encoded query values must not evade redaction.
    for pattern in (_URL, _BEARER, _JWT, _ASSIGNMENT, _META_TOKEN, _CONVERSION_ACTION):
        value = pattern.sub(REDACTED, value)
    return value

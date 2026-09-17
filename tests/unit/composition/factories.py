"""Fabrica de `ApiSettings` valido para tests de `composition` (settings,
entrypoint del servidor): construir los campos obligatorios a mano en cada
test seria repetitivo."""

from __future__ import annotations

from typing import Any
from uuid import UUID

from safent_ads.composition.settings import ApiSettings

_VALID_32_BYTE_KEY_B64 = "MDAwMDAwMDAwMDAwMDAwMDAwMDAwMDAwMDAwMDAwMDA="
# Clave publica Ed25519 (026, contracts/sso.md §3) real y fija, solo para
# que el modo companion arranque en tests que no ejercitan `/auth/exchange`
# -- ningun privado correspondiente existe fuera de este repo.
_VALID_SSO_PUBLIC_KEY = "UWraqmaS7EZ-sBSdLoGu-zaLxqe4q4Dn6aZ5stMf6qw"
# Trust de Enterprise valido pero inerte (004 A10: `ApiSettings` no arranca
# fuera de companion mode sin `ADS_SEAT_AUTHORITY_ENABLED`). Ningun test que
# no ejercite `/mcp` real necesita tocar esto -- solo que el arranque no
# falle.
_VALID_ENTERPRISE_ORIGIN = "https://enterprise.test"
_VALID_ENTERPRISE_SERVICE_SECRET = "a" * 64
_VALID_ENTERPRISE_ORG_IDS = frozenset({UUID("00000000-0000-0000-0000-000000000001")})


def build_api_settings(**overrides: Any) -> ApiSettings:
    defaults: dict[str, Any] = {
        "database_url": "postgresql+asyncpg://ads:test@localhost:5432/ads_test",
        "session_secret": "test-session-secret-0123456789abcdef",
        "totp_enc_key": _VALID_32_BYTE_KEY_B64,
        "mcp_token": "test-mcp-token-abc123",
        "broker_socket_path": "/tmp/safent-ads-test/broker.sock",
        "public_base_url": "https://ads.test.ts.net",
        "telegram_bot_token": "123456:test-bot-token",
        "telegram_owner_chat_ids": [111222333],
        "approval_signing_key": _VALID_32_BYTE_KEY_B64,
        "seat_authority_enabled": True,
        "enterprise_origin": _VALID_ENTERPRISE_ORIGIN,
        "enterprise_service_secret": _VALID_ENTERPRISE_SERVICE_SECRET,
        "enterprise_org_ids": _VALID_ENTERPRISE_ORG_IDS,
    }
    if overrides.get("companion_mode") and "sso_public_key" not in overrides:
        defaults["sso_public_key"] = _VALID_SSO_PUBLIC_KEY
    defaults.update(overrides)
    return ApiSettings(_env_file=None, **defaults)

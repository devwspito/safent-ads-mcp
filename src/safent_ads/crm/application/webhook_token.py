"""`GenerateWebhookToken`/`AuthenticateWebhookToken` (T220,
`POST /conversions/webhook-token` y `POST /conversions/webhook`):
`conversion_webhook_tokens` guarda solo el hash -- el crudo se muestra una
vez en el panel y nunca se persiste (mismo criterio que
`iam.application.verify_totp.hash_session_token` para la cookie de
sesion, hash local aqui para no importar la capa de aplicacion de `iam`
desde `crm`, contextos hermanos sin dependencia cruzada)."""

from __future__ import annotations

import hashlib
import secrets

from safent_ads.crm.application.ports import WebhookTokenRepository
from safent_ads.shared.ids import BusinessId

_TOKEN_BYTES = 32


def hash_webhook_token(raw_token: str) -> str:
    return hashlib.sha256(raw_token.encode("utf-8")).hexdigest()


class GenerateWebhookToken:
    def __init__(self, *, tokens: WebhookTokenRepository) -> None:
        self._tokens = tokens

    async def execute(self, business_id: BusinessId) -> str:
        raw_token = secrets.token_urlsafe(_TOKEN_BYTES)
        await self._tokens.upsert(business_id=business_id, token_hash=hash_webhook_token(raw_token))
        return raw_token


class AuthenticateWebhookToken:
    def __init__(self, *, tokens: WebhookTokenRepository) -> None:
        self._tokens = tokens

    async def execute(self, raw_token: str) -> BusinessId | None:
        return await self._tokens.find_business_id_by_token_hash(hash_webhook_token(raw_token))

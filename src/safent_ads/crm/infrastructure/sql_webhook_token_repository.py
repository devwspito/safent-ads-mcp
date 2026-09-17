"""`SqlWebhookTokenRepository` sobre `conversion_webhook_tokens`
(0029_economics_inputs, T220): un token activo por negocio -- regenerar
(`ON CONFLICT (business_id) DO UPDATE`) invalida el anterior al instante,
mismo criterio UX que el emparejamiento de Telegram."""

from __future__ import annotations

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from safent_ads.shared.ids import BusinessId

__all__ = ["SqlWebhookTokenRepository"]

_UPSERT = text("""
    INSERT INTO conversion_webhook_tokens (business_id, token_hash)
    VALUES (:business_id, :token_hash)
    ON CONFLICT (business_id) DO UPDATE SET
        token_hash = EXCLUDED.token_hash, created_at = now()
""")

_FIND_BY_HASH = text(
    "SELECT business_id FROM conversion_webhook_tokens WHERE token_hash = :token_hash"
)


class SqlWebhookTokenRepository:
    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def upsert(self, *, business_id: BusinessId, token_hash: str) -> None:
        await self._session.execute(
            _UPSERT, {"business_id": business_id.value, "token_hash": token_hash}
        )
        await self._session.flush()

    async def find_business_id_by_token_hash(self, token_hash: str) -> BusinessId | None:
        result = await self._session.execute(_FIND_BY_HASH, {"token_hash": token_hash})
        row = result.one_or_none()
        return None if row is None else BusinessId(row[0])

"""`SqlCrmBridgeTokenRepository` sobre `crm_bridge_tokens`
(0033_crm_bridge_health, spec 027, contracts/crm-link.md §2): reusa
`GenerateWebhookToken`/`AuthenticateWebhookToken` (`crm.application.
webhook_token`) -- ambos casos de uso son genericos sobre cualquier
`WebhookTokenRepository`, solo cambia la tabla de respaldo. Tabla propia
(no `conversion_webhook_tokens`): son dos superficies de confianza
distintas, un secreto compartido ampliaria el radio de una filtracion
(ver docstring de `0033_crm_bridge_health`)."""

from __future__ import annotations

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from safent_ads.shared.ids import BusinessId

__all__ = ["SqlCrmBridgeTokenRepository"]

_UPSERT = text("""
    INSERT INTO crm_bridge_tokens (business_id, token_hash)
    VALUES (:business_id, :token_hash)
    ON CONFLICT (business_id) DO UPDATE SET
        token_hash = EXCLUDED.token_hash, created_at = now()
""")

_FIND_BY_HASH = text("SELECT business_id FROM crm_bridge_tokens WHERE token_hash = :token_hash")


class SqlCrmBridgeTokenRepository:
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

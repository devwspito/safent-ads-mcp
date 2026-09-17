"""`SqlCloudflareConnectionStore`: adaptador SQL de `CloudflareConnectionStore`
sobre `cloudflare_connection` (0050_cloudflare_connection). Fila unica
(`id BOOLEAN PRIMARY KEY DEFAULT TRUE`, ver la migracion) cifrada con
`AesGcmTotpCipher` reusada de `iam` -- misma clave `ADS_TOTP_ENC_KEY` que
ya cifra el secreto TOTP y el codigo de emparejamiento de Telegram
(`notifications/infrastructure/telegram_pairing_sql.py`).

`zones` viaja como `payload::text` + `CAST(:zones AS JSONB)`, nunca
confiando en la decodificacion automatica jsonb->list del driver (mismo
criterio documentado en `audit/infrastructure/sql_repository.py`)."""

from __future__ import annotations

import json
import uuid

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from safent_ads.iam.infrastructure.aesgcm_totp_cipher import (
    PURPOSE_CLOUDFLARE_API_TOKEN,
    AesGcmTotpCipher,
)
from safent_ads.integrations.cloudflare.connection_port import CloudflareConnectionRecord

__all__ = [
    "RequestScopedCloudflareConnectionStore",
    "SqlCloudflareConnectionStore",
    "build_request_scoped_cloudflare_connection_store",
]

_SELECT = text("""
    SELECT api_token_encrypted, account_id, zones::text AS zones_text,
           connected_at, connected_by_owner_id
      FROM cloudflare_connection WHERE id
""")

_UPSERT = text("""
    INSERT INTO cloudflare_connection
        (id, api_token_encrypted, account_id, zones, connected_at,
         connected_by_owner_id, updated_at)
    VALUES (TRUE, :token, :account_id, CAST(:zones AS JSONB), :connected_at, :owner_id, now())
    ON CONFLICT (id) DO UPDATE SET
        api_token_encrypted = EXCLUDED.api_token_encrypted,
        account_id = EXCLUDED.account_id,
        zones = EXCLUDED.zones,
        connected_at = EXCLUDED.connected_at,
        connected_by_owner_id = EXCLUDED.connected_by_owner_id,
        updated_at = now()
""")

_DELETE = text("DELETE FROM cloudflare_connection WHERE id")


class SqlCloudflareConnectionStore:
    def __init__(self, session: AsyncSession, *, cipher: AesGcmTotpCipher) -> None:
        self._session = session
        self._cipher = cipher

    async def get(self) -> CloudflareConnectionRecord | None:
        row = (await self._session.execute(_SELECT)).mappings().first()
        if row is None:
            return None
        connected_by = row["connected_by_owner_id"]
        return CloudflareConnectionRecord(
            token=self._cipher.decrypt(
                bytes(row["api_token_encrypted"]), purpose=PURPOSE_CLOUDFLARE_API_TOKEN
            ),
            account_id=row["account_id"],
            zones=tuple(json.loads(row["zones_text"])),
            connected_at=row["connected_at"],
            connected_by_owner_id=uuid.UUID(str(connected_by)) if connected_by else None,
        )

    async def save(self, record: CloudflareConnectionRecord) -> None:
        await self._session.execute(
            _UPSERT,
            {
                "token": self._cipher.encrypt(record.token, purpose=PURPOSE_CLOUDFLARE_API_TOKEN),
                "account_id": record.account_id,
                "zones": json.dumps(list(record.zones)),
                "connected_at": record.connected_at,
                "owner_id": record.connected_by_owner_id,
            },
        )

    async def delete(self) -> None:
        await self._session.execute(_DELETE)


class RequestScopedCloudflareConnectionStore:
    """`CloudflareConnectionStore` real, una sesion por llamada
    (`session_factory()`, mismo patron que `RequestScopedExecutionReadPort`):
    lo que `composition/app.py` cablea UNA vez al arrancar para
    `DynamicCloudflareService`/`GetCloudflareConnectionStatus` del MCP --
    ninguna de las dos puede abrazar una `AsyncSession` fijada al arranque,
    el estado de conexion cambia en caliente desde el panel."""

    def __init__(
        self, session_factory: async_sessionmaker[AsyncSession], *, cipher: AesGcmTotpCipher
    ) -> None:
        self._session_factory = session_factory
        self._cipher = cipher

    async def get(self) -> CloudflareConnectionRecord | None:
        async with self._session_factory() as session:
            return await SqlCloudflareConnectionStore(session, cipher=self._cipher).get()

    async def save(self, record: CloudflareConnectionRecord) -> None:
        async with self._session_factory() as session:
            await SqlCloudflareConnectionStore(session, cipher=self._cipher).save(record)
            await session.commit()

    async def delete(self) -> None:
        async with self._session_factory() as session:
            await SqlCloudflareConnectionStore(session, cipher=self._cipher).delete()
            await session.commit()


def build_request_scoped_cloudflare_connection_store(
    session_factory: async_sessionmaker[AsyncSession], *, totp_enc_key: str
) -> RequestScopedCloudflareConnectionStore:
    """`composition/app.py` llama esto con la clave en claro (`ApiSettings.
    totp_enc_key.get_secret_value()`), nunca con un `AesGcmTotpCipher` ya
    construido -- mismo patron que `notifications.presentation.rest.
    build_telegram_pairing_router(totp_enc_key=...)`: la composicion pasa
    primitivas, el modulo de infraestructura es quien conoce el cifrador."""
    return RequestScopedCloudflareConnectionStore(
        session_factory, cipher=AesGcmTotpCipher(totp_enc_key)
    )

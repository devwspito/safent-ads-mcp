"""`SqlCloudflareConnectionStore` (`connection_store.py`,
0050_cloudflare_connection) contra Postgres real: fila unica cifrada
(AES-256-GCM, `AesGcmTotpCipher` reusada de `iam`), upsert al reconectar,
y borrado limpio de la fila al desconectar."""

from __future__ import annotations

from datetime import UTC, datetime

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from safent_ads.iam.infrastructure.aesgcm_totp_cipher import AesGcmTotpCipher
from safent_ads.integrations.cloudflare.connection_port import CloudflareConnectionRecord
from safent_ads.integrations.cloudflare.connection_store import SqlCloudflareConnectionStore
from tests.conftest import OwnerFactory

pytestmark = pytest.mark.integration

_VALID_32_BYTE_KEY_B64 = "MDAwMDAwMDAwMDAwMDAwMDAwMDAwMDAwMDAwMDAwMDA="
_CONNECTED_AT = datetime(2026, 9, 15, 12, 0, tzinfo=UTC)
_TOKEN = "sk-cloudflare-super-secret-token-do-not-leak"  # noqa: S105 - fixture


def _cipher() -> AesGcmTotpCipher:
    return AesGcmTotpCipher(_VALID_32_BYTE_KEY_B64)


async def test_get_returns_none_when_never_connected(db_session: AsyncSession) -> None:
    store = SqlCloudflareConnectionStore(db_session, cipher=_cipher())

    assert await store.get() is None


async def test_save_then_get_round_trips_the_decrypted_token(
    db_session: AsyncSession, owner_factory: OwnerFactory
) -> None:
    owner_id = await owner_factory.create()
    store = SqlCloudflareConnectionStore(db_session, cipher=_cipher())
    record = CloudflareConnectionRecord(
        token=_TOKEN,
        account_id="a" * 32,
        zones=("example.com", "example.net"),
        connected_at=_CONNECTED_AT,
        connected_by_owner_id=owner_id,
    )

    await store.save(record)
    await db_session.commit()

    stored = await store.get()
    assert stored is not None
    assert stored.token == _TOKEN
    assert stored.account_id == "a" * 32
    assert stored.zones == ("example.com", "example.net")
    assert stored.connected_at == _CONNECTED_AT
    assert stored.connected_by_owner_id == owner_id


async def test_the_raw_column_never_holds_the_plaintext_token(db_session: AsyncSession) -> None:
    store = SqlCloudflareConnectionStore(db_session, cipher=_cipher())
    await store.save(
        CloudflareConnectionRecord(
            token=_TOKEN,
            account_id=None,
            zones=(),
            connected_at=_CONNECTED_AT,
            connected_by_owner_id=None,
        )
    )
    await db_session.commit()

    row = (
        await db_session.execute(text("SELECT api_token_encrypted FROM cloudflare_connection"))
    ).scalar_one()
    assert _TOKEN.encode() not in bytes(row)


async def test_reconnecting_upserts_the_single_row(db_session: AsyncSession) -> None:
    store = SqlCloudflareConnectionStore(db_session, cipher=_cipher())
    await store.save(
        CloudflareConnectionRecord(
            token="old-token",
            account_id="a" * 32,
            zones=("example.com",),
            connected_at=_CONNECTED_AT,
            connected_by_owner_id=None,
        )
    )
    await store.save(
        CloudflareConnectionRecord(
            token="new-token",
            account_id="b" * 32,
            zones=("example.net",),
            connected_at=_CONNECTED_AT,
            connected_by_owner_id=None,
        )
    )
    await db_session.commit()

    stored = await store.get()
    assert stored is not None
    assert stored.token == "new-token"  # noqa: S105 - fixture, no secreto real
    assert stored.account_id == "b" * 32
    assert stored.zones == ("example.net",)


async def test_delete_removes_the_row(db_session: AsyncSession) -> None:
    store = SqlCloudflareConnectionStore(db_session, cipher=_cipher())
    await store.save(
        CloudflareConnectionRecord(
            token=_TOKEN,
            account_id=None,
            zones=(),
            connected_at=_CONNECTED_AT,
            connected_by_owner_id=None,
        )
    )
    await db_session.commit()

    await store.delete()
    await db_session.commit()

    assert await store.get() is None

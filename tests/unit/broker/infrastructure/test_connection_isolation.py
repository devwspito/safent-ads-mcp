import asyncio
import base64
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from pathlib import Path
from uuid import uuid4

import pytest

from safent_ads.accounts.domain.refs import CredentialRefId
from safent_ads.broker.application.connection_scope import (
    ConnectionScope,
    connection_scope,
    current_connection_scope,
)
from safent_ads.broker.application.ports import CredentialRecord
from safent_ads.broker.infrastructure.connected_credential_store import ConnectedCredentialStore
from safent_ads.broker.infrastructure.credential_store import (
    CredentialStoreKeyError,
    EncryptedCredentialStore,
)
from safent_ads.broker.platforms.live_google_ads_client import LiveGoogleAdsSearchClient
from safent_ads.broker.platforms.live_meta_graph_client import LiveMetaGraphClient
from safent_ads.shared.clock import FixedClock
from safent_ads.shared.ids import PlatformCode

NOW = datetime(2026, 9, 11, tzinfo=UTC)


@pytest.mark.parametrize("platform", list(PlatformCode))
async def test_same_remote_account_connections_never_fallback_or_cross_threads(
    tmp_path: Path, platform: PlatformCode
) -> None:
    key = base64.b64encode(b"a" * 32).decode()
    store = EncryptedCredentialStore(tmp_path, key)
    business = uuid4()
    account = "123" if platform == PlatformCode.GOOGLE else "act_123"
    scopes = [
        ConnectionScope(business, uuid4()),
        ConnectionScope(business, uuid4()),
        ConnectionScope(uuid4(), uuid4()),
    ]
    refs = []
    for index, scope in enumerate(scopes):
        ref = CredentialRefId(uuid4())
        refs.append(ref)
        record = CredentialRecord(
            platform,
            f"canary-{index}",
            "refresh_token" if platform == PlatformCode.GOOGLE else "long_lived_token",
            (),
            NOW,
            NOW + timedelta(days=1),
            business_id=str(scope.business_id),
            connection_id=str(scope.connection_id),
            owner_id=str(uuid4()),
        )
        store.save_credential(ref, record)
        store.bind_account_credential(
            platform,
            account,
            ref,
            business_id=str(scope.business_id),
            connection_id=str(scope.connection_id),
        )
    reader = ConnectedCredentialStore(EncryptedCredentialStore(tmp_path, key), FixedClock(NOW))
    live = (
        LiveGoogleAdsSearchClient(client_id="test", client_secret="test", credential_store=reader)
        if platform == PlatformCode.GOOGLE
        else LiveMetaGraphClient(app_id="test", app_secret="test", credential_store=reader)
    )

    async def read(scope: ConnectionScope) -> str | None:
        with connection_scope(scope):
            # The real SDK clients also resolve inside asyncio.to_thread/asyncio.run.
            credential = await asyncio.to_thread(
                lambda: asyncio.run(reader.get_credential(platform, account))
            )
            if credential is not None:
                actual = await asyncio.to_thread(live._resolve_credential, account)
                assert actual == credential
            return (
                None if credential is None else credential.refresh_token or credential.access_token
            )

    assert await asyncio.gather(*(read(scope) for scope in scopes)) == [
        "canary-0",
        "canary-1",
        "canary-2",
    ]
    assert current_connection_scope() is None
    assert await reader.get_credential(platform, account) is None
    assert await read(ConnectionScope(uuid4(), scopes[0].connection_id)) is None
    store.revoke_credential(refs[0], at=NOW)
    assert await read(scopes[0]) is None
    assert await read(scopes[1]) == "canary-1"
    second = store.get_credential(refs[1])
    assert second is not None
    store.save_credential(refs[1], replace(second, expires_at=NOW))
    assert await read(scopes[1]) is None
    assert await read(scopes[2]) == "canary-2"
    with pytest.raises(CredentialStoreKeyError):
        store.bind_account_credential(
            platform,
            account,
            refs[2],
            business_id=str(scopes[0].business_id),
            connection_id=str(scopes[0].connection_id),
        )


async def test_context_is_reset_after_cancellation() -> None:
    async def cancel() -> None:
        with connection_scope(ConnectionScope(uuid4(), uuid4())):
            raise asyncio.CancelledError

    with pytest.raises(asyncio.CancelledError):
        await cancel()
    assert current_connection_scope() is None

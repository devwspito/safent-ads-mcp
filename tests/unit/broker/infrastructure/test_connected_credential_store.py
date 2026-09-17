"""Encrypted OAuth records reach live clients, with revocation on the next call."""

import base64
import uuid
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from safent_ads.accounts.domain.refs import CredentialRefId
from safent_ads.broker.application.connection_scope import ConnectionScope, connection_scope
from safent_ads.broker.application.ports import CredentialRecord, GoogleAppCredentials
from safent_ads.broker.infrastructure.connected_credential_store import ConnectedCredentialStore
from safent_ads.broker.infrastructure.credential_store import (
    CredentialStoreKeyError,
    EncryptedCredentialStore,
)
from safent_ads.shared.clock import FixedClock
from safent_ads.shared.ids import PlatformCode

_NOW = datetime(2026, 9, 10, tzinfo=UTC)
_KEY = base64.b64encode(b"0" * 32).decode()
_SCOPE = ConnectionScope(uuid.uuid4(), uuid.uuid4())
_SCOPE_ARGS = {"business_id": str(_SCOPE.business_id), "connection_id": str(_SCOPE.connection_id)}


@pytest.fixture(autouse=True)
def scoped_request():
    with connection_scope(_SCOPE):
        yield


def _store(path: Path) -> EncryptedCredentialStore:
    return EncryptedCredentialStore(path, _KEY)


def _record(platform: PlatformCode) -> CredentialRecord:
    return CredentialRecord(
        platform=platform,
        token="test-secret",
        token_type="refresh_token" if platform == PlatformCode.GOOGLE else "long_lived_token",
        scopes=("ads_read",),
        obtained_at=_NOW,
        expires_at=None,
        **_SCOPE_ARGS,
    )


@pytest.mark.parametrize("platform", list(PlatformCode))
async def test_binding_survives_restart_and_revocation_is_immediate(
    tmp_path: Path,
    platform: PlatformCode,
) -> None:
    store = _store(tmp_path)
    ref = CredentialRefId(uuid.uuid4())
    account = "123" if platform == PlatformCode.GOOGLE else "act_123"
    store.save_credential(ref, _record(platform))
    store.bind_account_credential(platform, account, ref, **_SCOPE_ARGS)
    restarted = _store(tmp_path)
    reader = ConnectedCredentialStore(restarted, FixedClock(_NOW))
    credential = await reader.get_credential(platform, account)
    assert credential is not None
    assert (
        credential.refresh_token == "test-secret"  # noqa: S105 - test fixture
        if platform == PlatformCode.GOOGLE
        else credential.access_token == "test-secret"  # noqa: S105 - test fixture
    )
    assert await reader.get_credential(platform, "other-account") is None
    restarted.revoke_credential(ref, at=_NOW)
    assert await reader.get_credential(platform, account) is None
    assert all(b"test-secret" not in path.read_bytes() for path in tmp_path.rglob("*.enc"))


@pytest.mark.parametrize("offset", [-1, 0])
async def test_expired_meta_credential_fails_closed(tmp_path: Path, offset: int) -> None:
    store = _store(tmp_path)
    ref = CredentialRefId(uuid.uuid4())
    store.save_credential(
        ref, replace(_record(PlatformCode.META), expires_at=_NOW + timedelta(seconds=offset))
    )
    store.bind_account_credential(PlatformCode.META, "act_123", ref, **_SCOPE_ARGS)
    reader = ConnectedCredentialStore(store, FixedClock(_NOW))
    assert await reader.get_credential(PlatformCode.META, "act_123") is None


async def test_reconnect_replaces_alias_without_cached_token(tmp_path: Path) -> None:
    store = _store(tmp_path)
    reader = ConnectedCredentialStore(store, FixedClock(_NOW))
    for token in ("first", "second"):
        ref = CredentialRefId(uuid.uuid4())
        store.save_credential(ref, replace(_record(PlatformCode.META), token=token))
        store.bind_account_credential(PlatformCode.META, "act_123", ref, **_SCOPE_ARGS)
        result = await reader.get_credential(PlatformCode.META, "act_123")
        assert result is not None and result.access_token == token


async def test_google_manager_is_optional_and_app_changes_apply(tmp_path: Path) -> None:
    store = _store(tmp_path)
    ref = CredentialRefId(uuid.uuid4())
    store.save_credential(ref, _record(PlatformCode.GOOGLE))
    store.bind_account_credential(PlatformCode.GOOGLE, "123", ref, **_SCOPE_ARGS)
    reader = ConnectedCredentialStore(store, FixedClock(_NOW))
    credential = await reader.get_credential(PlatformCode.GOOGLE, "123")
    assert credential is not None and credential.login_customer_id is None
    store.save_google_app_credentials(
        GoogleAppCredentials(
            client_id="test",
            client_secret="test",
            login_customer_id="456",
            updated_at=_NOW,
        )
    )
    credential = await reader.get_credential(PlatformCode.GOOGLE, "123")
    assert credential is not None and credential.login_customer_id == "456"


def test_cannot_bind_a_credential_from_another_platform(tmp_path: Path) -> None:
    store = _store(tmp_path)
    ref = CredentialRefId(uuid.uuid4())
    store.save_credential(ref, _record(PlatformCode.GOOGLE))
    with pytest.raises(CredentialStoreKeyError):
        store.bind_account_credential(PlatformCode.META, "act_123", ref)

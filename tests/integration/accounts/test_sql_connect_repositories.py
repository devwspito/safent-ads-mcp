"""`SqlCredentialRepository`/`SqlOAuthConnectSessionRepository` contra
Postgres real (migracion 0013): los dobles en memoria no modelan el CHECK
de coherencia estado/`completed_at`, el `UNIQUE` de `state_hash`, ni las
FK de `oauth_connect_sessions` hacia `businesses`/`owners`
(data-model.md §Migration plan)."""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from safent_ads.accounts.domain.oauth_connect_session import OAuthConnectSession, OAuthSessionStatus
from safent_ads.accounts.domain.platform_credential import CredentialStatus, PlatformCredential
from safent_ads.accounts.domain.refs import CredentialRefId
from safent_ads.accounts.infrastructure.sql_connect_repositories import (
    SqlCredentialRepository,
    SqlOAuthConnectSessionRepository,
)
from safent_ads.shared.ids import BusinessId, PlatformCode
from tests.conftest import BusinessFactory, OwnerFactory

pytestmark = pytest.mark.integration

_NOW = datetime(2026, 9, 9, tzinfo=UTC)


def _credential(*, business_id: BusinessId) -> PlatformCredential:
    return PlatformCredential(
        credential_ref_id=CredentialRefId(uuid.uuid4()),
        business_id=business_id,
        platform=PlatformCode.GOOGLE,
        alias=f"alias-{uuid.uuid4().hex[:12]}",
        scopes=frozenset({"https://www.googleapis.com/auth/adwords"}),
        obtained_at=_NOW,
        expires_at=None,
    )


async def test_credential_round_trips_through_credential_refs(db_session: AsyncSession) -> None:
    business_id = BusinessId.new()
    credential = _credential(business_id=business_id)
    repository = SqlCredentialRepository(db_session)

    await repository.save(credential)
    reloaded = await repository.get(credential.credential_ref_id, business_id=business_id)

    assert reloaded is not None
    assert reloaded.credential_ref_id == credential.credential_ref_id
    assert reloaded.business_id == business_id
    assert reloaded.platform == PlatformCode.GOOGLE
    assert reloaded.scopes == credential.scopes
    assert reloaded.status == CredentialStatus.CONNECTED


async def test_credential_get_returns_none_for_unknown_id(db_session: AsyncSession) -> None:
    repository = SqlCredentialRepository(db_session)

    reloaded = await repository.get(CredentialRefId(uuid.uuid4()), business_id=BusinessId.new())

    assert reloaded is None


async def test_credential_save_is_an_upsert_that_rotates_status(db_session: AsyncSession) -> None:
    business_id = BusinessId.new()
    credential = _credential(business_id=business_id)
    repository = SqlCredentialRepository(db_session)
    await repository.save(credential)

    credential.revoke(at=_NOW)
    await repository.save(credential)

    reloaded = await repository.get(credential.credential_ref_id, business_id=business_id)
    assert reloaded is not None
    assert reloaded.status == CredentialStatus.REVOKED
    assert reloaded.revoked_at == _NOW


async def test_oauth_connect_session_round_trips_by_id_and_state_hash(
    db_session: AsyncSession, business_factory: BusinessFactory, owner_factory: OwnerFactory
) -> None:
    business_id = BusinessId(await business_factory.create())
    owner_id = await owner_factory.create()
    session = OAuthConnectSession(
        session_id=uuid.uuid4(),
        business_id=business_id,
        owner_id=owner_id,
        provider=PlatformCode.GOOGLE,
        state_hash=f"hash-{uuid.uuid4().hex}",
        expires_at=_NOW + timedelta(minutes=10),
    )
    repository = SqlOAuthConnectSessionRepository(db_session)

    await repository.save(session)

    by_id = await repository.get_by_id(session.session_id)
    by_state_hash = await repository.get_by_state_hash(session.state_hash)

    assert by_id is not None
    assert by_id.business_id == business_id
    assert by_id.owner_id == owner_id
    assert by_id.status == OAuthSessionStatus.WAITING
    assert by_state_hash is not None
    assert by_state_hash.session_id == session.session_id


async def test_oauth_connect_session_get_by_state_hash_returns_none_when_unknown(
    db_session: AsyncSession,
) -> None:
    repository = SqlOAuthConnectSessionRepository(db_session)

    assert await repository.get_by_state_hash("no-such-hash") is None


async def test_oauth_connect_session_save_persists_resolution(
    db_session: AsyncSession, business_factory: BusinessFactory, owner_factory: OwnerFactory
) -> None:
    business_id = BusinessId(await business_factory.create())
    owner_id = await owner_factory.create()
    session = OAuthConnectSession(
        session_id=uuid.uuid4(),
        business_id=business_id,
        owner_id=owner_id,
        provider=PlatformCode.META,
        state_hash=f"hash-{uuid.uuid4().hex}",
        expires_at=_NOW + timedelta(minutes=10),
    )
    repository = SqlOAuthConnectSessionRepository(db_session)
    await repository.save(session)

    session.mark_ok(at=_NOW)
    await repository.save(session)

    reloaded = await repository.get_by_id(session.session_id)
    assert reloaded is not None
    assert reloaded.status == OAuthSessionStatus.OK
    assert reloaded.completed_at == _NOW

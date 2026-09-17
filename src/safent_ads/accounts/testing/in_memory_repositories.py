"""Implementaciones en memoria de `AccountRepository`/`AdEntityRepository`
(ports.py) y `CredentialRepository`/`OAuthConnectSessionRepository`
(connect_ports.py) para tests de los casos de uso de `application` sin
depender de Postgres/SQLAlchemy."""

from __future__ import annotations

import uuid
from collections.abc import Iterable, Sequence

from safent_ads.accounts.application.ports import AccountRef
from safent_ads.accounts.domain.ad_entity import AdEntity
from safent_ads.accounts.domain.errors import AccountOwnershipConflictError
from safent_ads.accounts.domain.oauth_connect_session import OAuthConnectSession
from safent_ads.accounts.domain.platform_account import PlatformAccount
from safent_ads.accounts.domain.platform_credential import PlatformCredential
from safent_ads.accounts.domain.refs import CredentialRefId
from safent_ads.shared.ids import BusinessId, EntityLevel, EntityRef, PlatformCode


class InMemoryAccountRepository:
    def __init__(self, accounts: Iterable[PlatformAccount] = ()) -> None:
        self._by_ref: dict[AccountRef, PlatformAccount] = {a.account_ref: a for a in accounts}

    async def get_by_ref(self, account_ref: AccountRef) -> PlatformAccount | None:
        return self._by_ref.get(account_ref)

    async def list_by_business(self, business_id: BusinessId) -> Sequence[PlatformAccount]:
        return [a for a in self._by_ref.values() if a.business_id == business_id]

    async def save(self, account: PlatformAccount) -> None:
        existing = self._by_ref.get(account.account_ref)
        if (
            existing is not None
            and existing.business_id != account.business_id
            or account.account_ref.business_id not in (None, account.business_id.value)
        ):
            raise AccountOwnershipConflictError("account_business_mismatch")
        self._by_ref[account.account_ref] = account

    async def exists_for_platform(self, platform: PlatformCode) -> bool:
        return any(ref.platform == platform for ref in self._by_ref)


class InMemoryAdEntityRepository:
    def __init__(self, entities: Iterable[AdEntity] = ()) -> None:
        self._by_ref: dict[EntityRef, AdEntity] = {e.entity_ref: e for e in entities}

    async def get_by_ref(self, entity_ref: EntityRef) -> AdEntity | None:
        return self._by_ref.get(entity_ref)

    async def list_by_account(self, account_ref: AccountRef) -> Sequence[AdEntity]:
        return [e for e in self._by_ref.values() if self._belongs_to(e, account_ref)]

    async def save(self, entity: AdEntity) -> None:
        self._by_ref[entity.entity_ref] = entity

    async def save_many(self, entities: Sequence[AdEntity]) -> None:
        for entity in entities:
            await self.save(entity)

    def _belongs_to(self, entity: AdEntity, account_ref: AccountRef) -> bool:
        ref = entity.parent_ref
        while ref.level != EntityLevel.ACCOUNT:
            ancestor = self._by_ref.get(ref)
            if ancestor is None:
                return False
            ref = ancestor.parent_ref
        return (
            ref.platform == account_ref.platform
            and ref.external_id == account_ref.external_account_id
            and ref.connection_id == account_ref.connection_id
            and ref.business_id == account_ref.business_id
        )


class InMemoryCredentialRepository:
    def __init__(self, credentials: Iterable[PlatformCredential] = ()) -> None:
        self._by_id: dict[CredentialRefId, PlatformCredential] = {
            c.credential_ref_id: c for c in credentials
        }

    async def get(
        self,
        credential_ref_id: CredentialRefId,
        *,
        business_id: BusinessId,  # noqa: ARG002
    ) -> PlatformCredential | None:
        return self._by_id.get(credential_ref_id)

    async def save(self, credential: PlatformCredential) -> None:
        self._by_id[credential.credential_ref_id] = credential


class InMemoryOAuthConnectSessionRepository:
    def __init__(self, sessions: Iterable[OAuthConnectSession] = ()) -> None:
        self._by_id: dict[uuid.UUID, OAuthConnectSession] = {s.session_id: s for s in sessions}

    async def save(self, session: OAuthConnectSession) -> None:
        self._by_id[session.session_id] = session

    async def get_by_id(self, session_id: uuid.UUID) -> OAuthConnectSession | None:
        return self._by_id.get(session_id)

    async def get_by_state_hash(self, state_hash: str) -> OAuthConnectSession | None:
        for session in self._by_id.values():
            if session.state_hash == state_hash:
                return session
        return None

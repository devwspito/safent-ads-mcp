"""Guion bajo en el nombre del modulo: detalle interno compartido por
`CompleteOAuthConnect` y `RegisterMetaSystemUserToken` (mismo patron que
`accounts/domain/_aggregate.py`), no API publica de `application`.

Orden de escritura obligatorio: la credencial primero, la cuenta despues
-- `platform_accounts.credential_ref_id` referencia `credential_refs.id`
(FK de 0001_bootstrap)."""

from __future__ import annotations

import uuid

from safent_ads.accounts.application.connect_ports import CredentialRepository, DiscoveredAccount
from safent_ads.accounts.application.ports import AccountRepository
from safent_ads.accounts.domain.errors import AccountOwnershipConflictError
from safent_ads.accounts.domain.platform_account import PlatformAccount
from safent_ads.accounts.domain.platform_credential import PlatformCredential
from safent_ads.accounts.domain.refs import AccountRef
from safent_ads.shared.ids import BusinessId


async def upsert_discovered_account(
    accounts: AccountRepository,
    credentials: CredentialRepository,
    business_id: BusinessId,
    discovered: DiscoveredAccount,
) -> PlatformAccount:
    if discovered.business_id not in (None, str(business_id)):
        raise AccountOwnershipConflictError("connection_business_mismatch")
    await credentials.save(_new_credential(business_id, discovered))
    account = PlatformAccount(
        business_id=business_id,
        account_ref=AccountRef(
            discovered.platform,
            discovered.external_account_id,
            business_id.value if discovered.connection_id else None,
            uuid.UUID(discovered.connection_id) if discovered.connection_id else None,
        ),
        currency=discovered.currency,
        timezone=discovered.timezone,
        api_tier=discovered.api_tier,
        credential_ref_id=discovered.credential_ref_id,
        connection_owner_id=uuid.UUID(discovered.owner_id) if discovered.owner_id else None,
    )
    await accounts.save(account)
    return account


def _new_credential(business_id: BusinessId, discovered: DiscoveredAccount) -> PlatformCredential:
    return PlatformCredential(
        credential_ref_id=discovered.credential_ref_id,
        business_id=business_id,
        platform=discovered.platform,
        alias=str(discovered.credential_ref_id),
        scopes=frozenset(discovered.scopes),
        obtained_at=discovered.obtained_at,
        expires_at=discovered.expires_at,
    )

"""Resolve account credentials from the same encrypted store used by OAuth.

No decrypted-token cache: reconnect, expiry and revocation apply to the next
request, including after a broker restart. Google refresh is handled by its SDK.
"""

import json
from uuid import UUID

from safent_ads.accounts.application.ports import WriteIntent
from safent_ads.broker.application.connection_scope import current_connection_scope
from safent_ads.broker.application.ports import (
    ComposioAccountBinding,
    CredentialRecord,
    PlatformCredential,
)
from safent_ads.broker.domain.ledger_scope import LedgerScope, LedgerScopeError
from safent_ads.broker.infrastructure.credential_store import EncryptedCredentialStore
from safent_ads.shared.clock import Clock
from safent_ads.shared.ids import PlatformCode

_COMPOSIO_CONNECTION = "composio_connection"


class ConnectedCredentialStore:
    def __init__(
        self, store: EncryptedCredentialStore, clock: Clock, login_customer_id: str | None = None
    ) -> None:
        self._store = store
        self._clock = clock
        self._login_customer_id = login_customer_id

    async def get_credential(
        self, platform: PlatformCode, external_account_id: str
    ) -> PlatformCredential | None:
        record = self._connected_record(platform, external_account_id)
        if record is None:
            return None
        if record.token_type == _COMPOSIO_CONNECTION:
            binding = _composio_binding(record)
            if binding is None:
                return None
            return PlatformCredential(
                platform=platform,
                external_account_id=external_account_id,
                login_customer_id=binding.login_customer_id,
                composio=binding,
                scopes=record.scopes,
            )
        if platform == PlatformCode.GOOGLE:
            app = self._store.get_google_app_credentials()
            return PlatformCredential(
                platform=platform,
                external_account_id=external_account_id,
                refresh_token=record.token,
                login_customer_id=app.login_customer_id if app else self._login_customer_id,
                scopes=record.scopes,
            )
        return PlatformCredential(
            platform=platform,
            external_account_id=external_account_id,
            access_token=record.token,
            scopes=record.scopes,
        )

    def resolve_write_scope(self, intent: WriteIntent, external_account_id: str) -> LedgerScope:
        """Validate the signed reference against the encrypted server binding.

        Deliberately synchronous: no SDK call, token refresh or request-body-only
        fallback. Re-read on authorize and reserve so revocation is not cached.
        """
        current = current_connection_scope()
        ref = intent.entity_ref
        if (
            current is None
            or ref.business_id != current.business_id
            or ref.connection_id != current.connection_id
            or intent.business_id != str(current.business_id)
        ):
            raise LedgerScopeError("ledger_scope_unverified")
        record = self._connected_record(ref.platform, external_account_id)
        if record is None or record.business_id is None:
            raise LedgerScopeError("ledger_scope_unverified")
        return LedgerScope(UUID(record.business_id), record.platform, external_account_id)

    def _connected_record(
        self, platform: PlatformCode, external_account_id: str
    ) -> CredentialRecord | None:
        scope = current_connection_scope()
        if scope is None:
            return None  # Legacy references are history, never operational credentials.
        reference = self._store.account_credential_ref(
            platform,
            external_account_id,
            business_id=str(scope.business_id),
            connection_id=str(scope.connection_id),
        )
        record = self._store.get_credential(reference) if reference is not None else None
        if (
            record is None
            or record.platform != platform
            or record.business_id != str(scope.business_id)
            or record.connection_id != str(scope.connection_id)
            or not record.token
            or record.revoked_at is not None
            or (record.expires_at is not None and record.expires_at <= self._clock.now())
        ):
            return None
        valid_types = (
            {"refresh_token"}
            if platform == PlatformCode.GOOGLE
            else {"long_lived_token", "system_user_token"}
        )
        if record.token_type == _COMPOSIO_CONNECTION:
            return record if _composio_binding(record) is not None else None
        if record.token_type not in valid_types:
            return None
        return record


def _composio_binding(record: CredentialRecord) -> ComposioAccountBinding | None:
    """Only the broker's verified-connect completion may write this record.

    No legacy token, unverified callback query, or client request fallback.
    The existing encrypted alias binds platform/native account/business/connection.
    """
    try:
        payload = json.loads(record.token)
        if not isinstance(payload, dict):
            return None
        return ComposioAccountBinding(**payload)
    except (TypeError, ValueError):
        return None

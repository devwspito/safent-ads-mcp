"""Dobles compartidos por los tests de `accounts/application`."""

from __future__ import annotations

from collections.abc import Sequence
from datetime import UTC, datetime, timedelta

from safent_ads.accounts.application.connect_ports import (
    CredentialStatusResult,
    OAuthBeginResult,
    OAuthCompleteResult,
)
from safent_ads.accounts.application.errors import BrokerRequestDeniedError
from safent_ads.accounts.application.platform_apps_ports import (
    GoogleAppCredentialsInput,
    MetaAppCredentialsInput,
    PlatformAppStatus,
)
from safent_ads.accounts.application.ports import (
    AccountRef,
    AdEntitySnapshot,
    AdsPlatformPort,
    AssetUploadRequest,
    EntityStateSnapshot,
    IdempotencyKey,
    MetricFactSnapshot,
    MetricsRequest,
    PlatformAssetHandle,
    SignedAuthorization,
    WriteIntent,
    WriteOutcome,
)
from safent_ads.accounts.domain.refs import CredentialRefId
from safent_ads.shared.ids import EntityRef, PlatformCode

NOW = datetime(2026, 9, 9, tzinfo=UTC)


class FakeAdsPlatformPort(AdsPlatformPort):
    """Doble de `AdsPlatformPort`: cablea respuestas fijas por test, nunca
    toca red ni SDKs. `execute_write` siempre `DENIED` (contrato F1)."""

    def __init__(
        self,
        inventory: Sequence[AdEntitySnapshot] = (),
        entity_states: dict[EntityRef, EntityStateSnapshot] | None = None,
    ) -> None:
        self.inventory = list(inventory)
        self.entity_states = entity_states or {}

    async def fetch_account_inventory(
        self,
        account_ref: AccountRef,  # noqa: ARG002 - forma exacta del puerto
    ) -> Sequence[AdEntitySnapshot]:
        return self.inventory

    async def fetch_metrics(
        self,
        request: MetricsRequest,  # noqa: ARG002 - forma exacta del puerto
    ) -> Sequence[MetricFactSnapshot]:
        return []

    async def read_entity_state(self, entity_ref: EntityRef) -> EntityStateSnapshot:
        return self.entity_states[entity_ref]

    async def upload_asset(
        self,
        request: AssetUploadRequest,  # noqa: ARG002 - forma exacta del puerto
    ) -> PlatformAssetHandle:
        raise NotImplementedError

    async def execute_write(
        self,
        intent: WriteIntent,  # noqa: ARG002 - forma exacta del puerto
        authorization: SignedAuthorization,  # noqa: ARG002
        idempotency_key: IdempotencyKey,  # noqa: ARG002
    ) -> WriteOutcome:
        return WriteOutcome(
            outcome="DENIED",
            applied_value=None,
            state_hash_after=None,
            error_code="NO_WRITE_PATH_IN_F1",
            platform_request_id=None,
        )


class FakeEventBus:
    def __init__(self) -> None:
        self.published: list[object] = []

    async def publish(self, event: object) -> None:
        self.published.append(event)


class FakeOAuthBrokerPort:
    """Doble de `OAuthBrokerPort`: nunca toca un socket. `deny` simula
    `BrokerRequestDeniedError` para probar la traduccion a errores de
    `application` sin depender del broker real."""

    def __init__(
        self,
        *,
        begin_result: OAuthBeginResult | None = None,
        complete_result: OAuthCompleteResult | None = None,
        status_result: CredentialStatusResult | None = None,
        deny: BrokerRequestDeniedError | None = None,
    ) -> None:
        self.begin_result = begin_result
        self.complete_result = complete_result
        self.status_result = status_result
        self.deny = deny
        self.revoked: list[CredentialRefId] = []
        self.received_complete_calls: list[tuple[str, str]] = []

    async def begin(
        self,
        provider: PlatformCode,
        business_id: object,
        redirect_uri: str,  # noqa: ARG002
        *,
        owner_id: str | None = None,  # noqa: ARG002
    ) -> OAuthBeginResult:
        del provider, business_id, redirect_uri, owner_id
        if self.deny is not None:
            raise self.deny
        assert self.begin_result is not None
        return self.begin_result

    async def complete(self, state: str, code: str) -> OAuthCompleteResult:
        self.received_complete_calls.append((state, code))
        if self.deny is not None:
            raise self.deny
        assert self.complete_result is not None
        return self.complete_result

    async def credential_status(self, credential_ref_id: CredentialRefId) -> CredentialStatusResult:  # noqa: ARG002
        if self.deny is not None:
            raise self.deny
        assert self.status_result is not None
        return self.status_result

    async def revoke_credential(self, credential_ref_id: CredentialRefId) -> None:
        if self.deny is not None:
            raise self.deny
        self.revoked.append(credential_ref_id)

    async def register_meta_system_user_token(
        self, token: str, *, business_id: str | None = None, owner_id: str | None = None
    ) -> OAuthCompleteResult:  # noqa: ARG002
        del token, business_id, owner_id
        if self.deny is not None:
            raise self.deny
        assert self.complete_result is not None
        return self.complete_result


class FakePlatformAppsBrokerPort:
    """Doble de `PlatformAppsBrokerPort`: nunca toca un socket. `deny`
    simula `BrokerRequestDeniedError`, mismo patron que
    `FakeOAuthBrokerPort`."""

    def __init__(
        self,
        *,
        status_result: PlatformAppStatus | None = None,
        deny: BrokerRequestDeniedError | None = None,
    ) -> None:
        self.status_result = status_result
        self.deny = deny
        self.received_google: list[GoogleAppCredentialsInput] = []
        self.received_meta: list[MetaAppCredentialsInput] = []
        self.deleted: list[PlatformCode] = []

    async def set_google_app_credentials(
        self, credentials: GoogleAppCredentialsInput
    ) -> PlatformAppStatus:
        self.received_google.append(credentials)
        if self.deny is not None:
            raise self.deny
        assert self.status_result is not None
        return self.status_result

    async def set_meta_app_credentials(
        self, credentials: MetaAppCredentialsInput
    ) -> PlatformAppStatus:
        self.received_meta.append(credentials)
        if self.deny is not None:
            raise self.deny
        assert self.status_result is not None
        return self.status_result

    async def get_app_status(self, platform: PlatformCode) -> PlatformAppStatus:  # noqa: ARG002
        if self.deny is not None:
            raise self.deny
        assert self.status_result is not None
        return self.status_result

    async def delete_app_credentials(self, platform: PlatformCode) -> None:
        self.deleted.append(platform)
        if self.deny is not None:
            raise self.deny


def default_begin_result(*, expires_in: timedelta = timedelta(minutes=10)) -> OAuthBeginResult:
    return OAuthBeginResult(
        authorization_url="https://accounts.google.com/auth?state=abc123",
        state="abc123",
        expires_at=NOW + expires_in,
    )

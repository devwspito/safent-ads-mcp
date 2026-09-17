"""`OAuthConnectFlow`: orquesta el flujo "Conectar" de principio a fin —
genera `state`/PKCE, canjea el `code` con el proveedor, descubre las
cuentas accesibles y las guarda cifradas. `ads-api` nunca ve el token
(contracts/rest-api.md §Conexiones): solo recibe de vuelta
`authorization_url`/`state`, y metadatos de las cuentas descubiertas
(`DiscoveredAccount`, sin campo de token)."""

from __future__ import annotations

import uuid
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import TYPE_CHECKING, Protocol

from safent_ads.accounts.domain.google_customer_id import normalize_google_customer_id
from safent_ads.accounts.domain.platform_account import ApiTier
from safent_ads.accounts.domain.refs import CredentialRefId
from safent_ads.broker.application.errors import (
    CredentialNotFoundError,
    GoogleProjectAccessDeniedError,
    OAuthProviderDeniedError,
    OAuthSessionExpiredError,
    OAuthSessionNotFoundError,
)
from safent_ads.broker.application.oauth_state import (
    generate_pkce_verifier,
    generate_state,
    hash_state,
    pkce_challenge,
)
from safent_ads.broker.application.ports import (
    ConnectSessionRecord,
    CredentialRecord,
    OAuthConnectStorePort,
)
from safent_ads.broker.platforms.google_oauth_adapter import (
    GoogleCustomerMetadata,
    GoogleOAuthError,
    GoogleOAuthTokens,
)
from safent_ads.broker.platforms.meta_oauth_adapter import (
    MetaAdAccount,
    MetaOAuthError,
    MetaOAuthTokens,
)
from safent_ads.broker.platforms.oauth_http import GoogleCloudProjectAccessError, OAuthHttpError
from safent_ads.shared.clock import Clock
from safent_ads.shared.ids import PlatformCode

if TYPE_CHECKING:
    from safent_ads.broker.application.managed_oauth_connect import ManagedOAuthConnectService


class GoogleOAuthProvider(Protocol):
    """Forma que `OAuthConnectFlow` necesita de Google -- `GoogleOAuthAdapter`
    (credenciales estaticas) y `broker/platforms/dynamic_oauth_adapters.py
    ::DynamicGoogleOAuthAdapter` (credenciales resueltas del almacen cifrado
    en cada llamada, app-credentials-ui) la satisfacen las dos, sin que este
    orquestador conozca cual es cual (DIP)."""

    def authorization_url(self, *, state: str, code_challenge: str, redirect_uri: str) -> str: ...

    async def exchange_code(
        self, *, code: str, code_verifier: str, redirect_uri: str
    ) -> GoogleOAuthTokens: ...

    async def list_accessible_customers(self, access_token: str) -> Sequence[str]: ...

    async def fetch_customer_metadata(
        self, customer_id: str, access_token: str
    ) -> GoogleCustomerMetadata: ...


class MetaOAuthProvider(Protocol):
    """Forma que `OAuthConnectFlow` necesita de Meta -- mismo criterio que
    `GoogleOAuthProvider`."""

    def authorization_url(self, *, state: str, redirect_uri: str) -> str: ...

    async def exchange_code_for_long_lived_token(
        self, *, code: str, redirect_uri: str
    ) -> MetaOAuthTokens: ...

    async def list_ad_accounts(self, access_token: str) -> Sequence[MetaAdAccount]: ...

    async def validate_system_user_token(self, token: str) -> Sequence[MetaAdAccount]: ...


_SESSION_TTL = timedelta(minutes=10)
_REFRESH_TOKEN = "refresh_token"  # noqa: S105 - nombre de tipo de credencial, no secreto
_LONG_LIVED_TOKEN = "long_lived_token"  # noqa: S105
_SYSTEM_USER_TOKEN = "system_user_token"  # noqa: S105
_META_SCOPES: tuple[str, ...] = ("ads_read", "ads_management", "business_management")


@dataclass(frozen=True, slots=True)
class DiscoveredAccount:
    """Nunca lleva el token: solo lo que `ads-api` necesita para crear/
    actualizar un `PlatformAccount` y su `credential_refs`."""

    platform: PlatformCode
    external_account_id: str
    label: str
    currency: str
    timezone: str
    api_tier: ApiTier
    credential_ref_id: CredentialRefId
    scopes: tuple[str, ...]
    obtained_at: datetime
    expires_at: datetime | None
    business_id: str | None = None
    connection_id: str | None = None
    owner_id: str | None = None


@dataclass(frozen=True, slots=True)
class BeginResult:
    authorization_url: str
    state: str
    expires_at: datetime
    connection_id: str | None = None


@dataclass(frozen=True, slots=True)
class CompleteResult:
    accounts: tuple[DiscoveredAccount, ...]


@dataclass(frozen=True, slots=True)
class StatusResult:
    status: str
    scopes: tuple[str, ...]
    expires_at: datetime | None
    last_validated_at: datetime | None


class OAuthConnectFlow:
    def __init__(
        self,
        store: OAuthConnectStorePort,
        google: GoogleOAuthProvider,
        meta: MetaOAuthProvider,
        clock: Clock,
        *,
        managed: ManagedOAuthConnectService | None = None,
    ) -> None:
        self._store = store
        self._google = google
        self._meta = meta
        self._clock = clock
        self._managed = managed

    async def begin(
        self,
        *,
        provider: PlatformCode,
        business_id: str,
        redirect_uri: str,
        owner_id: str | None = None,
        google_customer_id: str | None = None,
    ) -> BeginResult:
        customer_id = normalize_google_customer_id(google_customer_id, provider=provider)
        selection = {"google_customer_id": customer_id} if customer_id is not None else {}
        if self._managed is not None and self._managed.configured(provider):
            return await self._managed.begin(
                provider=provider,
                business_id=business_id,
                redirect_uri=redirect_uri,
                owner_id=owner_id,
                **selection,
            )
        if self._managed is not None and self._managed.required:
            raise OAuthProviderDeniedError("managed_configuration_missing")
        state = generate_state()
        verifier = generate_pkce_verifier() if provider == PlatformCode.GOOGLE else None
        authorization_url = self._authorization_url(provider, state, verifier, redirect_uri)
        expires_at = self._clock.now() + _SESSION_TTL
        connection_id = str(uuid.uuid4())

        self._store.save_connect_session(
            hash_state(state),
            ConnectSessionRecord(
                provider=provider,
                business_id=business_id,
                redirect_uri=redirect_uri,
                pkce_verifier=verifier,
                created_at=self._clock.now(),
                expires_at=expires_at,
                connection_id=connection_id,
                owner_id=owner_id,
                google_customer_id=customer_id,
            ),
        )
        return BeginResult(
            authorization_url=authorization_url,
            state=state,
            expires_at=expires_at,
            connection_id=connection_id,
        )

    async def complete(self, *, state: str, code: str) -> CompleteResult:
        session = self._pop_valid_session(state)
        if session.managed_connection_id is not None:
            if self._managed is None:
                raise OAuthProviderDeniedError("managed_connection_unavailable")
            return await self._managed.complete(session, connected_account_id=code)
        if session.provider == PlatformCode.GOOGLE:
            return await self._complete_google(session, code=code)
        return await self._complete_meta(session, code=code)

    async def register_meta_system_user_token(
        self,
        *,
        token: str,
        business_id: str | None = None,
        owner_id: str | None = None,
    ) -> CompleteResult:
        try:
            ad_accounts = await self._meta.validate_system_user_token(token)
        except (MetaOAuthError, OAuthHttpError) as exc:
            raise OAuthProviderDeniedError(str(exc)) from exc
        tokens = MetaOAuthTokens(access_token=token, expires_at=None)
        session = (
            None
            if business_id is None
            else ConnectSessionRecord(
                PlatformCode.META,
                business_id,
                "",
                None,
                self._clock.now(),
                self._clock.now() + _SESSION_TTL,
                str(uuid.uuid4()),
                owner_id,
            )
        )
        return self._save_meta_accounts(
            tokens, ad_accounts, token_type=_SYSTEM_USER_TOKEN, session=session
        )

    async def status(self, credential_ref_id: CredentialRefId) -> StatusResult:
        record = self._store.get_credential(credential_ref_id)
        if record is None:
            raise CredentialNotFoundError(str(credential_ref_id))
        return StatusResult(
            status=self._status_of(record),
            scopes=record.scopes,
            expires_at=record.expires_at,
            last_validated_at=None,
        )

    async def revoke(self, credential_ref_id: CredentialRefId) -> None:
        self._store.revoke_credential(credential_ref_id, at=self._clock.now())

    def _pop_valid_session(self, state: str) -> ConnectSessionRecord:
        session = self._store.pop_connect_session(hash_state(state))
        if session is None:
            raise OAuthSessionNotFoundError("state desconocido o ya consumido")
        if session.expires_at <= self._clock.now():
            raise OAuthSessionExpiredError("sesion OAuth caducada")
        return session

    def _status_of(self, record: CredentialRecord) -> str:
        if record.revoked_at is not None:
            return "revoked"
        if record.expires_at is not None and record.expires_at <= self._clock.now():
            return "expired"
        return "connected"

    def _authorization_url(
        self, provider: PlatformCode, state: str, verifier: str | None, redirect_uri: str
    ) -> str:
        if provider == PlatformCode.GOOGLE:
            assert verifier is not None  # noqa: S101 - invariante: Google siempre lleva PKCE
            return self._google.authorization_url(
                state=state, code_challenge=pkce_challenge(verifier), redirect_uri=redirect_uri
            )
        return self._meta.authorization_url(state=state, redirect_uri=redirect_uri)

    async def _complete_google(self, session: ConnectSessionRecord, *, code: str) -> CompleteResult:
        assert session.pkce_verifier is not None  # noqa: S101 - invariante de `begin`
        try:
            tokens = await self._google.exchange_code(
                code=code, code_verifier=session.pkce_verifier, redirect_uri=session.redirect_uri
            )
            customer_ids = await self._google.list_accessible_customers(tokens.access_token)
            accounts_metadata = [
                await self._google.fetch_customer_metadata(customer_id, tokens.access_token)
                for customer_id in customer_ids
            ]
        except GoogleCloudProjectAccessError as exc:
            raise GoogleProjectAccessDeniedError("google_project_access_level_test") from exc
        except (GoogleOAuthError, OAuthHttpError) as exc:
            raise OAuthProviderDeniedError(str(exc)) from exc

        return CompleteResult(
            accounts=tuple(
                self._save_one_google_account(metadata, tokens, session)
                for metadata in accounts_metadata
            )
        )

    def _save_one_google_account(
        self,
        metadata: GoogleCustomerMetadata,
        tokens: GoogleOAuthTokens,
        session: ConnectSessionRecord,
    ) -> DiscoveredAccount:
        credential_ref_id = CredentialRefId(uuid.uuid4())
        self._store.save_credential(
            credential_ref_id,
            CredentialRecord(
                platform=PlatformCode.GOOGLE,
                token=tokens.refresh_token,
                token_type=_REFRESH_TOKEN,
                scopes=tokens.scopes,
                obtained_at=self._clock.now(),
                # Google no expone caducidad del refresh token: solo revoca
                # (por el propietario o por inactividad de 6 meses).
                expires_at=None,
                business_id=session.business_id,
                connection_id=session.connection_id,
                owner_id=session.owner_id,
            ),
        )
        self._store.bind_account_credential(
            PlatformCode.GOOGLE,
            metadata.customer_id,
            credential_ref_id,
            business_id=session.business_id,
            connection_id=session.connection_id,
        )
        return DiscoveredAccount(
            platform=PlatformCode.GOOGLE,
            external_account_id=metadata.customer_id,
            label=metadata.descriptive_name,
            currency=metadata.currency,
            timezone=metadata.timezone,
            api_tier=ApiTier.GOOGLE_EXPLORER,
            credential_ref_id=credential_ref_id,
            scopes=tokens.scopes,
            obtained_at=self._clock.now(),
            expires_at=None,
            business_id=session.business_id,
            connection_id=session.connection_id,
            owner_id=session.owner_id,
        )

    async def _complete_meta(self, session: ConnectSessionRecord, *, code: str) -> CompleteResult:
        try:
            tokens = await self._meta.exchange_code_for_long_lived_token(
                code=code, redirect_uri=session.redirect_uri
            )
            ad_accounts = await self._meta.list_ad_accounts(tokens.access_token)
        except (MetaOAuthError, OAuthHttpError) as exc:
            raise OAuthProviderDeniedError(str(exc)) from exc
        return self._save_meta_accounts(
            tokens, ad_accounts, token_type=_LONG_LIVED_TOKEN, session=session
        )

    def _save_meta_accounts(
        self,
        tokens: MetaOAuthTokens,
        ad_accounts: Sequence[MetaAdAccount],
        *,
        token_type: str,
        session: ConnectSessionRecord | None = None,
    ) -> CompleteResult:
        accounts = tuple(
            self._save_one_meta_account(account, tokens, token_type=token_type, session=session)
            for account in ad_accounts
        )
        return CompleteResult(accounts=accounts)

    def _save_one_meta_account(
        self,
        account: MetaAdAccount,
        tokens: MetaOAuthTokens,
        *,
        token_type: str,
        session: ConnectSessionRecord | None = None,
    ) -> DiscoveredAccount:
        credential_ref_id = CredentialRefId(uuid.uuid4())
        self._store.save_credential(
            credential_ref_id,
            CredentialRecord(
                platform=PlatformCode.META,
                token=tokens.access_token,
                token_type=token_type,
                scopes=_META_SCOPES,
                obtained_at=self._clock.now(),
                expires_at=tokens.expires_at,
                business_id=session.business_id if session else None,
                connection_id=session.connection_id if session else None,
                owner_id=session.owner_id if session else None,
            ),
        )
        self._store.bind_account_credential(
            PlatformCode.META,
            account.account_id,
            credential_ref_id,
            business_id=session.business_id if session else None,
            connection_id=session.connection_id if session else None,
        )
        return DiscoveredAccount(
            platform=PlatformCode.META,
            external_account_id=account.account_id,
            label=account.name,
            currency=account.currency,
            timezone=account.timezone_name,
            api_tier=ApiTier.META_LIMITED,
            credential_ref_id=credential_ref_id,
            scopes=_META_SCOPES,
            obtained_at=self._clock.now(),
            expires_at=tokens.expires_at,
            business_id=session.business_id if session else None,
            connection_id=session.connection_id if session else None,
            owner_id=session.owner_id if session else None,
        )

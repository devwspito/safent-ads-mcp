"""Puerto hacia la infraestructura de credenciales de CLIENTE (integracion,
regla de producto): el propietario conecta su cuenta de Google/Meta por
OAuth desde el panel; el secreto resultante (refresh token / system user
token) se cifra y vive en `platform_credentials`, nunca en variables de
entorno. `BrokerSettings` solo declara credenciales de VENDOR --
`client_id`/`client_secret` (app OAuth), `app_id`/`app_secret` --
compartidas por toda cuenta conectada de esa plataforma.

`broker/infrastructure/credential_store.py` (AES-256-GCM sobre la tabla
`platform_credentials`, mas los ops de socket `oauth_begin`/
`oauth_complete`/`credential_status`/`revoke_credential`) es responsabilidad
de la lane `us3-oauth-connect`, en paralelo. Este puerto es el contrato que
esa lane implementa de verdad; hasta que aterrice,
`InMemoryCredentialStore` (`broker/infrastructure/`) no tiene ninguna
cuenta conectada -- todo adaptador que la consulte falla cerrado con
`CredentialNotConnectedError` (`broker/platforms/errors.py`), nunca con un
token inventado ni leido de un fichero `.env` por cuenta."""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import datetime
from typing import Literal, Protocol

from safent_ads.accounts.domain.google_customer_id import normalize_google_customer_id
from safent_ads.accounts.domain.refs import CredentialRefId
from safent_ads.shared.ids import PlatformCode


@dataclass(frozen=True, slots=True)
class ComposioAccountBinding:
    """Server-verified connection identifiers, never native provider tokens."""

    connected_account_id: str = field(repr=False)
    user_id: str = field(repr=False)
    auth_config_id: str = field(repr=False)
    login_customer_id: str | None = None

    def __post_init__(self) -> None:
        if (
            not re.fullmatch(r"ca_[A-Za-z0-9_-]{1,128}", self.connected_account_id)
            or not re.fullmatch(r"ac_[A-Za-z0-9_-]{1,128}", self.auth_config_id)
            or not re.fullmatch(r"[A-Za-z0-9_.:@-]{1,200}", self.user_id)
            or (
                self.login_customer_id is not None
                and not re.fullmatch(r"[0-9]{1,20}", self.login_customer_id)
            )
        ):
            raise ValueError("composio_binding_invalid")


@dataclass(frozen=True, slots=True)
class PlatformCredential:
    """Secreto de CLIENTE ya descifrado, resuelto para una cuenta externa
    concreta. `refresh_token`/`login_customer_id` tienen sentido solo para
    Google (OAuth 2.0 + MCC de acceso); `access_token` (system user, no
    caduca) solo para Meta -- el campo que no aplica a la plataforma queda
    `None`."""

    platform: PlatformCode
    external_account_id: str
    refresh_token: str | None = field(default=None, repr=False)
    login_customer_id: str | None = None
    access_token: str | None = field(default=None, repr=False)
    composio: ComposioAccountBinding | None = field(default=None, repr=False)
    scopes: tuple[str, ...] = ()


class CredentialStorePort(Protocol):
    """`us3-oauth-connect` implementa esto de verdad. El broker nunca lee
    un secreto de CLIENTE de `BrokerSettings`/entorno -- solo de aqui."""

    async def get_credential(
        self, platform: PlatformCode, external_account_id: str
    ) -> PlatformCredential | None: ...


@dataclass(frozen=True, slots=True)
class ConnectSessionRecord:
    """El `state`/`code_verifier` PKCE en vuelo de un flujo OAuth "Conectar"
    (contracts/rest-api.md §Conexiones, paso 1), antes de canjear el
    `code`."""

    provider: PlatformCode
    business_id: str
    redirect_uri: str
    pkce_verifier: str | None = field(repr=False)
    created_at: datetime
    expires_at: datetime
    connection_id: str | None = None
    owner_id: str | None = None
    managed_connection_id: str | None = None
    managed_user_id: str | None = None
    managed_auth_config_id: str | None = None
    google_customer_id: str | None = None

    def __post_init__(self) -> None:
        object.__setattr__(
            self,
            "google_customer_id",
            normalize_google_customer_id(self.google_customer_id, provider=self.provider),
        )


@dataclass(frozen=True, slots=True)
class CredentialRecord:
    """El token ya canjeado (refresh token de Google, token de Meta) de una
    cuenta ya conectada, indexado por `CredentialRefId`."""

    platform: PlatformCode
    token: str = field(repr=False)
    token_type: str
    scopes: tuple[str, ...]
    obtained_at: datetime
    expires_at: datetime | None
    revoked_at: datetime | None = None
    business_id: str | None = None
    connection_id: str | None = None
    owner_id: str | None = None


class OAuthConnectStorePort(Protocol):
    """Persistencia del flujo OAuth "Conectar" (US3): sesion en vuelo +
    credencial ya canjeada. `broker/infrastructure/credential_store.py`
    (`EncryptedCredentialStore`, AES-256-GCM) es su unica implementacion de
    produccion -- distinto de `CredentialStorePort` de arriba, que resuelve
    credenciales YA conectadas para las lecturas/escrituras de US1/F2, no el
    flujo de conexion en si."""

    def save_connect_session(self, state_hash: str, record: ConnectSessionRecord) -> None: ...

    def pop_connect_session(self, state_hash: str) -> ConnectSessionRecord | None: ...

    def save_credential(
        self, credential_ref_id: CredentialRefId, record: CredentialRecord
    ) -> None: ...

    def get_credential(self, credential_ref_id: CredentialRefId) -> CredentialRecord | None: ...

    def revoke_credential(self, credential_ref_id: CredentialRefId, *, at: datetime) -> None: ...

    def bind_account_credential(
        self,
        platform: PlatformCode,
        external_account_id: str,
        credential_ref_id: CredentialRefId,
        *,
        business_id: str | None = None,
        connection_id: str | None = None,
    ) -> None: ...


# --- lane: app-credentials-ui ---
@dataclass(frozen=True, slots=True, kw_only=True)
class GoogleAppCredentials:
    """Credenciales de VENDOR de Google (OAuth client de la app, mas la
    MCC opcional), tecleadas por el propietario desde el
    panel (owner decision: "cada usuario puede tener datos diferentes",
    nunca desde `vendor.env`). `login_customer_id` es opcional -- no toda
    instalacion opera bajo una MCC."""

    client_id: str
    client_secret: str = field(repr=False)
    login_customer_id: str | None
    updated_at: datetime
    client_type: Literal["web", "desktop"] = "web"


@dataclass(frozen=True, slots=True, kw_only=True)
class MetaAppCredentials:
    """Credenciales de VENDOR de Meta (app de Facebook Login for
    Business), tecleadas por el propietario desde el panel."""

    app_id: str
    app_secret: str = field(repr=False)
    updated_at: datetime


class AppCredentialsStorePort(Protocol):
    """Persistencia de las credenciales de VENDOR (`GoogleAppCredentials`/
    `MetaAppCredentials`), una por plataforma, por instalacion --
    `broker/infrastructure/credential_store.py::EncryptedCredentialStore`
    es su unica implementacion de produccion, mismo cifrado AES-256-GCM
    que `OAuthConnectStorePort`. Sustituye a `BrokerSettings.google_ads_*`/
    `meta_app_*` como fuente principal; el entorno queda como respaldo de
    desarrollo (`composition/broker.py`)."""

    def save_google_app_credentials(self, credentials: GoogleAppCredentials) -> None: ...

    def get_google_app_credentials(self) -> GoogleAppCredentials | None: ...

    def save_meta_app_credentials(self, credentials: MetaAppCredentials) -> None: ...

    def get_meta_app_credentials(self) -> MetaAppCredentials | None: ...

    def delete_app_credentials(self, platform: PlatformCode) -> None: ...


# --- end lane: app-credentials-ui ---

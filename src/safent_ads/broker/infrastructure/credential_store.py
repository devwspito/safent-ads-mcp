"""Almacen cifrado de credenciales de CLIENTE y de VENDOR del broker
(threat-model.md C-24: "Secretos solo en entorno o secret manager, 0600;
nunca en BD").

Cuatro tipos de registro, cada uno un fichero AES-256-GCM independiente
bajo `BrokerSettings.credential_store_dir`: version || nonce(12B) ||
ciphertext_con_tag. El AAD vincula categoria e identificador del registro;
no se admite el formato anterior sin esa vinculacion.

- `ConnectSessionRecord`: el `state`/`code_verifier` PKCE en vuelo de un
  flujo OAuth "Conectar" (contracts/rest-api.md §Conexiones, paso 1). Se
  consume una unica vez (`pop_connect_session`): leer y borrar es la unica
  forma de recuperarlo, asi que un replay del `state` nunca encuentra nada.
- `CredentialRecord`: el token ya canjeado (refresh token de Google, token
  de Meta) de una cuenta ya conectada, indexado por `CredentialRefId`.
- `GoogleAppCredentials`/`MetaAppCredentials`: las credenciales de VENDOR
  (OAuth client de Google, app de Meta) que el
  propietario teclea desde el panel (owner decision, app-credentials-ui) --
  un fichero fijo por plataforma (`google.enc`/`meta.enc`), no por cuenta.

Fail closed sin excepcion de entorno: `credential_master_key` es
obligatoria en `BrokerSettings` (sin valor por defecto, igual que
`ADS_TOTP_ENC_KEY`/`ADS_APPROVAL_SIGNING_KEY") — no existe en este
repositorio ningun secreto con una via de arranque mas permisiva "en
desarrollo", y este no iba a ser el primero."""

from __future__ import annotations

import base64
import hashlib
import json
import os
import re
import tempfile
import uuid
from dataclasses import asdict
from datetime import datetime
from pathlib import Path
from typing import Any

from cryptography.hazmat.primitives.ciphers.aead import AESGCM

from safent_ads.accounts.domain.refs import CredentialRefId
from safent_ads.broker.application.ports import (
    ConnectSessionRecord,
    CredentialRecord,
    GoogleAppCredentials,
    MetaAppCredentials,
)
from safent_ads.shared.errors import InfrastructureError
from safent_ads.shared.ids import PlatformCode

_NONCE_LENGTH_BYTES = 12
_KEY_LENGTH_BYTES = 32
_FILE_MODE = 0o600
_DIR_MODE = 0o700
_FORMAT = b"safent-ads-credential:v2\x00"


class CredentialStoreKeyError(InfrastructureError):
    """`ADS_CREDENTIAL_MASTER_KEY` ausente o no decodifica a 32 bytes."""


def _decode_master_key(key_b64: str) -> bytes:
    if not key_b64:
        raise CredentialStoreKeyError("ADS_CREDENTIAL_MASTER_KEY vacio o ausente")
    try:
        key_bytes = base64.b64decode(key_b64, validate=True)
    except (ValueError, TypeError) as exc:
        raise CredentialStoreKeyError("ADS_CREDENTIAL_MASTER_KEY no es base64 valido") from exc
    if len(key_bytes) != _KEY_LENGTH_BYTES:
        raise CredentialStoreKeyError(
            f"ADS_CREDENTIAL_MASTER_KEY debe decodificar a {_KEY_LENGTH_BYTES} bytes"
        )
    return key_bytes


def _to_json(
    record: ConnectSessionRecord | CredentialRecord | GoogleAppCredentials | MetaAppCredentials,
) -> bytes:
    payload = {
        key: value.isoformat() if isinstance(value, datetime) else value
        for key, value in asdict(record).items()
    }
    return json.dumps(payload, sort_keys=True).encode("utf-8")


def _optional_datetime(raw: str | None) -> datetime | None:
    return None if raw is None else datetime.fromisoformat(raw)


def _connect_session_from_json(raw: dict[str, Any]) -> ConnectSessionRecord:
    return ConnectSessionRecord(
        provider=PlatformCode(raw["provider"]),
        business_id=raw["business_id"],
        redirect_uri=raw["redirect_uri"],
        pkce_verifier=raw["pkce_verifier"],
        created_at=datetime.fromisoformat(raw["created_at"]),
        expires_at=datetime.fromisoformat(raw["expires_at"]),
        connection_id=raw.get("connection_id"),
        owner_id=raw.get("owner_id"),
        managed_connection_id=raw.get("managed_connection_id"),
        managed_user_id=raw.get("managed_user_id"),
        managed_auth_config_id=raw.get("managed_auth_config_id"),
        google_customer_id=raw.get("google_customer_id"),
    )


def _credential_from_json(raw: dict[str, Any]) -> CredentialRecord:
    return CredentialRecord(
        platform=PlatformCode(raw["platform"]),
        token=raw["token"],
        token_type=raw["token_type"],
        scopes=tuple(raw["scopes"]),
        obtained_at=datetime.fromisoformat(raw["obtained_at"]),
        expires_at=_optional_datetime(raw["expires_at"]),
        revoked_at=_optional_datetime(raw["revoked_at"]),
        business_id=raw.get("business_id"),
        connection_id=raw.get("connection_id"),
        owner_id=raw.get("owner_id"),
    )


def _google_app_credentials_from_json(raw: dict[str, Any]) -> GoogleAppCredentials:
    return GoogleAppCredentials(
        client_id=raw["client_id"],
        client_secret=raw["client_secret"],
        client_type=raw.get("client_type", "web"),
        login_customer_id=raw["login_customer_id"],
        updated_at=datetime.fromisoformat(raw["updated_at"]),
    )


def _meta_app_credentials_from_json(raw: dict[str, Any]) -> MetaAppCredentials:
    return MetaAppCredentials(
        app_id=raw["app_id"],
        app_secret=raw["app_secret"],
        updated_at=datetime.fromisoformat(raw["updated_at"]),
    )


class EncryptedCredentialStore:
    """Un fichero por registro, nunca una base de datos compartida —
    coherente con que `ads-broker` no tiene `database_url`
    (composition/settings.py)."""

    def __init__(self, store_dir: Path, master_key_b64: str) -> None:
        self._aesgcm = AESGCM(_decode_master_key(master_key_b64))
        self._sessions_dir = store_dir / "sessions"
        self._credentials_dir = store_dir / "credentials"
        self._app_credentials_dir = store_dir / "app_credentials"
        self._accounts_dir = store_dir / "accounts"
        self._sessions_dir.mkdir(parents=True, exist_ok=True, mode=_DIR_MODE)
        self._credentials_dir.mkdir(parents=True, exist_ok=True, mode=_DIR_MODE)
        self._app_credentials_dir.mkdir(parents=True, exist_ok=True, mode=_DIR_MODE)
        self._accounts_dir.mkdir(parents=True, exist_ok=True, mode=_DIR_MODE)

    def bind_account_credential(
        self,
        platform: PlatformCode,
        external_account_id: str,
        credential_ref_id: CredentialRefId,
        *,
        business_id: str | None = None,
        connection_id: str | None = None,
    ) -> None:
        record = self.get_credential(credential_ref_id)
        if record is None or record.platform != platform:
            raise CredentialStoreKeyError("credencial ausente o de otra plataforma")
        if (record.business_id, record.connection_id) != (business_id, connection_id):
            raise CredentialStoreKeyError("credencial de otra conexión o negocio")
        self._write(
            self._account_path(platform, external_account_id, business_id, connection_id),
            json.dumps({"credential_ref_id": str(credential_ref_id)}).encode(),
        )

    def account_credential_ref(
        self,
        platform: PlatformCode,
        external_account_id: str,
        *,
        business_id: str | None = None,
        connection_id: str | None = None,
    ) -> CredentialRefId | None:
        try:
            path = self._account_path(platform, external_account_id, business_id, connection_id)
            raw = path.read_bytes()
        except FileNotFoundError:
            return None
        return CredentialRefId(uuid.UUID(json.loads(self._decrypt(raw, path))["credential_ref_id"]))

    def _account_path(
        self,
        platform: PlatformCode,
        external_account_id: str,
        business_id: str | None = None,
        connection_id: str | None = None,
    ) -> Path:
        # Hashing avoids path traversal and keeps account identifiers out of filenames.
        if (business_id is None) != (connection_id is None):
            raise CredentialStoreKeyError("scope de conexión incompleto")
        identity = f"{platform.value}:{external_account_id}"
        if connection_id is not None:
            identity = json.dumps(
                [platform.value, business_id, str(uuid.UUID(connection_id)), external_account_id]
            )
        digest = hashlib.sha256(identity.encode()).hexdigest()
        return self._accounts_dir / f"{digest}.enc"

    def save_connect_session(self, state_hash: str, record: ConnectSessionRecord) -> None:
        self._write(self._session_path(state_hash), _to_json(record))

    def pop_connect_session(self, state_hash: str) -> ConnectSessionRecord | None:
        """Claim atomically BEFORE reading, across threads/processes/restarts.

        A process crash after claiming consumes the session (fail closed).
        Authenticate against the original record ID, not the temporary name.
        """
        path = self._session_path(state_hash)
        claimed = self._sessions_dir / f".consumed-{uuid.uuid4().hex}"
        try:
            path.rename(claimed)
        except FileNotFoundError:
            return None
        try:
            return _connect_session_from_json(json.loads(self._decrypt(claimed.read_bytes(), path)))
        finally:
            claimed.unlink(missing_ok=True)

    def _session_path(self, state_hash: str) -> Path:
        if not re.fullmatch(r"[A-Za-z0-9_-]{1,128}", state_hash):
            raise CredentialStoreKeyError("identificador de sesion no valido")
        return self._sessions_dir / f"{state_hash}.enc"

    def save_credential(self, credential_ref_id: CredentialRefId, record: CredentialRecord) -> None:
        self._write(self._credential_path(credential_ref_id), _to_json(record))

    def get_credential(self, credential_ref_id: CredentialRefId) -> CredentialRecord | None:
        path = self._credential_path(credential_ref_id)
        try:
            raw = path.read_bytes()
        except FileNotFoundError:
            return None
        return _credential_from_json(json.loads(self._decrypt(raw, path)))

    def revoke_credential(self, credential_ref_id: CredentialRefId, *, at: datetime) -> None:
        """Sobrescribe el token con una cadena vacia: el fichero deja de
        contener secreto util aunque el registro de estado permanezca."""
        existing = self.get_credential(credential_ref_id)
        if existing is None:
            return
        revoked = CredentialRecord(
            platform=existing.platform,
            token="",
            token_type=existing.token_type,
            scopes=existing.scopes,
            obtained_at=existing.obtained_at,
            expires_at=existing.expires_at,
            revoked_at=at,
            business_id=existing.business_id,
            connection_id=existing.connection_id,
            owner_id=existing.owner_id,
        )
        self.save_credential(credential_ref_id, revoked)

    def save_google_app_credentials(self, credentials: GoogleAppCredentials) -> None:
        self._write(self._app_credentials_path(PlatformCode.GOOGLE), _to_json(credentials))

    def get_google_app_credentials(self) -> GoogleAppCredentials | None:
        raw = self._read_app_credentials(PlatformCode.GOOGLE)
        return None if raw is None else _google_app_credentials_from_json(raw)

    def save_meta_app_credentials(self, credentials: MetaAppCredentials) -> None:
        self._write(self._app_credentials_path(PlatformCode.META), _to_json(credentials))

    def get_meta_app_credentials(self) -> MetaAppCredentials | None:
        raw = self._read_app_credentials(PlatformCode.META)
        return None if raw is None else _meta_app_credentials_from_json(raw)

    def delete_app_credentials(self, platform: PlatformCode) -> None:
        self._app_credentials_path(platform).unlink(missing_ok=True)

    def _read_app_credentials(self, platform: PlatformCode) -> dict[str, Any] | None:
        path = self._app_credentials_path(platform)
        try:
            raw = path.read_bytes()
        except FileNotFoundError:
            return None
        decrypted: dict[str, Any] = json.loads(self._decrypt(raw, path))
        return decrypted

    def _app_credentials_path(self, platform: PlatformCode) -> Path:
        return self._app_credentials_dir / f"{platform.value}.enc"

    def _credential_path(self, credential_ref_id: CredentialRefId) -> Path:
        return self._credentials_dir / f"{credential_ref_id}.enc"

    def _write(self, path: Path, plaintext: bytes) -> None:
        blob = self._encrypt(plaintext, path)
        fd, temporary = tempfile.mkstemp(dir=path.parent, prefix=".credential-")
        try:
            with os.fdopen(fd, "wb") as stream:
                stream.write(blob)
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(temporary, path)
        finally:
            Path(temporary).unlink(missing_ok=True)

    def _aad(self, path: Path) -> bytes:
        # Stable across a backup/restore to a different directory. Binding to
        # category + ID prevents moving ciphertext between records/categories.
        return _FORMAT + f"{path.parent.name}/{path.name}".encode()

    def _encrypt(self, plaintext: bytes, path: Path) -> bytes:
        nonce = os.urandom(_NONCE_LENGTH_BYTES)
        return _FORMAT + nonce + self._aesgcm.encrypt(nonce, plaintext, self._aad(path))

    def _decrypt(self, blob: bytes, path: Path) -> bytes:
        if not blob.startswith(_FORMAT):
            # No fallback to unbound v1 ciphertext. An operator must explicitly
            # reconnect/re-enter credentials before deploying this clean format.
            raise CredentialStoreKeyError("formato de credencial no admitido; reconecta la cuenta")
        encrypted = blob[len(_FORMAT) :]
        if len(encrypted) <= _NONCE_LENGTH_BYTES:
            raise CredentialStoreKeyError("registro cifrado demasiado corto")
        nonce, ciphertext = encrypted[:_NONCE_LENGTH_BYTES], encrypted[_NONCE_LENGTH_BYTES:]
        return self._aesgcm.decrypt(nonce, ciphertext, self._aad(path))

"""`EncryptedCredentialStore` (threat-model.md C-24): cifrado en reposo,
un solo uso de la sesion OAuth, y fail-closed sin clave maestra."""

from __future__ import annotations

import base64
import shutil
import uuid
from concurrent.futures import ProcessPoolExecutor
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from cryptography.exceptions import InvalidTag
from cryptography.hazmat.primitives.ciphers.aead import AESGCM

from safent_ads.accounts.domain.refs import CredentialRefId
from safent_ads.broker.application.ports import (
    ConnectSessionRecord,
    CredentialRecord,
    GoogleAppCredentials,
    MetaAppCredentials,
)
from safent_ads.broker.infrastructure.credential_store import (
    CredentialStoreKeyError,
    EncryptedCredentialStore,
)
from safent_ads.shared.ids import PlatformCode

_NOW = datetime(2026, 9, 9, tzinfo=UTC)
_VALID_KEY_B64 = base64.b64encode(b"0" * 32).decode()


def _store(tmp_path: Path, key_b64: str = _VALID_KEY_B64) -> EncryptedCredentialStore:
    return EncryptedCredentialStore(tmp_path / "credentials", key_b64)


def _session_record(**overrides: object) -> ConnectSessionRecord:
    defaults: dict[str, object] = {
        "provider": PlatformCode.GOOGLE,
        "business_id": str(uuid.uuid4()),
        "redirect_uri": "https://ads.example.ts.net/api/v1/platform-accounts/connect/google/callback",
        "pkce_verifier": "a-code-verifier",
        "created_at": _NOW,
        "expires_at": _NOW + timedelta(minutes=10),
    }
    defaults.update(overrides)
    return ConnectSessionRecord(**defaults)  # type: ignore[arg-type]


def _credential_record(**overrides: object) -> CredentialRecord:
    defaults: dict[str, object] = {
        "platform": PlatformCode.GOOGLE,
        "token": "1//refresh-token-secret",
        "token_type": "refresh_token",
        "scopes": ("https://www.googleapis.com/auth/adwords",),
        "obtained_at": _NOW,
        "expires_at": None,
    }
    defaults.update(overrides)
    return CredentialRecord(**defaults)  # type: ignore[arg-type]


def test_missing_key_fails_closed(tmp_path: Path) -> None:
    with pytest.raises(CredentialStoreKeyError):
        EncryptedCredentialStore(tmp_path / "credentials", "")


def test_malformed_key_fails_closed(tmp_path: Path) -> None:
    with pytest.raises(CredentialStoreKeyError):
        EncryptedCredentialStore(tmp_path / "credentials", base64.b64encode(b"short").decode())


def test_store_directories_are_owner_only(tmp_path: Path) -> None:
    store_dir = tmp_path / "credentials"
    _store(tmp_path)

    assert (store_dir / "sessions").stat().st_mode & 0o777 == 0o700
    assert (store_dir / "credentials").stat().st_mode & 0o777 == 0o700


def test_connect_session_round_trips(tmp_path: Path) -> None:
    store = _store(tmp_path)
    record = _session_record()

    store.save_connect_session("state-hash-1", record)
    popped = store.pop_connect_session("state-hash-1")

    assert popped == record


def test_connect_session_is_single_use(tmp_path: Path) -> None:
    store = _store(tmp_path)
    store.save_connect_session("state-hash-1", _session_record())

    first = store.pop_connect_session("state-hash-1")
    second = store.pop_connect_session("state-hash-1")

    assert first is not None
    assert second is None


def test_unknown_session_returns_none(tmp_path: Path) -> None:
    store = _store(tmp_path)
    assert store.pop_connect_session("never-existed") is None


def test_credential_round_trips(tmp_path: Path) -> None:
    store = _store(tmp_path)
    credential_ref_id = CredentialRefId(uuid.uuid4())
    record = _credential_record()

    store.save_credential(credential_ref_id, record)

    assert store.get_credential(credential_ref_id) == record


def test_unknown_credential_returns_none(tmp_path: Path) -> None:
    store = _store(tmp_path)
    assert store.get_credential(CredentialRefId(uuid.uuid4())) is None


def test_revoke_blanks_the_token_but_keeps_metadata(tmp_path: Path) -> None:
    store = _store(tmp_path)
    credential_ref_id = CredentialRefId(uuid.uuid4())
    store.save_credential(credential_ref_id, _credential_record())

    store.revoke_credential(credential_ref_id, at=_NOW)

    revoked = store.get_credential(credential_ref_id)
    assert revoked is not None
    assert revoked.token == ""
    assert revoked.revoked_at == _NOW
    assert revoked.token_type == "refresh_token"  # noqa: S105 - nombre de tipo, no secreto


def test_revoke_unknown_credential_is_a_noop(tmp_path: Path) -> None:
    store = _store(tmp_path)
    store.revoke_credential(CredentialRefId(uuid.uuid4()), at=_NOW)  # no debe lanzar


def test_ciphertext_on_disk_never_contains_the_plaintext_token(tmp_path: Path) -> None:
    store_dir = tmp_path / "credentials"
    store = EncryptedCredentialStore(store_dir, _VALID_KEY_B64)
    credential_ref_id = CredentialRefId(uuid.uuid4())
    store.save_credential(credential_ref_id, _credential_record(token="super-secret-refresh"))

    on_disk = (store_dir / "credentials" / f"{credential_ref_id}.enc").read_bytes()

    assert b"super-secret-refresh" not in on_disk


def test_wrong_key_cannot_decrypt(tmp_path: Path) -> None:
    store = _store(tmp_path)
    credential_ref_id = CredentialRefId(uuid.uuid4())
    store.save_credential(credential_ref_id, _credential_record())

    other_key = base64.b64encode(b"1" * 32).decode()
    other_store = _store(tmp_path, key_b64=other_key)

    with pytest.raises(Exception):  # noqa: B017,PT011 - InvalidTag de `cryptography`, sin subclase propia
        other_store.get_credential(credential_ref_id)


def test_ciphertext_cannot_be_transplanted_between_credential_ids(tmp_path: Path) -> None:
    store = _store(tmp_path)
    first, second = CredentialRefId(uuid.uuid4()), CredentialRefId(uuid.uuid4())
    store.save_credential(first, _credential_record(token="first-token"))
    store.save_credential(second, _credential_record(token="second-token"))
    folder = tmp_path / "credentials" / "credentials"
    (folder / f"{second}.enc").write_bytes((folder / f"{first}.enc").read_bytes())
    with pytest.raises(InvalidTag):
        store.get_credential(second)


def test_ciphertext_cannot_be_transplanted_between_oauth_sessions(tmp_path: Path) -> None:
    store = _store(tmp_path)
    store.save_connect_session("first", _session_record(business_id="business-a"))
    store.save_connect_session("second", _session_record(business_id="business-b"))
    folder = tmp_path / "credentials" / "sessions"
    (folder / "second.enc").write_bytes((folder / "first.enc").read_bytes())
    with pytest.raises(InvalidTag):
        store.pop_connect_session("second")
    assert store.pop_connect_session("second") is None


def test_ciphertext_cannot_be_transplanted_between_account_bindings(tmp_path: Path) -> None:
    store = _store(tmp_path)
    ref = CredentialRefId(uuid.uuid4())
    store.save_credential(ref, _credential_record())
    for account in ("123", "456"):
        store.bind_account_credential(PlatformCode.GOOGLE, account, ref)
    files = list((tmp_path / "credentials" / "accounts").glob("*.enc"))
    original = files[1].read_bytes()
    files[1].write_bytes(files[0].read_bytes())
    assert files[1].read_bytes() != original
    with pytest.raises(InvalidTag):
        for account in ("123", "456"):
            store.account_credential_ref(PlatformCode.GOOGLE, account)


def test_old_unbound_ciphertext_is_rejected_without_fallback_or_overwrite(tmp_path: Path) -> None:
    store = _store(tmp_path)
    ref = CredentialRefId(uuid.uuid4())
    nonce = b"n" * 12
    legacy = nonce + AESGCM(b"0" * 32).encrypt(nonce, b"{}", None)
    path = tmp_path / "credentials" / "credentials" / f"{ref}.enc"
    path.write_bytes(legacy)
    with pytest.raises(CredentialStoreKeyError, match="formato de credencial"):
        store.get_credential(ref)
    assert path.read_bytes() == legacy


def test_ciphertext_is_bound_to_record_category(tmp_path: Path) -> None:
    store = _store(tmp_path)
    store.save_google_app_credentials(_google_app_credentials())
    root = tmp_path / "credentials"
    (root / "sessions" / "google.enc").write_bytes(
        (root / "app_credentials" / "google.enc").read_bytes()
    )
    with pytest.raises(InvalidTag):
        store.pop_connect_session("google")


def test_backup_restores_in_another_directory_with_the_same_key(tmp_path: Path) -> None:
    store = _store(tmp_path)
    ref = CredentialRefId(uuid.uuid4())
    record = _credential_record()
    store.save_credential(ref, record)
    shutil.copytree(tmp_path / "credentials", tmp_path / "restored")
    restored = EncryptedCredentialStore(tmp_path / "restored", _VALID_KEY_B64)
    assert restored.get_credential(ref) == record


def _consume_from_another_process(store_dir: str) -> bool:
    store = EncryptedCredentialStore(Path(store_dir), _VALID_KEY_B64)
    return store.pop_connect_session("concurrent") is not None


def test_only_one_process_can_consume_an_oauth_session(tmp_path: Path) -> None:
    store = _store(tmp_path)
    store.save_connect_session("concurrent", _session_record())
    with ProcessPoolExecutor(max_workers=4) as workers:
        winners = list(
            workers.map(_consume_from_another_process, [str(tmp_path / "credentials")] * 16)
        )
    assert sum(winners) == 1
    assert not list((tmp_path / "credentials" / "sessions").glob(".consumed-*"))


@pytest.mark.parametrize("identifier", ["../outside", "/absolute", "x/y", "x\\y", "", "x" * 129])
def test_session_identifier_cannot_escape_its_directory(tmp_path: Path, identifier: str) -> None:
    store = _store(tmp_path)
    with pytest.raises(CredentialStoreKeyError):
        store.save_connect_session(identifier, _session_record())
    with pytest.raises(CredentialStoreKeyError):
        store.pop_connect_session(identifier)


def test_secret_records_have_redacted_repr() -> None:
    assert "1//refresh-token-secret" not in repr(_credential_record())
    assert "a-code-verifier" not in repr(_session_record())
    assert "client-secret-value" not in repr(_google_app_credentials())
    assert "meta-app-secret-value" not in repr(_meta_app_credentials())


# --- lane: app-credentials-ui ---
def _google_app_credentials(**overrides: object) -> GoogleAppCredentials:
    defaults: dict[str, object] = {
        "client_id": "abc123.apps.googleusercontent.com",
        "client_secret": "client-secret-value",
        "login_customer_id": "1234567890",
        "updated_at": _NOW,
    }
    defaults.update(overrides)
    return GoogleAppCredentials(**defaults)  # type: ignore[arg-type]


def _meta_app_credentials(**overrides: object) -> MetaAppCredentials:
    defaults: dict[str, object] = {
        "app_id": "9876543210",
        "app_secret": "meta-app-secret-value",
        "updated_at": _NOW,
    }
    defaults.update(overrides)
    return MetaAppCredentials(**defaults)  # type: ignore[arg-type]


def test_google_app_credentials_round_trip(tmp_path: Path) -> None:
    store = _store(tmp_path)
    record = _google_app_credentials()

    store.save_google_app_credentials(record)

    assert store.get_google_app_credentials() == record


def test_meta_app_credentials_round_trip(tmp_path: Path) -> None:
    store = _store(tmp_path)
    record = _meta_app_credentials()

    store.save_meta_app_credentials(record)

    assert store.get_meta_app_credentials() == record


def test_unknown_app_credentials_return_none(tmp_path: Path) -> None:
    store = _store(tmp_path)

    assert store.get_google_app_credentials() is None
    assert store.get_meta_app_credentials() is None


def test_google_app_credentials_without_login_customer_id_round_trips(tmp_path: Path) -> None:
    store = _store(tmp_path)
    record = _google_app_credentials(login_customer_id=None)

    store.save_google_app_credentials(record)

    fetched = store.get_google_app_credentials()
    assert fetched is not None
    assert fetched.login_customer_id is None


def test_saving_google_app_credentials_replaces_the_previous_value(tmp_path: Path) -> None:
    store = _store(tmp_path)
    store.save_google_app_credentials(_google_app_credentials(client_secret="first"))

    store.save_google_app_credentials(_google_app_credentials(client_secret="second"))

    fetched = store.get_google_app_credentials()
    assert fetched is not None
    assert fetched.client_secret == "second"  # noqa: S105 - valor de prueba, no secreto real


def test_delete_app_credentials_removes_the_record(tmp_path: Path) -> None:
    store = _store(tmp_path)
    store.save_google_app_credentials(_google_app_credentials())
    store.save_meta_app_credentials(_meta_app_credentials())

    store.delete_app_credentials(PlatformCode.GOOGLE)

    assert store.get_google_app_credentials() is None
    assert store.get_meta_app_credentials() is not None


def test_delete_unknown_app_credentials_is_a_noop(tmp_path: Path) -> None:
    store = _store(tmp_path)
    store.delete_app_credentials(PlatformCode.META)  # no debe lanzar


def test_app_credentials_ciphertext_never_contains_the_plaintext_secret(tmp_path: Path) -> None:
    store_dir = tmp_path / "credentials"
    store = EncryptedCredentialStore(store_dir, _VALID_KEY_B64)
    store.save_google_app_credentials(_google_app_credentials(client_secret="super-secret-oauth"))

    on_disk = (store_dir / "app_credentials" / "google.enc").read_bytes()

    assert b"super-secret-oauth" not in on_disk


def test_app_credentials_directory_is_owner_only(tmp_path: Path) -> None:
    store_dir = tmp_path / "credentials"
    _store(tmp_path)

    assert (store_dir / "app_credentials").stat().st_mode & 0o777 == 0o700


# --- end lane: app-credentials-ui ---

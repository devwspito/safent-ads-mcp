"""Install-bound encrypted leases; only replay metadata ever reaches disk."""

from __future__ import annotations

import base64
import json
import os
import re
import sqlite3
import threading
import time
import uuid
from collections.abc import Callable
from pathlib import Path
from typing import Any

from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric.x25519 import X25519PublicKey
from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from cryptography.hazmat.primitives.kdf.hkdf import HKDF

from safent_ads.broker.application.managed_oauth_connect import ManagedOAuthConfig
from safent_ads.broker.infrastructure.composio_channel_key import derive_channel_private_key
from safent_ads.iam.infrastructure.ed25519_assertion_verifier import decode_ed25519_public_key

_AAD = b"safent-ads-broker:composio-config:v1"
_INFO = b"safent-composio-lease-v1"
_MAX_ENVELOPE = 32768
_NONCE_BYTES = 12
_LEASE_TTL = 90
_MAX_ID = 200
_MAX_API_KEY = 8192
_CLAIMS = {"v", "iss", "aud", "purpose", "sub", "iat", "exp", "jti", "revision", "config"}


class ComposioLeaseError(ValueError):
    """Static, non-secret error crossing the broker boundary."""


def _decode(value: str) -> bytes:
    if not isinstance(value, str) or not re.fullmatch(r"[A-Za-z0-9_-]+", value):
        raise ValueError("invalid_encoding")
    return base64.b64decode(value + "=" * (-len(value) % 4), altchars=b"-_", validate=True)


def _json(raw: bytes) -> dict[str, Any]:
    def unique(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
        result: dict[str, Any] = {}
        for key, value in pairs:
            if key in result:
                raise ValueError("duplicate_field")
            result[key] = value
        return result

    value = json.loads(raw, object_pairs_hook=unique)
    if not isinstance(value, dict):
        raise ValueError("invalid_object")
    return value


class ComposioLeaseStore:
    def __init__(
        self,
        *,
        master_key_b64: str,
        issuer_public_key: str,
        metadata_path: Path,
        clock: Callable[[], float] = time.time,
        monotonic: Callable[[], float] = time.monotonic,
    ) -> None:
        self._private_key = derive_channel_private_key(master_key_b64)
        self._issuer = decode_ed25519_public_key(issuer_public_key)
        self._path, self._clock, self._monotonic = metadata_path, clock, monotonic
        self._lock = threading.RLock()
        self._config = ManagedOAuthConfig(api_key="")
        self._expires = self._deadline = 0.0
        self._entity_id = ""
        metadata_path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        with sqlite3.connect(metadata_path) as conn:
            conn.execute(
                "CREATE TABLE IF NOT EXISTS lease_head (id INTEGER PRIMARY KEY CHECK(id=1), "
                "revision INTEGER NOT NULL, subject TEXT NOT NULL)"
            )
            conn.execute(
                "CREATE TABLE IF NOT EXISTS lease_seen "
                "(jti TEXT PRIMARY KEY, expires INTEGER NOT NULL)"
            )
        os.chmod(metadata_path, 0o600)

    def channel(self) -> dict[str, Any]:
        public = self._private_key.public_key().public_bytes(
            serialization.Encoding.Raw, serialization.PublicFormat.Raw
        )
        return {"version": 1, "public_key": base64.b64encode(public).decode("ascii")}

    def current_config(self) -> ManagedOAuthConfig:
        with self._lock:
            if self._clock() >= self._expires or self._monotonic() >= self._deadline:
                self._config = ManagedOAuthConfig(api_key="")
            return self._config

    def accept(self, envelope: str) -> None:
        try:
            self._accept(envelope)
        except Exception:
            # Never expose decrypted JSON, cryptographic errors, or key material.
            raise ComposioLeaseError("composio_lease_invalid") from None

    def _accept(self, envelope: str) -> None:
        if not isinstance(envelope, str) or not 1 <= len(envelope) <= _MAX_ENVELOPE:
            raise ValueError("invalid_envelope")
        outer = _json(_decode(envelope))
        if (
            set(outer) != {"v", "ephemeral_public_key", "nonce", "ciphertext"}
            or type(outer["v"]) is not int
            or outer["v"] != 1
        ):
            raise ValueError("invalid_envelope")
        nonce = _decode(outer["nonce"])
        if len(nonce) != _NONCE_BYTES:
            raise ValueError("invalid_nonce")
        peer = X25519PublicKey.from_public_bytes(_decode(outer["ephemeral_public_key"]))
        key = HKDF(algorithm=hashes.SHA256(), length=32, salt=None, info=_INFO).derive(
            self._private_key.exchange(peer)
        )
        token = AESGCM(key).decrypt(nonce, _decode(outer["ciphertext"]), _AAD).decode("ascii")
        payload, signature = token.split(".")
        payload_bytes = _decode(payload)
        self._issuer.verify(_decode(signature), payload_bytes)
        claims = _json(payload_bytes)
        if (
            set(claims) != _CLAIMS
            or type(claims["v"]) is not int
            or claims["v"] != 1
            or claims["iss"] != "safent-runtime"
            or claims["aud"] != "safent-ads-broker"
            or claims["purpose"] != "composio-config"
        ):
            raise ValueError("invalid_claims")
        now = self._clock()
        if (
            any(type(claims[key]) is not int for key in ("iat", "exp", "revision"))
            or not 0 < claims["exp"] - claims["iat"] <= _LEASE_TTL
            or claims["iat"] > now + 5
            or claims["exp"] <= now
            or not 0 <= claims["revision"] < 2**63
        ):
            raise ValueError("invalid_time_or_revision")
        subject = claims["sub"]
        if not isinstance(subject, str) or not 1 <= len(subject) <= _MAX_ID:
            raise ValueError("invalid_subject")
        jti = str(uuid.UUID(claims["jti"]))
        config = claims["config"]
        if (
            not isinstance(config, dict)
            or set(config) != {"enabled", "api_key", "entity_id", "auth_config_ids"}
            or type(config["enabled"]) is not bool
        ):
            raise ValueError("invalid_config")
        api_key, entity, mapping = config["api_key"], config["entity_id"], config["auth_config_ids"]
        if (
            not isinstance(api_key, str)
            or len(api_key) > _MAX_API_KEY
            or any(c in api_key for c in "\r\n")
            or not isinstance(entity, str)
            or len(entity) > _MAX_ID
            or not isinstance(mapping, dict)
            or set(mapping) - {"googleads", "metaads"}
        ):
            raise ValueError("invalid_config")
        if any(
            not isinstance(value, str) or re.fullmatch(r"ac_[A-Za-z0-9_-]{1,128}", value) is None
            for value in mapping.values()
        ):
            raise ValueError("invalid_auth_config")
        if config["enabled"] and (not api_key or not entity):
            raise ValueError("invalid_enabled_config")
        resolved = ManagedOAuthConfig(
            api_key=api_key if config["enabled"] else "",
            google_auth_config_id=mapping.get("googleads") if config["enabled"] else None,
            meta_auth_config_id=mapping.get("metaads") if config["enabled"] else None,
        )
        with self._lock, sqlite3.connect(self._path) as conn:
            conn.execute("BEGIN IMMEDIATE")
            previous = conn.execute(
                "SELECT revision, subject FROM lease_head WHERE id=1"
            ).fetchone()
            if previous is not None and (
                claims["revision"] <= previous[0] or subject != previous[1]
            ):
                raise ValueError("rollback_or_subject_mismatch")
            conn.execute("DELETE FROM lease_seen WHERE expires <= ?", (int(now),))
            conn.execute("INSERT INTO lease_seen VALUES (?, ?)", (jti, claims["exp"]))
            conn.execute(
                "INSERT INTO lease_head VALUES (1, ?, ?) ON CONFLICT(id) DO UPDATE SET "
                "revision=excluded.revision, subject=excluded.subject",
                (claims["revision"], subject),
            )
            conn.commit()
            self._config, self._entity_id = resolved, entity
            self._expires = claims["exp"]
            self._deadline = self._monotonic() + max(0, claims["exp"] - self._clock())

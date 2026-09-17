"""Firma de aprobaciones Ed25519 (threat-model.md C-3): "El backend firma sus
propias aprobaciones (HMAC compartido heredado de oposads) -> el componente
expuesto a injection autoriza" es exactamente el fallo que este modulo evita.
La clave privada solo existe en el proceso `ads-api` (vive en
`ApiSettings.approval_signing_key`, plan.md §3.1); `BrokerSettings` no
declara ese campo, asi que no hay ninguna trayectoria de codigo en el
broker capaz de cargarla. El broker solo posee la clave publica
(`BrokerSettings.approval_public_key`) para verificar.

`canonical_json` fija la serializacion (claves ordenadas, sin espacios,
UTF-8) para que firmante y verificador calculen la misma firma sobre el
mismo payload sin importar el orden de insercion de las claves en cada
proceso."""

from __future__ import annotations

import base64
import json
from collections.abc import Mapping, Sequence

from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives.asymmetric.ed25519 import (
    Ed25519PrivateKey,
    Ed25519PublicKey,
)
from cryptography.hazmat.primitives.serialization import Encoding, PublicFormat

from safent_ads.shared.errors import InfrastructureError

JsonValue = str | int | float | bool | None | Sequence["JsonValue"] | Mapping[str, "JsonValue"]

_SEED_LENGTH_BYTES = 32


class SigningKeyMissingError(InfrastructureError):
    """No hay material de clave privada valido: firmar es imposible."""


class VerificationKeyMissingError(InfrastructureError):
    """No hay material de clave publica valido: verificar es imposible."""


def canonical_json(payload: Mapping[str, JsonValue]) -> bytes:
    """Serializacion reproducible entre procesos: claves ordenadas, sin
    espacios, UTF-8. Firmante y verificador DEBEN usar exactamente esta
    funcion o la firma nunca cuadrara."""
    return json.dumps(
        payload, sort_keys=True, separators=(",", ":"), ensure_ascii=False
    ).encode("utf-8")


def _decode_seed(seed_b64: str) -> bytes:
    if not seed_b64:
        raise SigningKeyMissingError("ADS_APPROVAL_SIGNING_KEY vacio o ausente")
    try:
        seed = base64.b64decode(seed_b64, validate=True)
    except (ValueError, TypeError) as exc:
        raise SigningKeyMissingError("ADS_APPROVAL_SIGNING_KEY no es base64 valido") from exc
    if len(seed) != _SEED_LENGTH_BYTES:
        raise SigningKeyMissingError(
            f"ADS_APPROVAL_SIGNING_KEY debe decodificar a {_SEED_LENGTH_BYTES} bytes"
        )
    return seed


def _decode_public_key(public_key_b64: str) -> bytes:
    if not public_key_b64:
        raise VerificationKeyMissingError("ADS_APPROVAL_PUBLIC_KEY vacio o ausente")
    try:
        raw = base64.b64decode(public_key_b64, validate=True)
    except (ValueError, TypeError) as exc:
        raise VerificationKeyMissingError("ADS_APPROVAL_PUBLIC_KEY no es base64 valido") from exc
    if len(raw) != _SEED_LENGTH_BYTES:
        raise VerificationKeyMissingError(
            f"ADS_APPROVAL_PUBLIC_KEY debe decodificar a {_SEED_LENGTH_BYTES} bytes"
        )
    return raw


class ApprovalSigner:
    """Solo construible con material de clave privada real (ver
    `from_seed_b64`); no existe una via "vacia" que finja poder firmar."""

    def __init__(self, private_key: Ed25519PrivateKey) -> None:
        self._private_key = private_key

    @classmethod
    def from_seed_b64(cls, seed_b64: str) -> ApprovalSigner:
        seed = _decode_seed(seed_b64)
        return cls(Ed25519PrivateKey.from_private_bytes(seed))

    def sign(self, payload: Mapping[str, JsonValue]) -> bytes:
        return self._private_key.sign(canonical_json(payload))

    def public_key_b64(self) -> str:
        raw = self._private_key.public_key().public_bytes(Encoding.Raw, PublicFormat.Raw)
        return base64.b64encode(raw).decode("ascii")


class ApprovalVerifier:
    """El broker construye esto desde `BrokerSettings.approval_public_key`
    unicamente; nunca desde una semilla privada."""

    def __init__(self, public_key: Ed25519PublicKey) -> None:
        self._public_key = public_key

    @classmethod
    def from_public_key_b64(cls, public_key_b64: str) -> ApprovalVerifier:
        raw = _decode_public_key(public_key_b64)
        return cls(Ed25519PublicKey.from_public_bytes(raw))

    def verify(self, payload: Mapping[str, JsonValue], signature: bytes) -> bool:
        try:
            self._public_key.verify(signature, canonical_json(payload))
        except InvalidSignature:
            return False
        return True

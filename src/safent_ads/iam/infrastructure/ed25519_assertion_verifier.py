"""Adaptador Ed25519 de `AssertionVerifier` (026, contracts/sso.md §3/§4).
Formato `<b64url(payload)>.<b64url(sig)>`, sin envoltorio JWT: un único
algoritmo, sin `alg` negociable (sso.md §3). El payload firmado es JSON
compacto de claves ordenadas -- la firma cubre los bytes exactos que llegan,
nunca un `dict` reserializado por Python (que podría reordenar u omitir
espacios de forma distinta)."""

from __future__ import annotations

import base64
import json

from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey

from safent_ads.iam.application.errors import AssertionInvalidError, AssertionMalformedError
from safent_ads.iam.application.ports import AssertionPayload
from safent_ads.shared.errors import InfrastructureError

_REQUIRED_FIELDS = frozenset(
    {"v", "iss", "aud", "slug", "sub", "jti", "iat", "exp", "purpose", "surface"}
)


class SsoPublicKeyError(InfrastructureError):
    """`ADS_SSO_PUBLIC_KEY` ausente, no es base64 o no es una clave Ed25519
    válida de 32 bytes."""


def decode_ed25519_public_key(public_key_b64: str) -> Ed25519PublicKey:
    if not public_key_b64:
        raise SsoPublicKeyError("ADS_SSO_PUBLIC_KEY vacio o ausente")
    try:
        raw = base64.urlsafe_b64decode(_with_padding(public_key_b64))
        return Ed25519PublicKey.from_public_bytes(raw)
    except (ValueError, TypeError) as exc:
        raise SsoPublicKeyError("ADS_SSO_PUBLIC_KEY no es una clave Ed25519 valida") from exc


def _with_padding(value: str) -> str:
    return value + "=" * (-len(value) % 4)


class Ed25519AssertionVerifier:
    def __init__(self, public_key_b64: str) -> None:
        self._public_key = decode_ed25519_public_key(public_key_b64)

    def verify(self, assertion: str) -> AssertionPayload:
        payload_bytes, signature = self._split(assertion)
        self._verify_signature(payload_bytes, signature)
        return self._parse_payload(payload_bytes)

    def _split(self, assertion: str) -> tuple[bytes, bytes]:
        try:
            payload_b64, signature_b64 = assertion.split(".", 1)
            payload_bytes = base64.urlsafe_b64decode(_with_padding(payload_b64))
            signature = base64.urlsafe_b64decode(_with_padding(signature_b64))
        except (ValueError, TypeError) as exc:
            raise AssertionMalformedError("aserción ilegible") from exc
        return payload_bytes, signature

    def _verify_signature(self, payload_bytes: bytes, signature: bytes) -> None:
        try:
            self._public_key.verify(signature, payload_bytes)
        except InvalidSignature as exc:
            raise AssertionInvalidError("firma Ed25519 invalida") from exc

    def _parse_payload(self, payload_bytes: bytes) -> AssertionPayload:
        try:
            raw = json.loads(payload_bytes.decode("utf-8"))
        except (ValueError, UnicodeDecodeError) as exc:
            raise AssertionMalformedError("payload de la aserción no es JSON valido") from exc
        if not isinstance(raw, dict) or not _REQUIRED_FIELDS.issubset(raw.keys()):
            raise AssertionMalformedError("payload de la aserción incompleto")
        try:
            return AssertionPayload(
                v=int(raw["v"]),
                iss=str(raw["iss"]),
                aud=str(raw["aud"]),
                slug=str(raw["slug"]),
                sub=str(raw["sub"]),
                jti=str(raw["jti"]),
                iat=int(raw["iat"]),
                exp=int(raw["exp"]),
                purpose=str(raw["purpose"]),
                surface=str(raw["surface"]),
            )
        except (TypeError, ValueError) as exc:
            raise AssertionMalformedError("payload de la aserción mal tipado") from exc

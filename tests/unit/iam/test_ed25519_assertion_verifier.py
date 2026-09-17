"""`Ed25519AssertionVerifier` (026, tasks.md T002, contracts/sso.md §3):
firma/verificación reales con `cryptography` -- las pruebas de política
(literales, ventana temporal, `jti`) viven en
`test_exchange_owner_assertion.py`, estas solo cubren el sobre criptográfico."""

from __future__ import annotations

import base64
import json

import pytest
from cryptography.hazmat.primitives.asymmetric.ed25519 import (
    Ed25519PrivateKey,
    Ed25519PublicKey,
)

from safent_ads.iam.application.errors import AssertionInvalidError, AssertionMalformedError
from safent_ads.iam.infrastructure.ed25519_assertion_verifier import (
    Ed25519AssertionVerifier,
    SsoPublicKeyError,
    decode_ed25519_public_key,
)

_PAYLOAD = {
    "v": 1,
    "iss": "safent-runtime",
    "aud": "safent-ads",
    "slug": "safent-ads",
    "sub": "sub-owner-1",
    "jti": "11111111-1111-1111-1111-111111111111",
    "iat": 1_000_000,
    "exp": 1_000_060,
    "purpose": "cockpit_session",
    "surface": "safent_cockpit",
}


def _b64url(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).decode("ascii").rstrip("=")


def _keypair() -> tuple[Ed25519PrivateKey, str]:
    private_key = Ed25519PrivateKey.generate()
    public_bytes = private_key.public_key().public_bytes_raw()
    return private_key, _b64url(public_bytes)


def _sign_assertion(private_key: Ed25519PrivateKey, payload: dict[str, object]) -> str:
    payload_bytes = json.dumps(payload, separators=(",", ":"), sort_keys=True).encode("utf-8")
    signature = private_key.sign(payload_bytes)
    return f"{_b64url(payload_bytes)}.{_b64url(signature)}"


def test_decode_rejects_empty_key() -> None:
    with pytest.raises(SsoPublicKeyError):
        decode_ed25519_public_key("")


def test_decode_rejects_wrong_length_key() -> None:
    with pytest.raises(SsoPublicKeyError):
        decode_ed25519_public_key(_b64url(b"too-short"))


def test_decode_accepts_a_real_ed25519_public_key() -> None:
    _, public_key_b64 = _keypair()

    decoded = decode_ed25519_public_key(public_key_b64)

    assert isinstance(decoded, Ed25519PublicKey)


def test_verify_accepts_a_correctly_signed_assertion() -> None:
    private_key, public_key_b64 = _keypair()
    verifier = Ed25519AssertionVerifier(public_key_b64)
    assertion = _sign_assertion(private_key, _PAYLOAD)

    payload = verifier.verify(assertion)

    assert payload.jti == _PAYLOAD["jti"]
    assert payload.sub == _PAYLOAD["sub"]
    assert payload.exp == _PAYLOAD["exp"]


def test_verify_rejects_a_forged_signature() -> None:
    _, public_key_b64 = _keypair()
    forger_key, _ = _keypair()
    verifier = Ed25519AssertionVerifier(public_key_b64)
    forged = _sign_assertion(forger_key, _PAYLOAD)

    with pytest.raises(AssertionInvalidError):
        verifier.verify(forged)


def test_verify_rejects_a_tampered_payload_with_a_valid_signature_for_the_original() -> None:
    private_key, public_key_b64 = _keypair()
    verifier = Ed25519AssertionVerifier(public_key_b64)
    signed = _sign_assertion(private_key, _PAYLOAD)
    payload_b64, signature_b64 = signed.split(".", 1)
    tampered_payload = json.dumps({**_PAYLOAD, "sub": "attacker"}, separators=(",", ":"))
    tampered = f"{_b64url(tampered_payload.encode())}.{signature_b64}"
    del payload_b64

    with pytest.raises(AssertionInvalidError):
        verifier.verify(tampered)


def test_verify_rejects_malformed_base64() -> None:
    _, public_key_b64 = _keypair()
    verifier = Ed25519AssertionVerifier(public_key_b64)

    with pytest.raises(AssertionMalformedError):
        verifier.verify("not-base64-at-all.###")


def test_verify_rejects_a_payload_missing_required_fields() -> None:
    private_key, public_key_b64 = _keypair()
    verifier = Ed25519AssertionVerifier(public_key_b64)
    incomplete = dict(_PAYLOAD)
    del incomplete["jti"]
    assertion = _sign_assertion(private_key, incomplete)

    with pytest.raises(AssertionMalformedError):
        verifier.verify(assertion)


def test_verify_rejects_a_single_part_assertion() -> None:
    _, public_key_b64 = _keypair()
    verifier = Ed25519AssertionVerifier(public_key_b64)

    with pytest.raises(AssertionMalformedError):
        verifier.verify("no-dot-separator")

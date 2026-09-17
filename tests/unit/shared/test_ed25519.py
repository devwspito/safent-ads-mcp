"""`shared/crypto/ed25519.py` (threat-model.md C-3): la clave privada solo
existe donde `ApiSettings` la declara; `BrokerSettings` no tiene ese campo,
asi que no hay codigo capaz de cargarla en el broker."""

from __future__ import annotations

import base64

import pytest

from safent_ads.composition.settings import BrokerSettings
from safent_ads.shared.crypto.ed25519 import (
    ApprovalSigner,
    ApprovalVerifier,
    SigningKeyMissingError,
    VerificationKeyMissingError,
    canonical_json,
)


def test_api_cannot_sign_without_key() -> None:
    with pytest.raises(SigningKeyMissingError):
        ApprovalSigner.from_seed_b64("")


def test_api_cannot_sign_with_malformed_key() -> None:
    with pytest.raises(SigningKeyMissingError):
        ApprovalSigner.from_seed_b64(base64.b64encode(b"too-short").decode())


def test_broker_role_cannot_load_private_key() -> None:
    broker_settings = BrokerSettings(
        broker_socket_path="/tmp/safent-ads-test/broker.sock",
        approval_public_key="anything",
        allowed_uids=[10001],
        hard_caps_file="/etc/ads-broker/caps.yaml",
        credential_master_key="anything",
        credential_store_dir="/tmp/safent-ads-test/credentials",
    )

    assert not hasattr(broker_settings, "approval_signing_key")


def test_broker_cannot_verify_without_public_key() -> None:
    with pytest.raises(VerificationKeyMissingError):
        ApprovalVerifier.from_public_key_b64("")


def test_signature_roundtrip_canonical() -> None:
    seed_b64 = base64.b64encode(b"0" * 32).decode()
    signer = ApprovalSigner.from_seed_b64(seed_b64)
    verifier = ApprovalVerifier.from_public_key_b64(signer.public_key_b64())

    payload = {"proposal_id": "abc", "diff_hash": "deadbeef", "expires_at": "2026-09-09T00:00:00Z"}
    signature = signer.sign(payload)

    assert verifier.verify(payload, signature) is True


def test_signature_verification_is_independent_of_key_insertion_order() -> None:
    seed_b64 = base64.b64encode(b"1" * 32).decode()
    signer = ApprovalSigner.from_seed_b64(seed_b64)
    verifier = ApprovalVerifier.from_public_key_b64(signer.public_key_b64())

    ordered = {"a": 1, "b": 2, "c": 3}
    reordered = {"c": 3, "a": 1, "b": 2}
    signature = signer.sign(ordered)

    assert verifier.verify(reordered, signature) is True


def test_signature_rejects_tampered_payload() -> None:
    seed_b64 = base64.b64encode(b"2" * 32).decode()
    signer = ApprovalSigner.from_seed_b64(seed_b64)
    verifier = ApprovalVerifier.from_public_key_b64(signer.public_key_b64())

    signature = signer.sign({"amount": 100})

    assert verifier.verify({"amount": 999}, signature) is False


def test_signature_from_wrong_key_pair_is_rejected() -> None:
    signer_a = ApprovalSigner.from_seed_b64(base64.b64encode(b"3" * 32).decode())
    signer_b = ApprovalSigner.from_seed_b64(base64.b64encode(b"4" * 32).decode())
    verifier_for_a = ApprovalVerifier.from_public_key_b64(signer_a.public_key_b64())

    signature_from_b = signer_b.sign({"x": 1})

    assert verifier_for_a.verify({"x": 1}, signature_from_b) is False


def test_canonical_json_sorts_keys_and_strips_whitespace() -> None:
    assert canonical_json({"b": 1, "a": 2}) == b'{"a":2,"b":1}'

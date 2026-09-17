"""`generate_state`/`hash_state`/PKCE (RFC 7636)."""

from __future__ import annotations

import hashlib

from safent_ads.broker.application.oauth_state import (
    generate_pkce_verifier,
    generate_state,
    hash_state,
    pkce_challenge,
)


def test_generate_state_is_unpredictable() -> None:
    assert generate_state() != generate_state()


def test_hash_state_is_deterministic_sha256() -> None:
    state = "some-state-value"
    assert hash_state(state) == hashlib.sha256(state.encode("utf-8")).hexdigest()


def test_hash_state_differs_for_different_states() -> None:
    assert hash_state("a") != hash_state("b")


def test_pkce_verifier_length_within_rfc7636() -> None:
    verifier = generate_pkce_verifier()
    assert 43 <= len(verifier) <= 128


def test_pkce_challenge_is_deterministic_and_url_safe() -> None:
    verifier = generate_pkce_verifier()
    challenge_a = pkce_challenge(verifier)
    challenge_b = pkce_challenge(verifier)
    assert challenge_a == challenge_b
    assert "=" not in challenge_a
    assert "+" not in challenge_a
    assert "/" not in challenge_a


def test_pkce_challenge_differs_for_different_verifiers() -> None:
    assert pkce_challenge(generate_pkce_verifier()) != pkce_challenge(generate_pkce_verifier())

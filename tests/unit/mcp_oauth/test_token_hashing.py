"""`Sha256TokenHasher`/`SecretsOpaqueTokenFactory` (tasks.md T007,
threat-model.md C-44): hash estable, hex de 64, el token en claro nunca se
guarda, dos tokens seguidos difieren."""

from __future__ import annotations

from safent_ads.mcp_oauth.infrastructure.secrets_token_factory import SecretsOpaqueTokenFactory
from safent_ads.mcp_oauth.infrastructure.sha256_token_hasher import Sha256TokenHasher


def test_hash_is_stable_for_the_same_token() -> None:
    hasher = Sha256TokenHasher()

    assert hasher.hash("abc").value == hasher.hash("abc").value


def test_hash_is_64_char_hex() -> None:
    digest = Sha256TokenHasher().hash("abc").value

    assert len(digest) == 64
    assert all(char in "0123456789abcdef" for char in digest)


def test_hash_never_contains_the_raw_token() -> None:
    raw_token = "super-secret-raw-token"  # noqa: S105 - fixture, no secreto real

    digest = Sha256TokenHasher().hash(raw_token).value

    assert raw_token not in digest


def test_two_different_tokens_hash_differently() -> None:
    hasher = Sha256TokenHasher()

    assert hasher.hash("token-a").value != hasher.hash("token-b").value


def test_two_consecutive_generated_tokens_differ() -> None:
    factory = SecretsOpaqueTokenFactory()

    assert factory.new_token() != factory.new_token()


def test_generated_token_has_at_least_256_bits_of_entropy() -> None:
    # base64url sin relleno de 32 bytes -> 43 caracteres.
    token = SecretsOpaqueTokenFactory().new_token()

    assert len(token) >= 43

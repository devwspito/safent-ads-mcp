"""`AesGcmTotpCipher` (revision de seguridad de la conexion Cloudflare,
2026-09-15): la clave `ADS_TOTP_ENC_KEY` es UNICA para el secreto TOTP, el
token de Cloudflare y el codigo de emparejamiento de Telegram -- sin AAD
por proposito, un blob cifrado para una tabla decifra igual de valido en
otra (quien tenga acceso de escritura a la BD puede copiar
`cloudflare_connection.api_token_encrypted` a
`telegram_pairing.pairing_code_encrypted` y `GET /telegram/pairing`
devolveria el token de Cloudflare en claro)."""

from __future__ import annotations

import base64

import pytest
from cryptography.exceptions import InvalidTag
from cryptography.hazmat.primitives.ciphers.aead import AESGCM

from safent_ads.iam.infrastructure.aesgcm_totp_cipher import (
    PURPOSE_CLOUDFLARE_API_TOKEN,
    PURPOSE_TELEGRAM_PAIRING,
    PURPOSE_TOTP_SECRET,
    AesGcmTotpCipher,
)

_VALID_32_BYTE_KEY_B64 = "MDAwMDAwMDAwMDAwMDAwMDAwMDAwMDAwMDAwMDAwMDA="


def _cipher() -> AesGcmTotpCipher:
    return AesGcmTotpCipher(_VALID_32_BYTE_KEY_B64)


def test_round_trips_with_the_matching_purpose() -> None:
    cipher = _cipher()
    blob = cipher.encrypt("sk-cloudflare-token", purpose=PURPOSE_CLOUDFLARE_API_TOKEN)

    assert cipher.decrypt(blob, purpose=PURPOSE_CLOUDFLARE_API_TOKEN) == "sk-cloudflare-token"


def test_a_cloudflare_token_blob_never_decrypts_as_a_telegram_pairing_code() -> None:
    """El ataque concreto del hallazgo: copiar `api_token_encrypted` a
    `pairing_code_encrypted` ya no produce un descifrado valido."""
    cipher = _cipher()
    cloudflare_blob = cipher.encrypt(
        "sk-cloudflare-super-secret", purpose=PURPOSE_CLOUDFLARE_API_TOKEN
    )

    with pytest.raises(InvalidTag):
        cipher.decrypt(cloudflare_blob, purpose=PURPOSE_TELEGRAM_PAIRING)


def test_a_telegram_pairing_blob_never_decrypts_as_a_totp_secret() -> None:
    cipher = _cipher()
    pairing_blob = cipher.encrypt("ABC123", purpose=PURPOSE_TELEGRAM_PAIRING)

    with pytest.raises(InvalidTag):
        cipher.decrypt(pairing_blob, purpose=PURPOSE_TOTP_SECRET)


def test_legacy_blobs_encrypted_without_a_purpose_still_decrypt() -> None:
    """Migracion: filas ya en la BD antes de este cambio se cifraron con
    AAD `None`. `decrypt(..., purpose=...)` debe seguir leyendolas -- la
    fila se re-cifra con `purpose` en la proxima escritura de quien la
    posee, no aqui."""
    nonce = b"\x00" * 12
    key_bytes = base64.b64decode(_VALID_32_BYTE_KEY_B64)
    legacy_blob = nonce + AESGCM(key_bytes).encrypt(nonce, b"legacy-secret", None)

    assert _cipher().decrypt(legacy_blob, purpose=PURPOSE_CLOUDFLARE_API_TOKEN) == "legacy-secret"

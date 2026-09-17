"""Adaptador AES-256-GCM de `TotpCipher` (data-model.md: "TOTP...cifrado en
reposo"). Formato en disco: `nonce(12B) || ciphertext_con_tag`. La clave
viene de `ApiSettings.totp_enc_key` (32 bytes en base64); se valida al
construir para fallar alto en el arranque, no en el primer login
(threat-model.md C-24).

`purpose` (AAD, revision de seguridad de la conexion Cloudflare,
2026-09-15): la clave es UNICA para las tres cosas que cifra (secreto
TOTP, token de Cloudflare, codigo de emparejamiento de Telegram --
`connection_store.py`/`telegram_pairing_sql.py`); sin separacion por
dominio, quien tenga acceso de escritura a la BD puede copiar un blob
cifrado de una tabla a otra y `decrypt` lo acepta igual (GCM valida
autenticidad e integridad del ciphertext, no de "a que fila pertenece").
Igual que `broker/infrastructure/credential_store.py::_aad` (ata la
categoria al ciphertext), cada llamador pasa su propia constante
`PURPOSE_*` como AAD -- un blob cifrado para un proposito nunca decifra
para otro.

Migracion: filas ya existentes se cifraron con AAD `None` (antes de este
cambio). `decrypt` intenta primero con el `purpose` dado y, solo si eso
falla, reintenta una vez con AAD `None` (dato heredado) -- nunca al reves,
GCM es todo o nada, un blob ya migrado jamas decifra por casualidad sin
`purpose`. No hay migracion de datos en caliente: la fila se re-cifra con
`purpose` sola en la proxima escritura de quien la posee (`save`/
`start_pairing`), que ya pasa por `encrypt(..., purpose=...)`."""

from __future__ import annotations

import base64
import os
from typing import Final

from cryptography.exceptions import InvalidTag
from cryptography.hazmat.primitives.ciphers.aead import AESGCM

from safent_ads.shared.errors import InfrastructureError

_NONCE_LENGTH_BYTES = 12
_KEY_LENGTH_BYTES = 32

# Constantes de proposito (AAD): una por tabla/columna que reusa esta
# clave. Nunca reordenar/renombrar sin migrar los datos -- son el "a que
# fila pertenece este blob" que GCM autentica.
PURPOSE_TOTP_SECRET: Final = b"totp-secret"
PURPOSE_CLOUDFLARE_API_TOKEN: Final = b"cloudflare-api-token"
PURPOSE_TELEGRAM_PAIRING: Final = b"telegram-pairing"


class TotpEncryptionKeyError(InfrastructureError):
    """`ADS_TOTP_ENC_KEY` ausente o no decodifica a 32 bytes."""


class AesGcmTotpCipher:
    def __init__(self, key_b64: str) -> None:
        if not key_b64:
            raise TotpEncryptionKeyError("ADS_TOTP_ENC_KEY vacio o ausente")
        try:
            key_bytes = base64.b64decode(key_b64, validate=True)
        except (ValueError, TypeError) as exc:
            raise TotpEncryptionKeyError("ADS_TOTP_ENC_KEY no es base64 valido") from exc
        if len(key_bytes) != _KEY_LENGTH_BYTES:
            raise TotpEncryptionKeyError(
                f"ADS_TOTP_ENC_KEY debe decodificar a {_KEY_LENGTH_BYTES} bytes"
            )
        self._aesgcm = AESGCM(key_bytes)

    def encrypt(self, secret: str, *, purpose: bytes) -> bytes:
        nonce = os.urandom(_NONCE_LENGTH_BYTES)
        return nonce + self._aesgcm.encrypt(nonce, secret.encode("utf-8"), purpose)

    def decrypt(self, blob: bytes, *, purpose: bytes) -> str:
        if len(blob) <= _NONCE_LENGTH_BYTES:
            raise TotpEncryptionKeyError("blob cifrado demasiado corto")
        nonce, ciphertext = blob[:_NONCE_LENGTH_BYTES], blob[_NONCE_LENGTH_BYTES:]
        try:
            return self._aesgcm.decrypt(nonce, ciphertext, purpose).decode("utf-8")
        except InvalidTag:
            return self._aesgcm.decrypt(nonce, ciphertext, None).decode("utf-8")

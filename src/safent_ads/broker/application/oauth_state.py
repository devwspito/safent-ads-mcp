"""Generacion de `state` (anti-CSRF) y del par PKCE (RFC 7636) para el
flujo OAuth "Conectar" (contracts/rest-api.md §Conexiones: "El broker
genera state (32 bytes) y el code_verifier PKCE"). `hash_state` hashea el
`state` antes de usarlo como clave del almacen, igual que
`iam.application.verify_totp` hashea el token de sesion — defensa en
profundidad: un listado del directorio del almacen no revela `state`
validos en claro."""

from __future__ import annotations

import base64
import hashlib
import secrets

_STATE_BYTES = 32
_PKCE_VERIFIER_BYTES = 64  # decodifica a un verifier de 86 caracteres, dentro de RFC 7636 (43-128)


def generate_state() -> str:
    return secrets.token_urlsafe(_STATE_BYTES)


def hash_state(state: str) -> str:
    return hashlib.sha256(state.encode("utf-8")).hexdigest()


def generate_pkce_verifier() -> str:
    return secrets.token_urlsafe(_PKCE_VERIFIER_BYTES)


def pkce_challenge(verifier: str) -> str:
    """S256 (RFC 7636 §4.2): `BASE64URL-ENCODE(SHA256(verifier))` sin
    relleno."""
    digest = hashlib.sha256(verifier.encode("utf-8")).digest()
    return base64.urlsafe_b64encode(digest).decode("ascii").rstrip("=")

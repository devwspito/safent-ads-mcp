"""Adaptador `pyotp` de `TotpCodeVerifier`: RFC 6238 con tolerancia de un
paso (30s) para desfase de reloj entre servidor y app autenticadora."""

from __future__ import annotations

from datetime import datetime

import pyotp
from pyotp.utils import strings_equal

_VALID_WINDOW_STEPS = 1
_ISSUER = "Safent Ads"


class PyotpTotpVerifier:
    def generate_secret(self) -> str:
        return pyotp.random_base32()

    def verify_code(self, secret: str, code: str) -> bool:
        if not secret or not code.strip():
            return False
        return bool(pyotp.TOTP(secret).verify(code.strip(), valid_window=_VALID_WINDOW_STEPS))

    def matched_time_step(self, secret: str, code: str, *, at: datetime) -> int | None:
        """Como `verify_code`, pero devuelve el contador RFC 6238 (paso de
        30s) que de verdad hizo match dentro de la misma tolerancia de un
        paso -- T075/F2-F3, TOTP re-auth defecto 1: hace falta el contador
        exacto para poder quemarlo (`(owner_id, time_step)` UNIQUE) y que un
        codigo ya usado nunca vuelva a verificar, ni para la misma accion ni
        para otra."""
        if not secret or not code.strip():
            return None
        totp = pyotp.TOTP(secret)
        candidate = code.strip()
        base_step = totp.timecode(at)
        for offset in range(-_VALID_WINDOW_STEPS, _VALID_WINDOW_STEPS + 1):
            if strings_equal(candidate, totp.at(at, offset)):
                return base_step + offset
        return None

    def provisioning_uri(self, secret: str, email: str) -> str:
        return pyotp.TOTP(secret).provisioning_uri(name=email, issuer_name=_ISSUER)

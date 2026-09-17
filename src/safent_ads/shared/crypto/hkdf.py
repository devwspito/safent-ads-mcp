"""Derivacion de subclaves via HKDF-SHA256 (RFC 5869, threat-model.md C-24):
un unico secreto maestro de alta entropia (p.ej. `ADS_SESSION_SECRET`, 32
bytes de CSPRNG que ya genera el instalador) puede alimentar varios usos sin
pedirle al propietario un secreto nuevo por cada uno -- cada uso fija su
propio `info` para que las subclaves derivadas no colisionen entre si aunque
compartan el mismo maestro. Esto evita el patron "campo `SecretStr` con
`default=` publico" (B-1, security-review-f4.md): ningun secreto derivado
queda nunca versionado en el arbol porque no existe como valor propio, se
recalcula siempre desde el maestro."""

from __future__ import annotations

from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.kdf.hkdf import HKDF

_DEFAULT_DERIVED_KEY_LENGTH_BYTES = 32


def derive_key(
    master_secret: bytes, info: bytes, *, length: int = _DEFAULT_DERIVED_KEY_LENGTH_BYTES
) -> bytes:
    """HKDF-SHA256 sin `salt` (RFC 5869 lo permite: el maestro ya es
    aleatorio de alta entropia, un salt fijo no anadiria nada). Determinista
    -- mismo `master_secret`+`info`+`length` da siempre la misma clave, para
    que reiniciar el proceso no invalide firmas ya emitidas con la clave
    derivada anterior."""
    hkdf = HKDF(algorithm=hashes.SHA256(), length=length, salt=None, info=info)
    return hkdf.derive(master_secret)

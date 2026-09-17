"""Genera el par Ed25519 de aprobacion (quickstart.md §1, threat-model.md
C-3): `ads-api` se queda con la semilla privada (`ADS_APPROVAL_SIGNING_KEY`),
`ads-broker` solo recibe la clave publica (`ADS_APPROVAL_PUBLIC_KEY` en
`secrets/broker.env`). Nunca escribe ambas claves al mismo fichero ni al
mismo destino: quien despliega copia cada valor a su sitio.

Uso:
    python -m safent_ads.tools.gen_keys
    python -m safent_ads.tools.gen_keys --out-dir ./secrets
"""

from __future__ import annotations

import argparse
import base64
import os
import stat
from pathlib import Path

from cryptography.exceptions import InvalidKey
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from cryptography.hazmat.primitives.serialization import Encoding, PublicFormat

_PRIVATE_KEY_FILENAME = "approval_signing_key.b64"
_PUBLIC_KEY_FILENAME = "approval_public_key.b64"
_OWNER_READ_WRITE_ONLY = stat.S_IRUSR | stat.S_IWUSR  # 0600


class ApprovalKeyMaterialError(ValueError):
    """La semilla privada que se leyo de `secrets/api.env` no es material
    Ed25519 valido: falla alto en vez de generar un par nuevo en silencio."""


def generate_key_pair() -> tuple[str, str]:
    """`(semilla privada, clave publica)`, ambas en base64."""
    private_key = Ed25519PrivateKey.generate()
    seed = private_key.private_bytes_raw()
    return _encode(seed), public_key_for(_encode(seed))


def public_key_for(seed_b64: str) -> str:
    """Clave publica que corresponde a una semilla privada ya existente.

    `tools/first_run.py` la necesita para no REGENERAR el par cuando
    `secrets/api.env` ya tiene la privada pero `secrets/broker.env` se
    quedo sin la publica (proceso muerto entre los pasos 4 y 5 del
    contrato): regenerar dejaria al broker confiando en una publica que ya
    no verifica ninguna aprobacion."""
    try:
        seed = base64.b64decode(seed_b64, validate=True)
        private_key = Ed25519PrivateKey.from_private_bytes(seed)
    except (ValueError, InvalidKey) as exc:
        raise ApprovalKeyMaterialError(
            "ADS_APPROVAL_SIGNING_KEY no es una semilla Ed25519 valida en base64"
        ) from exc
    return _encode(private_key.public_key().public_bytes(Encoding.Raw, PublicFormat.Raw))


def _encode(raw: bytes) -> str:
    return base64.b64encode(raw).decode("ascii")


def _write_key_file(path: Path, content: str) -> None:
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, _OWNER_READ_WRITE_ONLY)
    try:
        with os.fdopen(fd, "w") as handle:
            handle.write(content + "\n")
    finally:
        os.chmod(path, _OWNER_READ_WRITE_ONLY)


def _parse_args(argv: list[str] | None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--out-dir",
        type=Path,
        default=None,
        help=f"Si se indica, escribe {_PRIVATE_KEY_FILENAME} y {_PUBLIC_KEY_FILENAME} "
        "ahi con permisos 0600.",
    )
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> None:
    args = _parse_args(argv)
    seed_b64, public_b64 = generate_key_pair()

    print(f"ADS_APPROVAL_SIGNING_KEY={seed_b64}")
    print(f"ADS_APPROVAL_PUBLIC_KEY={public_b64}")

    if args.out_dir is not None:
        args.out_dir.mkdir(parents=True, exist_ok=True)
        _write_key_file(args.out_dir / _PRIVATE_KEY_FILENAME, seed_b64)
        _write_key_file(args.out_dir / _PUBLIC_KEY_FILENAME, public_b64)
        print(f"Escritas en {args.out_dir} (0600).")


if __name__ == "__main__":
    main()

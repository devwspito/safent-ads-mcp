"""Seed/update a local Community owner password without MFA or changing legacy keys.

008-mcp-ads-estandar T017: `--password` en argv quedaba en el historial del
shell y en `ps` (NFR-001). La unica via es `--password-stdin`, leyendo la
contrasena de la entrada estandar."""

from __future__ import annotations

import argparse
import asyncio
import sys
import uuid

import asyncpg  # type: ignore[import-untyped]

from safent_ads.composition.settings import ApiSettings
from safent_ads.iam.infrastructure.argon2_password_hasher import Argon2PasswordHasher

_UPSERT_OWNER_SQL = """
    INSERT INTO owners (id, email, password_hash)
    VALUES ($1, $2, $3)
    ON CONFLICT (email) DO UPDATE
    SET password_hash = EXCLUDED.password_hash
"""


class MissingPasswordError(RuntimeError):
    """stdin no traia ninguna contrasena, o era un TTY (nunca tecleada visible)."""


def _parse_args(argv: list[str] | None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--email", required=True)
    parser.add_argument(
        "--password-stdin",
        action="store_true",
        required=True,
        help="Lee la contrasena de stdin. Unica via: nunca por argumento posicional ni --password.",
    )
    return parser.parse_args(argv)


def _read_password_from_stdin() -> str:
    if sys.stdin.isatty():
        raise MissingPasswordError(
            "--password-stdin exige que la contrasena llegue por stdin "
            "(evita un TTY: quedaria tecleada y visible)."
        )
    password = sys.stdin.read()
    password = password[:-1] if password.endswith("\n") else password
    if not password:
        raise MissingPasswordError("stdin no traia ninguna contrasena.")
    return password


def to_asyncpg_dsn(sqlalchemy_dsn: str) -> str:
    """`postgresql+asyncpg://` es el dialecto de SQLAlchemy; `asyncpg.connect`
    solo entiende `postgresql://`."""
    return sqlalchemy_dsn.replace("postgresql+asyncpg://", "postgresql://")


async def upsert_owner_password(*, dsn: str, email: str, password: str) -> None:
    """Alta o cambio de contrasena del dueno contra un DSN ya resuelto.

    Separado de `main()` para que `tools/first_run.py` (008 T016) COMPONGA
    esta herramienta en vez de reimplementar el hash Argon2 y el upsert: el
    primer arranque resuelve el DSN desde el `.env` que acaba de escribir,
    no desde `ApiSettings()` (que lee el fichero del directorio actual)."""
    password_hash = Argon2PasswordHasher().hash(password)
    connection = await asyncpg.connect(dsn)
    try:
        await connection.execute(_UPSERT_OWNER_SQL, uuid.uuid4(), email, password_hash)
    finally:
        await connection.close()


async def _seed(email: str, password: str) -> None:
    settings = ApiSettings()  # type: ignore[call-arg]
    dsn = to_asyncpg_dsn(settings.database_url.get_secret_value())
    await upsert_owner_password(dsn=dsn, email=email, password=password)


def main(argv: list[str] | None = None) -> None:
    args = _parse_args(argv)
    try:
        password = _read_password_from_stdin()
    except MissingPasswordError as exc:
        raise SystemExit(f"ERROR: {exc}") from exc
    asyncio.run(_seed(args.email, password))
    print(f"OWNER_EMAIL={args.email}")


if __name__ == "__main__":
    main()

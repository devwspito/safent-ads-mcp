"""Adaptador SQL de `OwnerRepository` sobre `owners` (0001_bootstrap.py).
`save` solo actualiza: crear la fila del propietario es responsabilidad de
un script de arranque (data-model.md: modelo de propietario unico), no de
un caso de uso de login."""

from __future__ import annotations

import uuid
from typing import Any

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from safent_ads.iam.domain.email import Email
from safent_ads.iam.domain.owner import Owner

_GET_BY_EMAIL_SQL = text(
    "SELECT id, email, password_hash, totp_secret_encrypted, totp_confirmed_at, created_at "
    "FROM owners WHERE email = :email"
)
_GET_BY_ID_SQL = text(
    "SELECT id, email, password_hash, totp_secret_encrypted, totp_confirmed_at, created_at "
    "FROM owners WHERE id = :id"
)
_UPDATE_SQL = text(
    """
    UPDATE owners
    SET password_hash = :password_hash,
        totp_secret_encrypted = :totp_secret_encrypted,
        totp_confirmed_at = :totp_confirmed_at
    WHERE id = :id
    """
)


def _row_to_owner(row: Any) -> Owner:  # noqa: ANN401 - fila heterogenea de SQLAlchemy
    return Owner(
        owner_id=row.id,
        email=Email(row.email),
        password_hash=row.password_hash,
        totp_secret_encrypted=bytes(row.totp_secret_encrypted)
        if row.totp_secret_encrypted is not None
        else None,
        totp_confirmed_at=row.totp_confirmed_at,
        created_at=row.created_at,
    )


class SqlOwnerRepository:
    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def get_by_email(self, email: Email) -> Owner | None:
        result = await self._session.execute(_GET_BY_EMAIL_SQL, {"email": str(email)})
        row = result.one_or_none()
        return None if row is None else _row_to_owner(row)

    async def get_by_id(self, owner_id: uuid.UUID) -> Owner | None:
        result = await self._session.execute(_GET_BY_ID_SQL, {"id": str(owner_id)})
        row = result.one_or_none()
        return None if row is None else _row_to_owner(row)

    async def save(self, owner: Owner) -> None:
        await self._session.execute(
            _UPDATE_SQL,
            {
                "id": str(owner.id),
                "password_hash": owner.password_hash,
                "totp_secret_encrypted": owner.totp_secret_encrypted,
                "totp_confirmed_at": owner.totp_confirmed_at,
            },
        )

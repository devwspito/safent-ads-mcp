"""Doble en memoria de `OwnerRepository`, para probar `Login`/`VerifyTotp`/
`EnrollTotp` sin Postgres."""

from __future__ import annotations

import uuid

from safent_ads.iam.domain.email import Email
from safent_ads.iam.domain.owner import Owner


class InMemoryOwnerRepository:
    def __init__(self, owners: list[Owner] | None = None) -> None:
        self._owners: dict[uuid.UUID, Owner] = {owner.id: owner for owner in owners or []}

    async def get_by_email(self, email: Email) -> Owner | None:
        for owner in self._owners.values():
            if owner.email == email:
                return owner
        return None

    async def get_by_id(self, owner_id: uuid.UUID) -> Owner | None:
        return self._owners.get(owner_id)

    async def save(self, owner: Owner) -> None:
        self._owners[owner.id] = owner

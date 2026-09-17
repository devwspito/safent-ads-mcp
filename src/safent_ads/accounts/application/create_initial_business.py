"""Human-only first local business; not Enterprise membership or Ads authority."""

from __future__ import annotations

import uuid
from typing import Protocol

from safent_ads.accounts.domain.business import Business
from safent_ads.shared.ids import BusinessId


class BusinessAlreadyConfiguredError(Exception):
    """A committed initial business exists; read /auth/me before any new intent."""


class OwnerConfigurationAmbiguousError(Exception):
    """The local single-owner model cannot authorize this caller unambiguously."""


class InitialBusinessRepository(Protocol):
    async def create_for_sole_owner(self, owner_id: uuid.UUID, business: Business) -> None: ...


class CreateInitialBusiness:
    def __init__(self, repository: InitialBusinessRepository) -> None:
        self._repository = repository

    async def execute(
        self, *, owner_id: uuid.UUID, name: str, timezone: str, reference_currency: str
    ) -> Business:
        business_id = uuid.uuid4()
        business = Business(
            business_id=BusinessId(business_id),
            name=name,
            slug=f"business-{business_id.hex}",
            timezone=timezone,
            reference_currency=reference_currency,
        )
        await self._repository.create_for_sole_owner(owner_id, business)
        return business

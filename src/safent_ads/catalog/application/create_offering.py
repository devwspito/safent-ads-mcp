"""Add catalog metadata idempotently, never approve or create advertisements."""

from dataclasses import dataclass
from typing import Protocol

from safent_ads.catalog.domain.offering import OfferingDetails
from safent_ads.shared.ids import BusinessId


class OfferingCodeConflictError(Exception):
    """An existing code identifies different facts; must not overwrite."""


class OfferingBusinessNotFoundError(Exception):
    """The requested catalog business is unavailable."""


@dataclass(frozen=True, slots=True)
class CreatedOffering:
    offering_id: str
    code: str
    title: str
    price_amount: str | None
    price_currency: str | None
    created: bool


class OfferingCreationPort(Protocol):
    async def create_or_get(
        self, business_id: BusinessId, details: OfferingDetails
    ) -> CreatedOffering: ...


class CreateOffering:
    def __init__(self, repository: OfferingCreationPort) -> None:
        self._repository = repository

    async def execute(self, business_id: BusinessId, details: OfferingDetails) -> CreatedOffering:
        return await self._repository.create_or_get(business_id, details)

"""Strict HTTP catalog input; domain validation is also used by MCP."""

from pydantic import BaseModel, ConfigDict, model_validator

from safent_ads.catalog.domain.offering import OfferingDetails


class CreateOfferingBody(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True, frozen=True)
    code: str
    title: str
    price_amount: str | None = None
    price_currency: str | None = None

    def details(self) -> OfferingDetails:
        return OfferingDetails(self.code, self.title, self.price_amount, self.price_currency)

    @model_validator(mode="after")
    def validate_details(self) -> "CreateOfferingBody":
        self.details()
        return self

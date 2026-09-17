"""One transaction, scoped business+code uniqueness, no update on replay."""

from decimal import Decimal

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from safent_ads.catalog.application.create_offering import (
    CreatedOffering,
    OfferingBusinessNotFoundError,
    OfferingCodeConflictError,
)
from safent_ads.catalog.domain.offering import OfferingDetails
from safent_ads.shared.ids import BusinessId

_INSERT = text("""
    INSERT INTO offerings (business_id, code, title, price_amount, price_currency)
    VALUES (:business, :code, :title, :amount, :currency)
    ON CONFLICT (business_id, code) DO NOTHING
    RETURNING id, title, price_amount, price_currency, is_active
""")
_SELECT = text("""
    SELECT id, title, price_amount, price_currency, is_active FROM offerings
     WHERE business_id=:business AND code=:code
""")


class RequestScopedOfferingCreation:
    def __init__(self, sessions: async_sessionmaker[AsyncSession]) -> None:
        self._sessions = sessions

    async def create_or_get(
        self, business_id: BusinessId, details: OfferingDetails
    ) -> CreatedOffering:
        amount = Decimal(details.price_amount) if details.price_amount is not None else None
        params = {
            "business": business_id.value,
            "code": details.code,
            "title": details.title,
            "amount": amount,
            "currency": details.price_currency,
        }
        async with self._sessions.begin() as session:
            exists = (
                await session.execute(
                    text("SELECT id FROM businesses WHERE id=:business FOR KEY SHARE"),
                    params,
                )
            ).scalar_one_or_none()
            if exists is None:
                raise OfferingBusinessNotFoundError
            row = (await session.execute(_INSERT, params)).mappings().one_or_none()
            created = row is not None
            if row is None:
                row = (await session.execute(_SELECT, params)).mappings().one()
                if (
                    row["title"] != details.title
                    or row["price_amount"] != amount
                    or row["price_currency"] != details.price_currency
                    or not row["is_active"]
                ):
                    raise OfferingCodeConflictError
            return CreatedOffering(
                offering_id=str(row["id"]),
                code=details.code,
                title=row["title"],
                price_amount=format(row["price_amount"], "f")
                if row["price_amount"] is not None
                else None,
                price_currency=row["price_currency"],
                created=created,
            )

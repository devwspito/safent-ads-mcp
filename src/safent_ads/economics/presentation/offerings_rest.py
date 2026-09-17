"""Borde HTTP de ofertas + economia unitaria (contracts/rest-api.md
§Economia unitaria, T131/T132): `GET /offerings`, `PUT /offerings/{id}/
economics`. Cuerpo `dict[str, Any]` parseado a mano (mismo patron que
`settings/presentation/rest.py::_parse_update_command`): sin logica de
negocio aqui, solo forma -> comando -> caso de uso -> JSON."""

from __future__ import annotations

import uuid
from decimal import Decimal, InvalidOperation
from typing import Annotated, Any

from fastapi import APIRouter, Body, Depends
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from safent_ads.catalog.application.create_offering import (
    CreateOffering,
    OfferingBusinessNotFoundError,
    OfferingCodeConflictError,
)
from safent_ads.catalog.infrastructure.sql_offering_creation import RequestScopedOfferingCreation
from safent_ads.catalog.presentation.offering_input import CreateOfferingBody
from safent_ads.economics.application.errors import (
    InvalidOfferingEconomicsError,
    OfferingNotFoundError,
)
from safent_ads.economics.application.list_offerings import ListOfferingsWithEconomics
from safent_ads.economics.application.ports import OfferingEconomicsInput
from safent_ads.economics.application.upsert_offering_economics import UpsertOfferingEconomics
from safent_ads.economics.domain.identifiers import ProductId
from safent_ads.economics.domain.payment_plan import PaymentPlan
from safent_ads.economics.infrastructure.offering_economics_sql import (
    SqlOfferingEconomicsRepository,
)
from safent_ads.economics.presentation.serialization import to_json_dict
from safent_ads.iam.presentation.errors import ApiError
from safent_ads.panel.presentation.deps import require_business_access
from safent_ads.shared.ids import BusinessId

__all__ = ["build_offerings_router"]

BusinessIdDep = Annotated[str, Depends(require_business_access)]
_NOT_FOUND = ApiError(status_code=404, code="NOT_FOUND", message="No encontrado.")


def build_offerings_router(session_factory: async_sessionmaker[AsyncSession]) -> APIRouter:
    router = APIRouter(prefix="/api/v1/offerings", tags=["offerings"])

    @router.post("")
    async def create_offering(
        business_id: BusinessIdDep, body: CreateOfferingBody
    ) -> dict[str, Any]:
        # Global CSRF middleware and the current-owner dependency apply unchanged.
        try:
            result = await CreateOffering(RequestScopedOfferingCreation(session_factory)).execute(
                BusinessId(uuid.UUID(business_id)),
                body.details(),
            )
        except OfferingBusinessNotFoundError as exc:
            raise _NOT_FOUND from exc
        except OfferingCodeConflictError as exc:
            raise ApiError(
                status_code=409,
                code="OFFERING_CODE_CONFLICT",
                message=(
                    "Ese código ya identifica otra oferta. Revisa el catálogo o elige otro código."
                ),
            ) from exc
        return to_json_dict(result)

    @router.get("")
    async def list_offerings(business_id: BusinessIdDep) -> dict[str, Any]:
        async with session_factory() as session:
            offerings = await ListOfferingsWithEconomics(
                offerings=SqlOfferingEconomicsRepository(session)
            ).execute(business_id=BusinessId(uuid.UUID(business_id)))
        return {"items": [to_json_dict(offering) for offering in offerings]}

    @router.put("/{offering_id}/economics")
    async def put_offering_economics(
        offering_id: uuid.UUID,
        business_id: BusinessIdDep,
        body: Annotated[dict[str, Any], Body(...)],
    ) -> dict[str, Any]:
        economics_input = _parse_economics_input(body)
        async with session_factory() as session:
            try:
                economics = await UpsertOfferingEconomics(
                    offerings=SqlOfferingEconomicsRepository(session)
                ).execute(
                    business_id=BusinessId(uuid.UUID(business_id)),
                    offering_id=ProductId(offering_id),
                    economics=economics_input,
                )
            except OfferingNotFoundError as exc:
                raise _NOT_FOUND from exc
            except InvalidOfferingEconomicsError as exc:
                raise ApiError(status_code=422, code="VALIDATION_ERROR", message=str(exc)) from exc
            await session.commit()
        return to_json_dict(economics)

    return router


def _parse_economics_input(body: dict[str, Any]) -> OfferingEconomicsInput:
    try:
        return OfferingEconomicsInput(
            vat_rate_pct=_require_decimal(body, "vat_rate_pct"),
            delivery_cost_minor=_require_int(body, "delivery_cost_minor"),
            sales_cost_minor=_require_int(body, "sales_cost_minor"),
            refund_rate_pct=_optional_decimal(body, "refund_rate_pct"),
            payment_plan=PaymentPlan(body.get("payment_plan", PaymentPlan.NONE.value)),
            currency=str(body.get("currency", "EUR")),
        )
    except (KeyError, TypeError, ValueError, InvalidOperation) as exc:
        raise ApiError(status_code=422, code="VALIDATION_ERROR", message=str(exc)) from exc


def _require_decimal(body: dict[str, Any], field: str) -> Decimal:
    if field not in body or body[field] is None:
        raise KeyError(field)
    return Decimal(str(body[field]))


def _optional_decimal(body: dict[str, Any], field: str) -> Decimal | None:
    value = body.get(field)
    return None if value is None else Decimal(str(value))


def _require_int(body: dict[str, Any], field: str) -> int:
    if field not in body or body[field] is None:
        raise KeyError(field)
    return int(body[field])

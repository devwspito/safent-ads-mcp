"""Borde REST del puente CRM->anuncios (contracts/crm-link.md §2, spec 027
T016): `POST /crm/customers`, `POST /crm/revenue-events`, `PUT /crm/bridge-
health`, `POST /crm/customers/forget`. Autenticado por `X-Bridge-Token`
(mismo patron que `X-Webhook-Token` de `/conversions/webhook`, tabla propia
`crm_bridge_tokens`): el runtime no tiene cookie de `ads-api`.

`POST /crm/bridge-token` (emision, sesion del propietario + confirmación humana)
no estaba en la lista explicita de T016 -- se anade porque sin un endpoint
de emision la superficie de ingesta es inerte (Assumption documentada,
mismo patron que `POST /conversions/webhook-token`).

Sin logica de negocio: valida forma y tamano, resuelve la identidad del
llamante por token, llama el caso de uso, mapea a JSON (mismo criterio que
`crm/presentation/rest.py`)."""

from __future__ import annotations

import hashlib
import uuid
from datetime import datetime
from typing import Annotated, Any

from fastapi import APIRouter, Depends, Request
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from safent_ads.crm.application.forget_customer import ForgetCustomer
from safent_ads.crm.application.ingest_customers import (
    CustomerBatchTooLargeError,
    CustomerIngestItem,
    IngestCustomers,
)
from safent_ads.crm.application.ingest_revenue_events import (
    IngestRevenueEvents,
    RevenueEventBatchTooLargeError,
    RevenueEventIngestItem,
)
from safent_ads.crm.application.record_bridge_health import RecordBridgeHealth
from safent_ads.crm.application.webhook_token import AuthenticateWebhookToken, GenerateWebhookToken
from safent_ads.crm.domain.errors import InvalidIdentityDigestError
from safent_ads.crm.infrastructure.sql_crm_bridge_health_repository import (
    SqlCrmBridgeHealthRepository,
)
from safent_ads.crm.infrastructure.sql_crm_bridge_token_repository import (
    SqlCrmBridgeTokenRepository,
)
from safent_ads.crm.infrastructure.sql_customer_forgotten_recorder import (
    SqlCustomerForgottenRecorder,
)
from safent_ads.crm.infrastructure.sql_customer_repository import SqlCustomerRepository
from safent_ads.crm.infrastructure.sql_identity_mapping_repository import (
    SqlIdentityMappingRepository,
)
from safent_ads.crm.infrastructure.sql_revenue_event_repository import SqlRevenueEventRepository
from safent_ads.iam.presentation.action_confirmation import require_action_confirmation
from safent_ads.iam.presentation.dependencies import CURRENT_OWNER, AuthenticatedOwner
from safent_ads.iam.presentation.errors import ApiError
from safent_ads.panel.presentation.deps import require_business_access
from safent_ads.shared.clock import Clock
from safent_ads.shared.ids import BusinessId

__all__ = ["build_crm_bridge_router"]

_OwnerDep = Annotated[AuthenticatedOwner, CURRENT_OWNER]
_BusinessIdDep = Annotated[str, Depends(require_business_access)]
_BRIDGE_TOKEN_HEADER = "X-Bridge-Token"  # noqa: S105 - nombre de cabecera, no un secreto
_MAX_BODY_BYTES = 2 * 1024 * 1024
_UNAUTHORIZED = ApiError(
    status_code=401, code="UNAUTHORIZED", message="Token de puente invalido o ausente."
)


def build_crm_bridge_router(
    *, session_factory: async_sessionmaker[AsyncSession], clock: Clock
) -> APIRouter:
    router = APIRouter(prefix="/api/v1/crm", tags=["crm-bridge"])

    @router.post("/customers", status_code=202)
    async def ingest_customers(request: Request, body: dict[str, Any]) -> dict[str, Any]:
        _require_body_within_limit(request)
        async with session_factory() as session:
            business_id = await _authenticate_bridge(request, session)
            items = [_parse_customer_item(raw) for raw in _require_items(body)]
            try:
                result = await IngestCustomers(
                    customers=SqlCustomerRepository(session),
                    identity_mappings=SqlIdentityMappingRepository(session),
                    clock=clock,
                ).execute(
                    business_id=business_id,
                    connector_id=_require_str(body, "connector_id"),
                    items=items,
                )
            except CustomerBatchTooLargeError as exc:
                raise _batch_too_large(exc) from exc
            await session.commit()
        return {
            "accepted": result.accepted,
            "merged": result.merged,
            "rejected": [{"index": r.index, "code": r.code} for r in result.rejected],
        }

    @router.post("/revenue-events", status_code=202)
    async def ingest_revenue_events(request: Request, body: dict[str, Any]) -> dict[str, Any]:
        _require_body_within_limit(request)
        async with session_factory() as session:
            business_id = await _authenticate_bridge(request, session)
            items = [_parse_revenue_event_item(raw) for raw in _require_items(body)]
            try:
                result = await IngestRevenueEvents(
                    customers=SqlCustomerRepository(session),
                    revenue_events=SqlRevenueEventRepository(session),
                ).execute(
                    business_id=business_id,
                    connector_id=_require_str(body, "connector_id"),
                    items=items,
                )
            except RevenueEventBatchTooLargeError as exc:
                raise _batch_too_large(exc) from exc
            await session.commit()
        return {
            "ingested": result.ingested,
            "duplicated": result.duplicated,
            "rejected": [
                {"source_event_id": r.source_event_id, "code": r.code} for r in result.rejected
            ],
        }

    @router.put("/bridge-health", status_code=204)
    async def put_bridge_health(request: Request, body: dict[str, Any]) -> None:
        async with session_factory() as session:
            business_id = await _authenticate_bridge(request, session)
            await RecordBridgeHealth(
                bridge_health=SqlCrmBridgeHealthRepository(session),
                revenue_events=SqlRevenueEventRepository(session),
                clock=clock,
            ).execute(
                business_id=business_id,
                connector_id=_require_str(body, "connector_id"),
                connector_state=_require_str(body, "connector_state"),
                cause=body.get("cause"),
            )
            await session.commit()

    @router.post("/customers/forget")
    async def forget_customer(request: Request, body: dict[str, Any]) -> dict[str, Any]:
        async with session_factory() as session:
            business_id = await _authenticate_bridge(request, session)
            identity_digest = _require_str(body, "identity_digest")
            try:
                result = await ForgetCustomer(
                    customers=SqlCustomerRepository(session),
                    revenue_events=SqlRevenueEventRepository(session),
                    identity_mappings=SqlIdentityMappingRepository(session),
                    audit=SqlCustomerForgottenRecorder(session_factory),
                ).execute(business_id=business_id, identity_digest=identity_digest)
            except InvalidIdentityDigestError as exc:
                raise ApiError(
                    status_code=400, code="IDENTITY_NOT_HASHED", message=str(exc)
                ) from exc
            await session.commit()
        return {"rows_deleted": result.rows_deleted, "recorded": result.recorded}

    @router.post("/bridge-token")
    async def generate_bridge_token(
        request: Request, business_id: _BusinessIdDep, owner: _OwnerDep
    ) -> dict[str, Any]:
        typed_business_id = BusinessId(uuid.UUID(business_id))
        async with session_factory() as session:
            await require_action_confirmation(
                request,
                session,
                owner,
                action_hash=_bridge_token_action_hash(typed_business_id),
                clock=clock,
            )
            token = await GenerateWebhookToken(tokens=SqlCrmBridgeTokenRepository(session)).execute(
                typed_business_id
            )
            await session.commit()
        return {"token": token}

    return router


async def _authenticate_bridge(request: Request, session: AsyncSession) -> BusinessId:
    raw_token = request.headers.get(_BRIDGE_TOKEN_HEADER)
    if not raw_token:
        raise _UNAUTHORIZED
    business_id = await AuthenticateWebhookToken(
        tokens=SqlCrmBridgeTokenRepository(session)
    ).execute(raw_token)
    if business_id is None:
        raise _UNAUTHORIZED
    return business_id


def _require_body_within_limit(request: Request) -> None:
    raw_length = request.headers.get("content-length")
    if raw_length is not None and int(raw_length) > _MAX_BODY_BYTES:
        raise ApiError(
            status_code=413,
            code="PAYLOAD_TOO_LARGE",
            message=f"El cuerpo supera el tope de {_MAX_BODY_BYTES} bytes.",
        )


def _batch_too_large(exc: Exception) -> ApiError:
    return ApiError(status_code=400, code="BATCH_TOO_LARGE", message=str(exc))


def _require_items(body: dict[str, Any]) -> list[dict[str, Any]]:
    items = body.get("items")
    if not isinstance(items, list):
        raise ApiError(status_code=400, code="VALIDATION_ERROR", message="falta 'items'")
    return items


def _require_str(body: dict[str, Any], field: str) -> str:
    value = body.get(field)
    if not isinstance(value, str) or not value:
        raise ApiError(status_code=400, code="VALIDATION_ERROR", message=f"falta '{field}'")
    return value


def _parse_customer_item(raw: dict[str, Any]) -> CustomerIngestItem:
    try:
        return CustomerIngestItem(
            identity_digest=str(raw["identity_digest"]),
            salt_version=int(raw["salt_version"]),
            entity_ref=raw.get("entity_ref"),
            attribution_rung=str(raw["attribution_rung"]),
            currency=str(raw.get("currency") or "EUR"),
        )
    except (KeyError, ValueError, TypeError) as exc:
        raise ApiError(
            status_code=400, code="VALIDATION_ERROR", message=f"item de cliente invalido: {exc}"
        ) from exc


def _parse_revenue_event_item(raw: dict[str, Any]) -> RevenueEventIngestItem:
    try:
        amount = raw["amount"]
        return RevenueEventIngestItem(
            source_event_id=str(raw["source_event_id"]),
            identity_digest=str(raw["identity_digest"]),
            kind=str(raw["kind"]),
            amount_minor=int(amount["amount_minor"]),
            currency=str(amount["currency"]),
            occurred_at=_parse_timestamp(raw["occurred_at"]),
            observed_at=_parse_timestamp(raw["observed_at"]),
            mapping_version=int(raw["mapping_version"]),
        )
    except (KeyError, ValueError, TypeError) as exc:
        raise ApiError(
            status_code=400, code="VALIDATION_ERROR", message=f"hecho de ingreso invalido: {exc}"
        ) from exc


def _parse_timestamp(raw: object) -> datetime:
    return datetime.fromisoformat(str(raw))


def _bridge_token_action_hash(business_id: BusinessId) -> str:
    return hashlib.sha256(f"crm-bridge-token:{business_id}".encode()).hexdigest()

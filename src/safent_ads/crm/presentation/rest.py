"""Borde HTTP de ingesta de conversiones (contracts/rest-api.md
§Conversiones, T220): `POST /conversions/import` (CSV, sesion del
propietario), `POST /conversions/webhook-token` (sesion + confirmación humana,
genera/regenera el token que se muestra una vez), `POST /conversions/
webhook` (SIN sesion -- autenticado por `X-Webhook-Token`, el CRM externo
del propietario nunca tiene cookie de `ads-api`).

Sin logica de negocio: valida forma, llama el caso de uso, mapea a JSON --
mismo criterio que `settings/presentation/rest.py`/`notifications/
presentation/rest.py`."""

from __future__ import annotations

import hashlib
import uuid
from datetime import datetime
from typing import Annotated, Any

from fastapi import APIRouter, Body, Depends, Request, UploadFile
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from safent_ads.crm.application.conversion_ingestion import (
    ConversionRow,
    ConversionRowError,
    build_conversion_signal,
    resolve_conversion_attribution,
)
from safent_ads.crm.application.import_conversions import (
    CsvHeaderError,
    ImportConversionsFromCsv,
)
from safent_ads.crm.application.ports import IdentitySaltProvider
from safent_ads.crm.application.webhook_token import AuthenticateWebhookToken, GenerateWebhookToken
from safent_ads.crm.domain.conversion_kind import ConversionKind
from safent_ads.crm.infrastructure.sql_repositories import SqlLeadAttributionRepository
from safent_ads.crm.infrastructure.sql_webhook_token_repository import SqlWebhookTokenRepository
from safent_ads.iam.presentation.action_confirmation import require_action_confirmation
from safent_ads.iam.presentation.dependencies import CURRENT_OWNER, AuthenticatedOwner
from safent_ads.iam.presentation.errors import ApiError
from safent_ads.panel.presentation.deps import require_business_access
from safent_ads.shared.clock import Clock
from safent_ads.shared.ids import BusinessId

__all__ = ["build_conversions_router"]

_OwnerDep = Annotated[AuthenticatedOwner, CURRENT_OWNER]
_BusinessIdDep = Annotated[str, Depends(require_business_access)]
_MAX_IMPORT_BYTES = 5 * 1024 * 1024
_UPLOAD_CHUNK_BYTES = 64 * 1024
_WEBHOOK_TOKEN_HEADER = "X-Webhook-Token"  # noqa: S105 - nombre de cabecera, no un secreto
_UNAUTHORIZED = ApiError(
    status_code=401, code="UNAUTHORIZED", message="Token de webhook invalido o ausente."
)


def build_conversions_router(
    *,
    session_factory: async_sessionmaker[AsyncSession],
    identity_salt: IdentitySaltProvider,
    clock: Clock,
) -> APIRouter:
    router = APIRouter(prefix="/api/v1/conversions", tags=["conversions"])

    @router.post("/import")
    async def import_conversions(business_id: _BusinessIdDep, file: UploadFile) -> dict[str, Any]:
        csv_text = await _read_csv_within_limit(file)
        async with session_factory() as session:
            try:
                result = await ImportConversionsFromCsv(
                    attributions=SqlLeadAttributionRepository(session),
                    identity_salt=identity_salt,
                    clock=clock,
                ).execute(business_id=BusinessId(uuid.UUID(business_id)), csv_text=csv_text)
            except CsvHeaderError as exc:
                raise ApiError(status_code=422, code="VALIDATION_ERROR", message=str(exc)) from exc
            await session.commit()
        return {
            "imported": result.imported,
            "duplicates": result.duplicates,
            "rejected": [{"line": row.line, "reason": row.reason} for row in result.rejected],
        }

    @router.post("/webhook-token")
    async def generate_webhook_token(
        request: Request, business_id: _BusinessIdDep, owner: _OwnerDep
    ) -> dict[str, Any]:
        typed_business_id = BusinessId(uuid.UUID(business_id))
        async with session_factory() as session:
            await require_action_confirmation(
                request,
                session,
                owner,
                action_hash=_webhook_token_action_hash(typed_business_id),
                clock=clock,
            )
            token = await GenerateWebhookToken(tokens=SqlWebhookTokenRepository(session)).execute(
                typed_business_id
            )
            await session.commit()
        return {"token": token}

    @router.post("/webhook", status_code=202)
    async def ingest_webhook_conversion(
        request: Request, body: Annotated[dict[str, Any], Body(...)]
    ) -> dict[str, Any]:
        raw_token = request.headers.get(_WEBHOOK_TOKEN_HEADER)
        if not raw_token:
            raise _UNAUTHORIZED
        async with session_factory() as session:
            business_id = await AuthenticateWebhookToken(
                tokens=SqlWebhookTokenRepository(session)
            ).execute(raw_token)
            if business_id is None:
                raise _UNAUTHORIZED
            try:
                await _ingest_single_conversion(session, business_id, body, identity_salt, clock)
            except ConversionRowError as exc:
                raise ApiError(status_code=422, code="VALIDATION_ERROR", message=str(exc)) from exc
            await session.commit()
        return {"status": "accepted"}

    return router


async def _ingest_single_conversion(
    session: AsyncSession,
    business_id: BusinessId,
    body: dict[str, Any],
    identity_salt: IdentitySaltProvider,
    clock: Clock,
) -> None:
    row = _parse_webhook_row(body)
    salt = identity_salt.for_business(business_id)
    signal = build_conversion_signal(business_id, row, salt=salt, observed_at=clock.now())
    attribution = resolve_conversion_attribution(signal)
    await SqlLeadAttributionRepository(session).save(attribution)


def _parse_webhook_row(body: dict[str, Any]) -> ConversionRow:
    try:
        return ConversionRow(
            kind=ConversionKind(body["kind"]),
            occurred_at=datetime.fromisoformat(str(body["occurred_at"])),
            amount_minor=int(body.get("amount_minor") or 0),
            currency=str(body.get("currency") or ""),
            offering_id=body.get("offering_id"),
            gclid=body.get("gclid"),
            fbclid=body.get("fbclid"),
            external_ref=body.get("external_ref"),
            email=body.get("email"),
            phone=body.get("phone"),
        )
    except (KeyError, ValueError, TypeError) as exc:
        raise ConversionRowError(f"cuerpo de webhook con forma inesperada: {exc}") from exc


def _webhook_token_action_hash(business_id: BusinessId) -> str:
    return hashlib.sha256(f"conversions-webhook-token:{business_id}".encode()).hexdigest()


async def _read_csv_within_limit(file: UploadFile) -> str:
    """Corta la lectura en streaming al tope de 5 MB (contracts/rest-api.md
    §Conversiones) sin bufferizar un fichero enorme antes de rechazarlo
    (mismo criterio que `brand/presentation/router.py::
    _read_upload_within_limit`, trust boundary, nunca I/O sin limite)."""
    chunks: list[bytes] = []
    total = 0
    while chunk := await file.read(_UPLOAD_CHUNK_BYTES):
        total += len(chunk)
        if total > _MAX_IMPORT_BYTES:
            raise ApiError(
                status_code=422,
                code="VALIDATION_ERROR",
                message=f"El fichero supera el tope de {_MAX_IMPORT_BYTES} bytes.",
            )
        chunks.append(chunk)
    try:
        return b"".join(chunks).decode("utf-8")
    except UnicodeDecodeError as exc:
        raise ApiError(
            status_code=422, code="VALIDATION_ERROR", message="El CSV debe ser UTF-8."
        ) from exc

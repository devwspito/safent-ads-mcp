"""`/api/v1/telegram/pairing*` (rest-api.md §Conexiones, Telegram y ajustes;
contracts/telegram.md §Emparejamiento con el panel; FR-25). Sin logica de
negocio: valida, llama los casos de uso de `notifications/application/
telegram_pairing.py`, mapea a JSON -- la unica excepcion es la escritura de
`decision_log` en `DELETE`, mismo criterio que `composition/execution_rest.py`
(la auditoria de una mutacion es responsabilidad del borde HTTP en este
repo, no del caso de uso).

`build_telegram_pairing_router` recibe primitivas (`session_factory`,
`clock`, `id_generator`, `totp_enc_key`, `allowlist_configured`), nunca
`Container` -- mismo patron que `optimization/presentation/rest.py::
build_optimization_router_over_sql`, para que este modulo siga siendo
presentacion de `notifications` y no un cableado de `composition`."""

from __future__ import annotations

import uuid
from typing import Annotated, Any

from fastapi import APIRouter, Body, Depends, Request
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from safent_ads.audit.application.record_decision import RecordDecision
from safent_ads.audit.domain.entry import ActorKind, DecisionKind, PendingDecision
from safent_ads.audit.infrastructure.sql_repository import SqlDecisionLogRepository
from safent_ads.iam.infrastructure.aesgcm_totp_cipher import AesGcmTotpCipher
from safent_ads.iam.presentation.action_confirmation import require_action_confirmation
from safent_ads.iam.presentation.dependencies import CURRENT_OWNER, AuthenticatedOwner
from safent_ads.iam.presentation.errors import ApiError
from safent_ads.notifications.application.errors import (
    TelegramNotPairedError,
    TelegramTestMessageRateLimitedError,
)
from safent_ads.notifications.application.ports import PairingSnapshot
from safent_ads.notifications.application.telegram_pairing import (
    GetTelegramPairingStatus,
    SendTelegramTestMessage,
    StartTelegramPairing,
    UnpairTelegram,
)
from safent_ads.notifications.domain.pairing import UNSCOPED_BUSINESS_ID, mask_chat_id
from safent_ads.notifications.infrastructure.telegram_pairing_sql import (
    SqlTelegramPairingRepository,
    SqlTestMessageOutbox,
)
from safent_ads.panel.presentation.deps import require_business_access
from safent_ads.shared.clock import Clock
from safent_ads.shared.ids import IdGenerator

_OwnerDep = Annotated[AuthenticatedOwner, CURRENT_OWNER]
# `require_business_access` (panel.presentation.deps): mismo criterio que
# `composition/execution_rest.py::_ScopedBusinessIdDep` -- fail closed,
# 404 nunca 403, para el `business_id` de query param de `GET
# /telegram/pairing` (el emparejamiento en si es del PROPIETARIO, ver
# `get_pairing`, pero la ruta sigue la misma convencion IDOR que el resto
# del contrato).
_ScopedBusinessIdDep = Annotated[str, Depends(require_business_access)]
_UNPAIR_CONFIRMATION_PHRASE = "DESEMPAREJAR"


def build_telegram_pairing_router(
    *,
    session_factory: async_sessionmaker[AsyncSession],
    totp_enc_key: str,
    allowlist_configured: bool,
    clock: Clock,
    id_generator: IdGenerator,
) -> APIRouter:
    cipher = AesGcmTotpCipher(totp_enc_key)
    router = APIRouter(prefix="/api/v1/telegram", tags=["telegram-pairing"])

    @router.get("/pairing")
    async def get_pairing(business_id: _ScopedBusinessIdDep, owner: _OwnerDep) -> dict[str, Any]:
        del business_id  # solo IDOR (rest-api.md): el emparejamiento es del PROPIETARIO
        async with session_factory() as session:
            snapshot = await GetTelegramPairingStatus(
                pairing=SqlTelegramPairingRepository(session, cipher=cipher), clock=clock
            ).execute(owner.owner_id)
        return _pairing_json(snapshot, allowlist_configured=allowlist_configured)

    @router.post("/pairing/start", status_code=201)
    async def start_pairing(request: Request, owner: _OwnerDep) -> dict[str, Any]:
        if not allowlist_configured:
            raise ApiError(
                status_code=409,
                code="TELEGRAM_ALLOWLIST_EMPTY",
                message="TELEGRAM_OWNER_CHAT_IDS esta vacia: paso de despliegue pendiente.",
            )
        async with session_factory() as session:
            await require_action_confirmation(
                request,
                session,
                owner,
                action_hash=_start_pairing_action_hash(owner.owner_id),
                clock=clock,
            )
            started = await StartTelegramPairing(
                pairing=SqlTelegramPairingRepository(session, cipher=cipher),
                clock=clock,
                allowlist_configured=allowlist_configured,
            ).execute(owner.owner_id)
            await session.commit()
        return {
            "pairing_code": started.pairing_code,
            "code_expires_at": started.code_expires_at.isoformat(),
        }

    @router.delete("/pairing", status_code=204)
    async def delete_pairing(owner: _OwnerDep, body: Annotated[dict[str, Any], Body(...)]) -> None:
        _require_typed_confirmation(body)
        async with session_factory() as session:
            await UnpairTelegram(
                pairing=SqlTelegramPairingRepository(session, cipher=cipher)
            ).execute(owner.owner_id)
            await RecordDecision(SqlDecisionLogRepository(session)).execute(
                PendingDecision(
                    business_id=UNSCOPED_BUSINESS_ID,
                    kind=DecisionKind.TELEGRAM_UNPAIRED,
                    actor_kind=ActorKind.OWNER,
                    actor_id=owner.email,
                    payload={},
                )
            )
            await session.commit()

    @router.post("/pairing/test-message", status_code=202)
    async def send_test_message(owner: _OwnerDep) -> dict[str, Any]:
        async with session_factory() as session:
            try:
                notification_id = await SendTelegramTestMessage(
                    pairing=SqlTelegramPairingRepository(session, cipher=cipher),
                    outbox=SqlTestMessageOutbox(session),
                    id_generator=id_generator,
                    clock=clock,
                ).execute(owner.owner_id)
            except TelegramNotPairedError as exc:
                raise ApiError(
                    status_code=409,
                    code="TELEGRAM_NOT_PAIRED",
                    message="No hay un emparejamiento vigente para enviar la prueba.",
                ) from exc
            except TelegramTestMessageRateLimitedError as exc:
                raise ApiError(
                    status_code=429,
                    code="RATE_LIMITED",
                    message="Demasiados mensajes de prueba recientes, espera unos minutos.",
                ) from exc
            await session.commit()
        return {"notification_id": str(notification_id)}

    return router


def _require_typed_confirmation(body: dict[str, Any]) -> None:
    typed_confirmation = str(body.get("typed_confirmation") or "").strip().upper()
    if typed_confirmation != _UNPAIR_CONFIRMATION_PHRASE:
        raise ApiError(
            status_code=428,
            code="TYPED_CONFIRMATION_REQUIRED",
            message=f"Escribe {_UNPAIR_CONFIRMATION_PHRASE} para confirmar.",
            details={"phrase": _UNPAIR_CONFIRMATION_PHRASE},
        )


def _start_pairing_action_hash(owner_id: uuid.UUID) -> str:
    """A diferencia de `execution_rest.py::_reauth_action_hash` (ata cuenta
    + limite + valor), aqui no hay mas "accion" que "este propietario pide
    un codigo nuevo": el `owner_id` ya la identifica por completo."""
    return f"telegram_pairing_start|{owner_id}"


def _pairing_json(snapshot: PairingSnapshot, *, allowlist_configured: bool) -> dict[str, Any]:
    return {
        "status": snapshot.status.value,
        "chat_id_masked": None if snapshot.chat_id is None else mask_chat_id(snapshot.chat_id),
        "paired_at": None if snapshot.paired_at is None else snapshot.paired_at.isoformat(),
        "allowlist_configured": allowlist_configured,
        "pairing_code": snapshot.pairing_code,
        "code_expires_at": (
            None if snapshot.code_expires_at is None else snapshot.code_expires_at.isoformat()
        ),
    }

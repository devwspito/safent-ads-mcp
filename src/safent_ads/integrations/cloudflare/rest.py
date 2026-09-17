"""`/api/v1/integrations/cloudflare*` (lane 006-cloudflare-ui, owner
decision: "crear una conexion con Cloudflare pidiendo el token e
indicando el enlace donde crearlo"). Sesion de propietario + CSRF de
doble envio (middleware global de `composition/api.py`, mismo criterio
que el resto de mutaciones) -- sin `business_id`: la conexion es de la
INSTALACION, no de un negocio (mismo trato que `/platform-apps`).

`build_cloudflare_connection_router` recibe primitivas (`session_factory`,
`totp_enc_key`, `clock`, `fallback_token_configured`, `cloudflare_allowed_
zones`), nunca `Container` -- mismo patron que `notifications/
presentation/rest.py::build_telegram_pairing_router`."""

from __future__ import annotations

import uuid
from typing import Annotated, Any

from fastapi import APIRouter, Body
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from safent_ads.audit.application.record_decision import RecordDecision
from safent_ads.audit.domain.entry import ActorKind, DecisionKind, PendingDecision
from safent_ads.audit.infrastructure.sql_repository import SqlDecisionLogRepository
from safent_ads.iam.infrastructure.aesgcm_totp_cipher import AesGcmTotpCipher
from safent_ads.iam.presentation.dependencies import CURRENT_OWNER, AuthenticatedOwner
from safent_ads.iam.presentation.errors import ApiError
from safent_ads.integrations.cloudflare.connection_port import (
    CloudflareConnectionRecord,
    CloudflareConnectionStatus,
    CloudflareTokenInvalidError,
)
from safent_ads.integrations.cloudflare.connection_service import (
    ConnectCloudflareToken,
    DisconnectCloudflareToken,
    GetCloudflareConnectionStatus,
)
from safent_ads.integrations.cloudflare.connection_store import SqlCloudflareConnectionStore
from safent_ads.shared.clock import Clock
from safent_ads.shared.ids import BusinessId

_MAX_TOKEN_LENGTH = 4096
_DISCONNECT_CONFIRMATION_PHRASE = "DESCONECTAR"
# Conexion de Cloudflare: por INSTALACION, no por negocio -- mismo
# `business_id` nulo que `accounts/presentation/platform_apps_router.py::
# _UNSCOPED_BUSINESS_ID` usa para las credenciales de VENDOR (tambien por
# PROPIETARIO), copia local por el mismo motivo (plan.md §4: modulos de un
# solo sentido, sin acoplar `integrations` a `accounts`).
_UNSCOPED_BUSINESS_ID = BusinessId.parse("00000000-0000-0000-0000-000000000000")
# Cloudflare emite IDs de cuenta como hex de 32 caracteres en minuscula
# (mismo patron que `client.py::_ACCOUNT_ID_PATTERN`, validado tambien
# aqui: el borde HTTP nunca confia en que la infraestructura sea la unica
# guardia).
_ACCOUNT_ID_PATTERN = r"^[0-9a-f]{32}$"

_OwnerDep = Annotated[AuthenticatedOwner, CURRENT_OWNER]


class _StrictModel(BaseModel):
    """Rechaza campos no declarados (threat-model.md C-11) -- mismo
    criterio que `accounts.presentation.payloads.StrictModel`, copia local
    para no acoplar `integrations` a `accounts` (plan.md §4: modulos de un
    solo sentido)."""

    model_config = ConfigDict(extra="forbid", frozen=True)


class ConnectCloudflareTokenRequest(_StrictModel):
    token: str = Field(min_length=1, max_length=_MAX_TOKEN_LENGTH)
    account_id: str | None = Field(default=None, pattern=_ACCOUNT_ID_PATTERN)


def _status_json(status: CloudflareConnectionStatus) -> dict[str, Any]:
    return {
        "connected": status.connected,
        "account_id": status.account_id,
        "zones": list(status.zones),
        "create_token_url": status.create_token_url,
        "required_permissions": list(status.required_permissions),
        "connected_at": None if status.connected_at is None else status.connected_at.isoformat(),
    }


def _token_invalid_error(exc: CloudflareTokenInvalidError) -> ApiError:
    return ApiError(
        status_code=422,
        code="CLOUDFLARE_TOKEN_INVALID",
        message=str(exc),
    )


def _connection_decision(
    *,
    kind: DecisionKind,
    actor_id: str,
    account_id: str | None,
    zone_count: int,
    connected_by_owner_id: uuid.UUID | None = None,
) -> PendingDecision:
    """`account_id`/`zone_count`, NUNCA el token (`_FORBIDDEN_PAYLOAD_KEYS`
    ademas lo rechazaria) -- revision de seguridad de la conexion
    Cloudflare (2026-09-15, hallazgo medio): conectar/desconectar no
    dejaba ningun rastro queryable, mismo criterio que
    `PLATFORM_APP_CREDENTIALS_SET`/`_DELETED`.

    `connected_by_owner_id` (solo en `DISCONNECTED`): `DELETE` borra la
    fila de `cloudflare_connection`, incluido ese campo -- sin copiarlo
    aqui, "quien conecto esto" se pierde para siempre en cuanto alguien
    desconecta. El `decision_log` es la unica historia que sobrevive."""
    payload: dict[str, Any] = {"account_id": account_id, "zone_count": zone_count}
    if connected_by_owner_id is not None:
        payload["connected_by_owner_id"] = str(connected_by_owner_id)
    return PendingDecision(
        business_id=_UNSCOPED_BUSINESS_ID,
        kind=kind,
        actor_kind=ActorKind.OWNER,
        actor_id=actor_id,
        payload=payload,
    )


def _require_typed_confirmation(body: dict[str, Any]) -> None:
    """Mismo patron que `platform_apps_router.py`/`notifications.presentation.
    rest.py` (palabra tecleada, nunca reauth TOTP): desconectar Cloudflare dejaria
    sin credencial las cuatro herramientas de DNS del MCP hasta que el
    propietario vuelva a conectar."""
    typed = str(body.get("typed_confirmation") or "").strip().upper()
    if typed != _DISCONNECT_CONFIRMATION_PHRASE:
        raise ApiError(
            status_code=428,
            code="TYPED_CONFIRMATION_REQUIRED",
            message=f"Escribe {_DISCONNECT_CONFIRMATION_PHRASE} para confirmar.",
            details={"phrase": _DISCONNECT_CONFIRMATION_PHRASE},
        )


def build_cloudflare_connection_router(
    *,
    session_factory: async_sessionmaker[AsyncSession],
    totp_enc_key: str,
    fallback_token_configured: bool,
    clock: Clock,
) -> APIRouter:
    cipher = AesGcmTotpCipher(totp_enc_key)
    router = APIRouter(prefix="/api/v1/integrations/cloudflare", tags=["integrations-cloudflare"])

    @router.get("")
    async def get_connection(_owner: _OwnerDep) -> dict[str, Any]:
        async with session_factory() as session:
            status = await GetCloudflareConnectionStatus(
                SqlCloudflareConnectionStore(session, cipher=cipher),
                fallback_token_configured=fallback_token_configured,
            ).execute()
        return _status_json(status)

    @router.post("/token", status_code=201)
    async def connect_token(
        body: ConnectCloudflareTokenRequest, owner: _OwnerDep
    ) -> dict[str, Any]:
        async with session_factory() as session:
            try:
                status = await ConnectCloudflareToken(
                    SqlCloudflareConnectionStore(session, cipher=cipher), clock=clock
                ).execute(token=body.token, account_id=body.account_id, owner_id=owner.owner_id)
            except CloudflareTokenInvalidError as exc:
                raise _token_invalid_error(exc) from exc
            await RecordDecision(SqlDecisionLogRepository(session)).execute(
                _connection_decision(
                    kind=DecisionKind.CLOUDFLARE_CONNECTED,
                    actor_id=owner.email,
                    account_id=status.account_id,
                    zone_count=len(status.zones),
                )
            )
            await session.commit()
        return _status_json(status)

    @router.delete("/token", status_code=204)
    async def disconnect_token(
        owner: _OwnerDep, body: Annotated[dict[str, Any], Body(default_factory=dict)]
    ) -> None:
        _require_typed_confirmation(body)
        async with session_factory() as session:
            store = SqlCloudflareConnectionStore(session, cipher=cipher)
            previous: CloudflareConnectionRecord | None = await store.get()
            await DisconnectCloudflareToken(store).execute()
            await RecordDecision(SqlDecisionLogRepository(session)).execute(
                _connection_decision(
                    kind=DecisionKind.CLOUDFLARE_DISCONNECTED,
                    actor_id=owner.email,
                    account_id=previous.account_id if previous is not None else None,
                    zone_count=len(previous.zones) if previous is not None else 0,
                    connected_by_owner_id=(
                        previous.connected_by_owner_id if previous is not None else None
                    ),
                )
            )
            await session.commit()

    return router

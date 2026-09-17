"""Borde HTTP de `settings` (contracts/rest-api.md §Ajustes): `GET/PUT
/settings`. `timezone`/`currency` los dicta la plataforma (data-model.md
§PlatformAccount): se leen del mismo `SettingsView`, nunca se aceptan en
el cuerpo de `PUT` -- `_parse_update_command` ni los mira."""

from __future__ import annotations

import uuid
from typing import Annotated, Any

from fastapi import APIRouter, Body, Depends

from safent_ads.iam.presentation.dependencies import CURRENT_OWNER, AuthenticatedOwner
from safent_ads.iam.presentation.errors import ApiError
from safent_ads.panel.presentation.deps import require_business_access
from safent_ads.settings.application.dto import SettingsView
from safent_ads.settings.application.get_settings import (
    BusinessNotFoundError,
    GetSettings,
    GetSettingsCommand,
)
from safent_ads.settings.application.update_settings import UpdateSettings, UpdateSettingsCommand
from safent_ads.settings.domain.value_objects import (
    ActiveHours,
    DigestHour,
    InvalidActiveHoursError,
    InvalidDigestHourError,
    Theme,
)

__all__ = ["build_settings_router"]

_OwnerDep = Annotated[AuthenticatedOwner, CURRENT_OWNER]
_BusinessIdDep = Annotated[str, Depends(require_business_access)]
_NOT_FOUND = ApiError(status_code=404, code="NOT_FOUND", message="No encontrado.")


def build_settings_router(get_settings: GetSettings, update_settings: UpdateSettings) -> APIRouter:
    router = APIRouter(prefix="/api/v1", tags=["settings"])

    @router.get("/settings")
    async def get_settings_route(business_id: _BusinessIdDep, owner: _OwnerDep) -> dict[str, Any]:
        try:
            view = await get_settings.execute(
                GetSettingsCommand(business_id=business_id, owner_id=owner.owner_id)
            )
        except BusinessNotFoundError as exc:
            raise _NOT_FOUND from exc
        return _to_json(view)

    @router.put("/settings")
    async def put_settings_route(
        business_id: _BusinessIdDep, owner: _OwnerDep, body: Annotated[dict[str, Any], Body(...)]
    ) -> dict[str, Any]:
        command = _parse_update_command(body, business_id=business_id, owner_id=owner.owner_id)
        try:
            view = await update_settings.execute(command)
        except BusinessNotFoundError as exc:
            raise _NOT_FOUND from exc
        return _to_json(view)

    return router


def _parse_update_command(
    body: dict[str, Any], *, business_id: str, owner_id: uuid.UUID
) -> UpdateSettingsCommand:
    try:
        active_hours = _parse_active_hours(body)
        digest_hour = DigestHour.parse(_require_str(body, "digest_hour"))
        theme = Theme(_require_str(body, "theme"))
    except (InvalidActiveHoursError, InvalidDigestHourError) as exc:
        raise ApiError(status_code=422, code="VALIDATION_ERROR", message=str(exc)) from exc
    except ValueError as exc:
        raise ApiError(status_code=422, code="VALIDATION_ERROR", message="theme invalido") from exc
    return UpdateSettingsCommand(
        business_id=business_id,
        owner_id=owner_id,
        active_hours=active_hours,
        digest_hour=digest_hour,
        theme=theme,
    )


def _parse_active_hours(body: dict[str, Any]) -> ActiveHours:
    raw = body.get("active_hours")
    if not isinstance(raw, dict):
        raise ApiError(status_code=422, code="VALIDATION_ERROR", message="active_hours requerido")
    return ActiveHours.parse(start=_require_str(raw, "start"), end=_require_str(raw, "end"))


def _require_str(body: dict[str, Any], key: str) -> str:
    value = body.get(key)
    if not isinstance(value, str) or not value:
        raise ApiError(status_code=422, code="VALIDATION_ERROR", message=f"{key} requerido")
    return value


def _to_json(view: SettingsView) -> dict[str, Any]:
    return {
        "business_id": view.business_id,
        "timezone": view.timezone,
        "currency": view.currency,
        "active_hours": {
            "start": view.active_hours.start_label,
            "end": view.active_hours.end_label,
        },
        "digest_hour": view.digest_hour.label,
        "theme": view.theme.value,
    }

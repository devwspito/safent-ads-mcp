"""`GET /platform-accounts` (contracts/rest-api.md §Conexiones, lineas
330-342): mapea `PlatformAccountConnectionView` (application, ya junta
cuenta + credencial) a la forma REST exacta que exige
`panel/src/api/schemas/connections.ts::platformAccountSchema`. Funcion
pura, no un metodo de `payloads.py` -- ese modulo se declara "sin logica"
(solo forma/validacion de entrada) y calcular `token.health` a partir de
`expires_at` SI es logica (comparacion contra `now`).

La clasificacion en si (`CredentialHealth`) vive en el dominio
(`accounts.domain.platform_credential.classify_credential_health`): el
cron de salud (`CheckCredentialHealth`, threat-model.md C-21, tasks.md
T126) usa exactamente la misma regla para decidir si alerta, para que este
endpoint y esa alerta nunca puedan divergir."""

from __future__ import annotations

from datetime import datetime
from typing import Any

from safent_ads.accounts.application.list_platform_accounts import PlatformAccountConnectionView
from safent_ads.accounts.domain.platform_credential import (
    CredentialHealth,
    classify_credential_health,
)

__all__ = ["platform_account_to_json"]


def platform_account_to_json(
    view: PlatformAccountConnectionView, *, now: datetime
) -> dict[str, Any]:
    health = _resolve_health(view, now=now)
    return {
        "platform_account_id": str(view.account_ref),
        "platform": view.account_ref.platform.value,
        "external_account_id": view.account_ref.external_account_id,
        "label": view.label,
        "status": view.status.name,
        "currency": view.currency,
        "timezone": view.timezone,
        "api_tier": view.api_tier.value,
        "token": _token_json(view, health=health, now=now),
        # Ni cuota real de plataforma ni `unavailable_levers` (Advantage+/
        # PMax por tipo de campana) tienen fuente persistida todavia --
        # "sin dato conocido" honesto (`None`/`[]`), nunca un numero
        # inventado (mismo criterio que `caps_and_pacing.py: is_partial`).
        "quota": {"window": "daily", "used_pct": None, "writes_remaining": None},
        "unavailable_levers": [],
        "last_synced_at": _iso(view.last_synced_at),
        # Persistido por `CheckCredentialHealth` en su ultimo chequeo
        # (tasks.md T126); `None` cuando no hay credencial que reportar o
        # todavia no corrio ningun ciclo (checklists/
        # panel-contract-followups.md #4, antes siempre `null`).
        "last_error_code": view.credential_last_error_code,
    }


def _resolve_health(view: PlatformAccountConnectionView, *, now: datetime) -> CredentialHealth:
    if view.credential_status is None:
        # Cuenta sin fila de credencial resoluble: fail-closed, misma
        # exigencia de reconectar que una credencial revocada.
        return CredentialHealth.REVOKED
    return classify_credential_health(view.credential_status, view.credential_expires_at, now=now)


def _token_json(
    view: PlatformAccountConnectionView, *, health: CredentialHealth, now: datetime
) -> dict[str, Any]:
    checked_at = view.credential_checked_at or view.credential_last_validated_at or now
    return {
        "health": health.value,
        "expires_at": _iso(view.credential_expires_at),
        "checked_at": checked_at.isoformat(),
    }


def _iso(value: datetime | None) -> str | None:
    return None if value is None else value.isoformat()

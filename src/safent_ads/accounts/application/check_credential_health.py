"""`CheckCredentialHealth` (tasks.md T126, threat-model.md C-21): por cada
`PlatformAccount` de un negocio, pregunta al broker el estado de su
credencial -- `OAuthBrokerPort.credential_status`, la misma operacion de
solo lectura que ya usa `GET /platform-accounts` (`connect_ports.py`: "por
construccion, ninguna forma... tiene un campo capaz de llevar un token") --
y lo persiste en `credential_refs`. Nunca contacta Google/Meta: el broker
resuelve esto contra su propio almacen local, sin salir a la plataforma
(`broker/application/oauth_connect_flow.py::status`).

Devuelve solo las transiciones de salud detectadas (`previous_health !=
current_health`): quien orquesta el ciclo (`orchestration/infrastructure`,
otra lane) decide si alerta y como -- `accounts` no conoce `notifications`
(plan.md §4, grafo aciclico)."""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime
from typing import Final

from safent_ads.accounts.application.connect_ports import CredentialRepository, OAuthBrokerPort
from safent_ads.accounts.application.errors import BrokerRequestDeniedError
from safent_ads.accounts.application.ports import AccountRepository
from safent_ads.accounts.domain.platform_account import PlatformAccount
from safent_ads.accounts.domain.platform_credential import (
    CredentialHealth,
    CredentialStatus,
    classify_credential_health,
    error_code_for_health,
)
from safent_ads.accounts.domain.refs import AccountRef, CredentialRefId
from safent_ads.shared.ids import BusinessId

_CREDENTIAL_NOT_FOUND: Final = "CREDENTIAL_NOT_FOUND"


@dataclass(frozen=True, slots=True, kw_only=True)
class CredentialHealthTransition:
    """Una cuenta cuya salud cambio en este chequeo (por eso nunca lleva
    `business_id`/`account_ref` opcionales: solo existe cuando hay algo que
    reportar)."""

    business_id: BusinessId
    account_ref: AccountRef
    previous_health: CredentialHealth
    current_health: CredentialHealth
    error_code: str | None


class CheckCredentialHealth:
    def __init__(
        self,
        accounts: AccountRepository,
        credentials: CredentialRepository,
        oauth_broker: OAuthBrokerPort,
    ) -> None:
        self._accounts = accounts
        self._credentials = credentials
        self._oauth_broker = oauth_broker

    async def execute(
        self, business_id: BusinessId, *, now: datetime
    ) -> Sequence[CredentialHealthTransition]:
        accounts = await self._accounts.list_by_business(business_id)
        transitions: list[CredentialHealthTransition] = []
        for account in accounts:
            transition = await self._check_one(business_id, account, now=now)
            if transition is not None:
                transitions.append(transition)
        return transitions

    async def _check_one(
        self, business_id: BusinessId, account: PlatformAccount, *, now: datetime
    ) -> CredentialHealthTransition | None:
        credential = await self._credentials.get(
            account.credential_ref_id, business_id=business_id
        )
        if credential is None:
            return None

        previous_health = credential.health(now=now)
        status, expires_at, error_code = await self._probe(account.credential_ref_id, now=now)
        credential.record_health_check(
            at=now, status=status, expires_at=expires_at, error_code=error_code
        )
        await self._credentials.save(credential)

        current_health = credential.health(now=now)
        if current_health == previous_health:
            return None
        return CredentialHealthTransition(
            business_id=business_id,
            account_ref=account.account_ref,
            previous_health=previous_health,
            current_health=current_health,
            error_code=error_code,
        )

    async def _probe(
        self, credential_ref_id: CredentialRefId, *, now: datetime
    ) -> tuple[CredentialStatus, datetime | None, str | None]:
        try:
            result = await self._oauth_broker.credential_status(credential_ref_id)
        except BrokerRequestDeniedError as exc:
            if exc.error_code != _CREDENTIAL_NOT_FOUND:
                raise
            return CredentialStatus.REVOKED, None, _CREDENTIAL_NOT_FOUND

        health = classify_credential_health(result.status, result.expires_at, now=now)
        return result.status, result.expires_at, error_code_for_health(health)

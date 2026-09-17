"""Caso de uso de `GET /mcp/health` (contrato del companion, definido en
el runtime: "Lo que debe exponer el servicio de ads" -- `{status,
contract_version, accounts_linked:{google, meta}, db}`, protegido por el
mismo bearer que `/mcp`).

`CONTRACT_VERSION` es la unica fuente del semver que el runtime companion
usa para el gate de compatibilidad (plan.md STRIDE D-2): subirla es la
unica forma de anunciar un cambio de contrato, nunca se duplica en otro
fichero."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Final, Protocol

CONTRACT_VERSION: Final[str] = "1.0.0"


class AccountLinkStatusPort(Protocol):
    """Que plataformas tienen al menos una `PlatformAccount` vinculada
    (`platform_accounts`, data-model.md). Nunca expone identificadores de
    cuenta ni credenciales -- solo el booleano agregado por plataforma."""

    async def linked_platforms(self) -> frozenset[str]: ...


class DatabaseHealthPort(Protocol):
    """Latido minimo de la base de datos (`SELECT 1`)."""

    async def ping(self) -> bool: ...


@dataclass(frozen=True, slots=True)
class AccountsLinked:
    google: bool
    meta: bool


@dataclass(frozen=True, slots=True)
class HealthReport:
    status: str
    contract_version: str
    accounts_linked: AccountsLinked
    db: str


class GetHealthStatus:
    """Un unico `execute()` (SRP): la BD caida degrada `status`/`db`, pero
    nunca hace que el endpoint falle o filtre una traza -- los puertos ya
    fallan cerrado a un valor seguro (`mcp.infrastructure.sql_health_ports`,
    "un health check nunca debe propagar", mismo criterio que
    `composition/api.py::_check_database`)."""

    def __init__(self, *, accounts: AccountLinkStatusPort, database: DatabaseHealthPort) -> None:
        self._accounts = accounts
        self._database = database

    async def execute(self) -> HealthReport:
        db_ok = await self._database.ping()
        linked = await self._accounts.linked_platforms()
        return HealthReport(
            status="ok" if db_ok else "degraded",
            contract_version=CONTRACT_VERSION,
            accounts_linked=AccountsLinked(google="google" in linked, meta="meta" in linked),
            db="ok" if db_ok else "error",
        )

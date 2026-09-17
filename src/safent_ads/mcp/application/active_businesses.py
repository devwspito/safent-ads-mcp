"""`ActiveBusinessIdsPort`: los negocios activos del despliegue, sin filtrar
por alcance.

Lo necesitan los resolutores de `CallerScope` que conceden acceso a TODOS
los negocios del propietario -- el modo de un solo propietario
(`SingleOwnerCallerScopeResolver`) y una concesion OAuth propia
(`mcp_oauth.presentation.caller_scope.OAuthCallerScopeResolver`) -- porque
`BusinessDirectoryPort` ya FILTRA por `CallerScope.allowed_business_ids` y
no puede usarse para construirlo (seria circular, y antes de la fusion
lane/003 + spec 002 se resolvia pasandole `None`, el comodin que
contracts/mcp.md §4 prohibe)."""

from __future__ import annotations

from typing import Protocol


class ActiveBusinessIdsPort(Protocol):
    async def active_business_ids(self) -> frozenset[str]: ...

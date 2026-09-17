"""`RevokeGrant` (contracts/oauth.md §6 `/revoke`, C-55 «Agentes
conectados»): revoca una concesion completa (acceso + refresco).
Idempotente: revocar un `grant_id` ya revocado no es un error (RFC 7009:
`/revoke` responde siempre 200).

`reason` lo decide el llamador (data-model.md "Desviaciones aplicadas en
0035" #8: `revoked_reason` es texto libre 1-200, valores en uso
`panel`/`revoke_endpoint`/`code_replay`/`refresh_reuse`) -- este caso de
uso no inventa un motivo generico propio: `presentation/sdk_provider.py`
(RFC 7009 `/revoke`) y `presentation/grants_router.py` (panel, T015b) piden
la misma revocacion por razones distintas y auditables por separado."""

from __future__ import annotations

import uuid

from safent_ads.mcp_oauth.application.errors import GrantNotFoundError
from safent_ads.mcp_oauth.application.ports import GrantRepository
from safent_ads.shared.clock import Clock


class RevokeGrant:
    def __init__(self, *, grants: GrantRepository, clock: Clock) -> None:
        self._grants = grants
        self._clock = clock

    async def execute(self, *, grant_id: uuid.UUID, owner_id: uuid.UUID, reason: str) -> None:
        grant = await self._grants.get_by_id(grant_id)
        if grant is None or grant.owner_id != owner_id:
            raise GrantNotFoundError(f"concesion {grant_id} no encontrada")
        if grant.is_revoked:
            return
        grant.revoke(now=self._clock.now(), reason=reason)
        await self._grants.save(grant)

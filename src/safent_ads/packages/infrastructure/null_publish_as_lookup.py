"""`NullPublishAsLookup`: implementa `PublishAsLookupPort` (T101) sin
ninguna fuente real -- el esquema de `accounts` (0001_bootstrap.py) no
guarda todavia ninguna pagina de Meta asociada a una conexion. Documentado
como gap en el informe de la rama: mientras no exista esa infraestructura,
`ProposeCampaignPackage` falla con `PLATFORM_NATIVE_INCOMPLETE` para Meta
en vez de inventar una pagina o bloquear en silencio."""

from __future__ import annotations

from safent_ads.packages.application.ports import ResolvedPublishAs
from safent_ads.shared.ids import EntityRef

__all__ = ["NullPublishAsLookup"]


class NullPublishAsLookup:
    async def resolve(self, *, account_ref: EntityRef) -> ResolvedPublishAs | None:  # noqa: ARG002
        return None

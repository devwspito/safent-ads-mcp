"""`SqlLandingDomainPolicy`: implementa `LandingDomainPolicyPort` sobre
`brand_kits.confirmed_website_host` (migracion 0018) por SQL directo --
`brand` no esta en `packages -> {proposals, execution, creative, accounts,
shared}` (data-model.md "Bounded contexts": es fuente de solo lectura para
el AGENTE, no una dependencia de `packages`).

Gap documentado: `catalog.domain.offering.OfferingDetails` no modela
ninguna URL de oferta todavia, asi que la mitad de la regla de
`contracts/mcp-tools.md` Revision 2 §R2.3 ("host de la landing de la
oferta O del sitio de la marca") solo cubre hoy el sitio de la marca."""

from __future__ import annotations

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from safent_ads.shared.ids import BusinessId

__all__ = ["SqlLandingDomainPolicy"]

_CONFIRMED_WEBSITE_HOST_SQL = text(
    "SELECT confirmed_website_host FROM brand_kits WHERE business_id = :business_id"
)


class SqlLandingDomainPolicy:
    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def allowed_hosts(self, *, business_id: BusinessId) -> frozenset[str]:
        host = (
            await self._session.execute(
                _CONFIRMED_WEBSITE_HOST_SQL, {"business_id": business_id.value}
            )
        ).scalar_one_or_none()
        return frozenset({host.lower()}) if host else frozenset()

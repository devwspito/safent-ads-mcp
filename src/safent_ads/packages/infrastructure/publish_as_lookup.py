"""`MetaPagesPublishAsLookup`: implementa `PublishAsLookupPort` (T101)
resolviendo la pagina de Meta ya conectada a la cuenta, en vez del
`NullPublishAsLookup` documentado como hueco (gap descubierto en la rama
anterior: "el esquema de `accounts` no guarda todavia ninguna pagina de
Meta asociada a una conexion" -- **falso** hoy: `BrokerReferenceDataPort.
list_meta_pages` (historia 18, R3) ya lee esa lista real del bróker via
`meta_reference_read`. Solo faltaba conectar los dos.

`MetaPageLookupPort` declara la forma MINIMA que necesita (no importa
`mcp.application.reference_data_port.MetaPage`: `packages -> {proposals,
execution, creative, accounts, shared}` no incluye `mcp`) -- estructural,
por eso `composition/app.py` puede pasarle el `BrokerReferenceDataPort`
que ya construye para el catalogo MCP, tal cual, sin adaptador."""

from __future__ import annotations

from collections.abc import Sequence
from typing import Protocol

from safent_ads.packages.application.ports import ResolvedPublishAs
from safent_ads.shared.ids import EntityRef

__all__ = ["MetaPageLike", "MetaPageLookupPort", "MetaPagesPublishAsLookup"]


class MetaPageLike(Protocol):
    @property
    def page_id(self) -> str: ...

    @property
    def name(self) -> str: ...


class MetaPageLookupPort(Protocol):
    async def list_meta_pages(
        self, *, business_id: str, account_ref: str
    ) -> Sequence[MetaPageLike]: ...


class MetaPagesPublishAsLookup:
    """Una sola pagina conectada resuelve sin preguntar; cero o mas de una
    fallan cerrado -- `ProposeCampaignPackage` traduce `None` a
    `PLATFORM_NATIVE_INCOMPLETE` (nunca inventa una pagina ni elige al
    azar entre varias). Elegir entre varias paginas es P3 (reduccion de
    preguntas), fuera de esta entrega."""

    def __init__(self, pages: MetaPageLookupPort) -> None:
        self._pages = pages

    async def resolve(self, *, account_ref: EntityRef) -> ResolvedPublishAs | None:
        if account_ref.business_id is None:
            return None
        pages = await self._pages.list_meta_pages(
            business_id=str(account_ref.business_id), account_ref=str(account_ref)
        )
        if len(pages) != 1:
            return None
        page = pages[0]
        return ResolvedPublishAs(page_id=page.page_id, page_name=page.name)

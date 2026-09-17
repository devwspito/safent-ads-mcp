"""004 tasks-2.md R7 (historias 21-23): `search_competitor_ads` necesita la
app de Meta de la empresa (razon *a*), por eso pasa por el broker. Google no
ofrece una API de la Biblioteca de Anuncios/Centro de Transparencia:
`available=false` tipado, nunca una descarga. Modulo autonomo, puro en sus
DTOs -- sin I/O, sin framework."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from enum import StrEnum
from typing import Protocol

from safent_ads.mcp.domain.competitor_urls import CompetitorLinks

__all__ = [
    "CompetitorAd",
    "CompetitorAdsResult",
    "CompetitorPlatform",
    "CompetitorResearchPort",
    "MetaAdLibraryUnavailableError",
    "MetaAdLibraryUnavailableReason",
]


class CompetitorPlatform(StrEnum):
    META = "meta"
    GOOGLE = "google"


@dataclass(frozen=True, slots=True)
class CompetitorAd:
    advertiser_name: str
    ad_text: str | None
    image_url: str | None
    start_date: date | None
    stop_date: date | None
    platforms: tuple[str, ...]
    reach_by_country: dict[str, int] | None


@dataclass(frozen=True, slots=True)
class CompetitorAdsResult:
    """`available=False` es una respuesta tipada valida (Google sin API, o
    Meta sin cuenta conectada / exigiendo confirmar identidad / con un
    fallo del proveedor): `ads` vacio, `reason` un codigo estable (nunca
    prosa que pueda quedar desactualizada o filtrar detalle del
    proveedor), `next_steps` una linea en castellano con lo que el
    propietario puede hacer ahora mismo (`None` cuando no hay nada
    accionable, p.ej. Google sin API), `fallback` la misma linea siempre
    que `available=False` -- la pagina publica sigue ahi aunque la API no
    responda -- y `links` siempre poblado. Nunca se relata el error real
    del proveedor (puede llevar tokens o URL internas)."""

    platform: CompetitorPlatform
    available: bool
    reason: str | None
    ads: tuple[CompetitorAd, ...]
    links: CompetitorLinks
    next_steps: str | None
    fallback: str | None


class MetaAdLibraryUnavailableReason(StrEnum):
    """Codigo estable que `search_competitor_ads` expone tal cual como
    `reason` (contrato con el arnes, nunca texto libre): `NOT_CONNECTED`
    cuando el negocio no tiene una cuenta de Meta ACTIVA o el bróker no
    pudo autenticar con la que tiene; `IDENTITY_CONFIRMATION_REQUIRED`
    solo para el fallo documentado y estable de Meta en `ads_archive` (400,
    `OAuthException/10` -- `MetaAdLibraryIdentityRequiredError`,
    fix/ad-library-identity-reason); `PROVIDER_ERROR` para cualquier otro
    fallo real del proveedor (Composio redacta el cuerpo de Meta antes de
    que llegue aqui, `broker/platforms/composio_transport.py` -- sin una
    senal fiable y especifica, este es el bucket honesto por defecto,
    nunca se inventa la mas especifica)."""

    NOT_CONNECTED = "meta_not_connected"
    IDENTITY_CONFIRMATION_REQUIRED = "identity_confirmation_required"
    PROVIDER_ERROR = "provider_error"


class MetaAdLibraryUnavailableError(Exception):
    """Meta no respondio (sin cuenta conectada, identidad sin confirmar, u
    otro motivo del proveedor): la presentacion traduce esto a
    `available=False` con el `reason` tipado, sin relatar el mensaje real,
    que puede llevar tokens o URL internas."""

    def __init__(self, reason: MetaAdLibraryUnavailableReason) -> None:
        super().__init__(reason.value)
        self.reason = reason


class CompetitorResearchPort(Protocol):
    """Estrecho a proposito: solo Meta pasa por aqui (razon *a*, credencial
    de la app de la empresa). El enrutado por `platform` -- Google siempre
    `available=false` sin red, Meta con enlaces siempre adjuntos -- es
    orquestacion pura y vive en `presentation/competitor_tools.py`, no
    aqui: mantiene este puerto testeable con un doble que solo sabe
    responder ads o lanzar `MetaAdLibraryUnavailableError`."""

    async def search_meta_ads(
        self,
        business_id: str,
        *,
        query: str | None,
        page_id: str | None,
        domain: str | None,
        country: str,
        active_only: bool,
    ) -> tuple[CompetitorAd, ...]: ...

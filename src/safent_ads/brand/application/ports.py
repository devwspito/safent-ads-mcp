"""Puerto de persistencia de `brand` (plan.md §5: puertos declarados en
`application`, adaptadores en `infrastructure`). Un `BrandKit` por negocio:
`save` es upsert (mismo patron que recargar `config/brand/<business>.yaml`
sin duplicar filas)."""

from __future__ import annotations

from typing import Protocol

from safent_ads.brand.domain.brand_asset import AssetKind
from safent_ads.brand.domain.brand_kit import BrandKit
from safent_ads.brand.domain.discovery import BrandDiscoveryDraft
from safent_ads.shared.ids import BusinessId


class BrandKitRepository(Protocol):
    async def get_by_business(self, business_id: BusinessId) -> BrandKit | None: ...

    async def save(self, brand_kit: BrandKit) -> None: ...


class BrandDiscoveryDraftRepository(Protocol):
    """Un borrador por negocio (mismo patron upsert que `BrandKitRepository`,
    0015_brand_discovery.py)."""

    async def get_by_business(self, business_id: BusinessId) -> BrandDiscoveryDraft | None: ...

    async def save(self, draft: BrandDiscoveryDraft) -> None: ...


class BrandAssetStoragePort(Protocol):
    """Almacen de bytes descubiertos/subidos para `brand` (logos, iconos,
    ficheros de fuente, JSON de paleta). Puerto propio de `brand`, no el
    `AssetStorePort` de `creative`: `brand` no puede importar `creative`
    sin invertir el grafo de dependencias (plan.md §4) -- mismo principio
    que documenta `creative/infrastructure/http_asset_fetcher.py` respecto
    a `broker/infrastructure/egress_guard.py`, repetido aqui a proposito."""

    async def put(self, payload: bytes, kind: AssetKind) -> str:
        """Devuelve la clave de almacenamiento opaca (`storage_uri`)."""
        ...

    async def get(self, key: str) -> bytes:
        """Lee los bytes guardados bajo `key` (el `storage_uri` opaco que
        ya devolvio `put`, nunca un valor tomado directamente de la
        peticion HTTP -- `GetBrandAssetPreview` solo lo obtiene resuelto
        via `BrandKitRepository`/`BrandDiscoveryDraftRepository`)."""
        ...


class WebsiteBrandDiscoveryPort(Protocol):
    """Unico punto de I/O de `IngestBrandFromWebsite`: rastrea `url` (ya
    validada por `domain.discovery.validate_discovery_url`) y devuelve un
    `BrandDiscoveryDraft` con los activos ya almacenados. El adaptador real
    (`infrastructure/website_brand_extractor.py`) es quien aplica
    threat-model.md C-11/C-12 con I/O real (DNS, robots.txt, topes de
    tamano); la aplicacion nunca ve un byte de HTTP."""

    async def discover(self, business_id: BusinessId, url: str) -> BrandDiscoveryDraft: ...


class BrandClaimsDecisionRecorder(Protocol):
    """Anota `decision_log` tras un `PUT /brand/claims` con exito
    (`UpdateBrandClaims`). Puerto estrecho a proposito -- una sola
    operacion, nunca el `DecisionLogRepository` entero de `audit` -- para
    que `brand.application` no dependa de mas superficie de `audit` de la
    que realmente usa; el adaptador real (`infrastructure`) es quien
    traduce esto a `audit.application.record_decision.RecordDecision`."""

    async def record(self, *, business_id: BusinessId, actor_email: str) -> None: ...

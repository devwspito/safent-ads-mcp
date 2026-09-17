"""Puertos de `creative` (contracts/creative-port.md, implementados
exactamente: un puerto por capacidad, ISP). Los adaptadores locales y de
respaldo cloud viven en `infrastructure/`; aqui solo las interfaces.

Ademas de los 9 puertos del contrato se anaden tres puertos de repositorio
(`CreativeBriefRepository`, `CreativeAssetRepository`, `CreativeJobRepository`)
y `ProposalGatewayPort`. El contrato no los declara porque cubre solo el
render/composicion de contenido binario; sin persistencia de agregados los
casos de uso de lectura (`list_creatives`, `get_creative`, `get_creative_job`)
y el traspaso a `proposals` (creative-port.md: "Nada se publica desde aqui")
no son implementables. `ProposalGatewayPort` es la unica puerta de salida
hacia `proposals`: `creative` nunca importa ese contexto (plan.md §4)."""

from __future__ import annotations

from collections.abc import Iterator, Sequence
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass
from datetime import datetime
from typing import Protocol

from safent_ads.creative.domain.brief import CreativeBrief
from safent_ads.creative.domain.creative_asset import CreativeAsset
from safent_ads.creative.domain.creative_job import CreativeJob, CreativeJobIdempotencyKey
from safent_ads.creative.domain.enums import JobWeight, MediaKind, Placement, RendererName
from safent_ads.creative.domain.gpu_lease import GpuLease
from safent_ads.creative.domain.identifiers import AssetId, AssetRef, BriefId, JobId
from safent_ads.creative.domain.policy import PolicyVerdict
from safent_ads.creative.domain.render_specs import (
    AudioAsset,
    BannerSpec,
    ImageSpec,
    MusicSpec,
    RenderedAsset,
    Timeline,
    VideoSpec,
    VoiceSpec,
)
from safent_ads.creative.domain.storage import StorageUri
from safent_ads.shared.ids import BusinessId, PlatformCode


class ImageRendererPort(Protocol):
    name: RendererName

    async def render(self, spec: ImageSpec) -> RenderedAsset: ...


# M-2 (revision de seguridad 0.2.22): `ImageRendererPort.render(spec)` es un
# Protocol compartido con los adaptadores DIRECTOS de `ads-broker`
# (Fal/OpenAI, `RenderImageService`) -- anadirle `business_id` a la firma
# obligaria a tocar esos adaptadores (`creative/infrastructure/**`) solo
# para que `BrokerImageRenderer` (el UNICO implementador que
# `GenerateCreativeAssets` inyecta hoy, `composition/app.py::
# _build_broker_image_renderers`) pudiera mandarlo por el socket. Mismo
# patron que `broker.application.connection_scope`: un contextvar
# explicito, fijado por el llamante (`GenerateCreativeAssets`) alrededor de
# la llamada y leido por `BrokerImageRenderer` al construir el sobre del
# socket -- ningun otro adaptador tiene que saber que existe.
_CURRENT_RENDER_BUSINESS_ID: ContextVar[str | None] = ContextVar(
    "current_render_business_id", default=None
)


def current_render_business_id() -> str | None:
    return _CURRENT_RENDER_BUSINESS_ID.get()


@contextmanager
def render_call_scope(business_id: str) -> Iterator[None]:
    token = _CURRENT_RENDER_BUSINESS_ID.set(business_id)
    try:
        yield
    finally:
        _CURRENT_RENDER_BUSINESS_ID.reset(token)


class VideoRendererPort(Protocol):
    name: RendererName

    async def render(self, spec: VideoSpec) -> RenderedAsset: ...


class VoiceRendererPort(Protocol):
    async def synthesize(self, spec: VoiceSpec) -> AudioAsset: ...


class MusicRendererPort(Protocol):
    async def compose(self, spec: MusicSpec) -> AudioAsset: ...


class BannerComposerPort(Protocol):
    async def compose(self, spec: BannerSpec) -> Sequence[RenderedAsset]: ...


class VideoComposerPort(Protocol):
    async def assemble(self, timeline: Timeline) -> RenderedAsset: ...


class PolicyCheckPort(Protocol):
    async def check(
        self, asset_ref: AssetRef, platform: PlatformCode, placement: Placement
    ) -> PolicyVerdict: ...


class GpuLeasePort(Protocol):
    async def acquire(self, weight: JobWeight, timeout_s: int) -> GpuLease: ...

    async def release(self, lease: GpuLease) -> None: ...


class AssetStorePort(Protocol):
    async def put(self, payload: bytes, media_kind: MediaKind) -> StorageUri: ...

    async def signed_preview_url(self, uri: StorageUri, ttl_s: int) -> str: ...

    async def open_preview(self, key: str, expires_at: int, signature: str) -> bytes:
        """Verifica la firma (tiempo constante) y la caducidad de una URL
        emitida por `signed_preview_url`, y devuelve los bytes ya leidos del
        almacen -- unico consumidor: `GET /api/v1/creative-previews/{key}`
        (`creative.presentation.router`). Nunca distingue el motivo del
        rechazo en el tipo de excepcion que ve la presentacion: firma
        invalida, enlace caducado y clave fuera del almacen viajan todos
        como el mismo error, para que la ruta responda siempre el mismo
        404 (rest-api.md: nunca revelar el motivo)."""
        ...


class AssetRetrievalPort(Protocol):
    """Lectura de bytes ya almacenados — p.ej. reenviar un frame clave a
    ComfyUI para image-to-video. No esta en `creative-port.md` (ese
    contrato solo cubre escritura + URL firmada; ISP: es una capacidad
    distinta, no una ampliacion de `AssetStorePort`)."""

    async def get(self, uri: StorageUri) -> bytes: ...


class AssetFetchPort(Protocol):
    """Descarga desde una URL EXTERNA ya validada por
    `domain/asset_import.validate_import_source_url` — distinta de
    `AssetRetrievalPort` (que lee de nuestro propio almacen, nunca de la
    red). Unico consumidor: `ImportCreativeAsset`
    (tool-surface.md §2.2 `import_creative_asset`, threat-model.md
    C-11/C-12)."""

    async def fetch(self, url: str) -> bytes: ...


class CreativeBriefRepository(Protocol):
    async def get(self, brief_id: BriefId) -> CreativeBrief | None: ...

    async def add(self, brief_id: BriefId, brief: CreativeBrief) -> None: ...

    async def list_for_business(
        self, business_id: BusinessId
    ) -> Sequence[tuple[BriefId, CreativeBrief]]: ...


class CreativeAssetRepository(Protocol):
    async def get(self, asset_id: AssetId) -> CreativeAsset | None: ...

    async def add(self, asset: CreativeAsset) -> None: ...

    async def update(self, asset: CreativeAsset) -> None: ...

    async def list_for_business(
        self, business_id: BusinessId, *, media_kind: MediaKind | None = None
    ) -> Sequence[CreativeAsset]: ...


class CreativeJobRepository(Protocol):
    async def get(self, job_id: JobId) -> CreativeJob | None: ...

    async def get_by_idempotency_key(
        self, key: CreativeJobIdempotencyKey
    ) -> CreativeJob | None: ...

    async def add(self, job: CreativeJob) -> None: ...

    async def update(self, job: CreativeJob) -> None: ...


@dataclass(frozen=True, slots=True, kw_only=True)
class ProposedAdCopy:
    """Copy que acompana la propuesta de publicacion (rest-api.md
    §Creatividades `POST /creatives/{asset_id}/propose-publication`).
    Deliberadamente NO es `domain.copy.AdCopy`: ese VO exige un
    `CallToAction` del catalogo cerrado del dominio, pero el contrato REST
    acepta el CTA como texto libre que el propietario escribe en el panel
    (mismo criterio que `creative-port.md`: "Nada se publica desde aqui" —
    la validacion final del CTA por plataforma la aplica `run_policy_check`
    antes, no este VO de transporte)."""

    headline: str
    primary_text: str
    cta: str


@dataclass(frozen=True, slots=True, kw_only=True)
class CreativePublicationProposal:
    """Resultado de `ProposalGatewayPort.propose_creative_publication`:
    misma forma que `POST .../propose-publication` responde (rest-api.md),
    sin importar ningun tipo de `proposals` (plan.md §4)."""

    proposal_id: str
    diff_hash: str
    expires_at: datetime


class ProposalGatewayPort(Protocol):
    """Unica salida hacia `proposals` (creative-port.md: "Nada se publica
    desde aqui"). Crea una `PropuestaDeAccion` en `pendiente`; nunca toca
    una plataforma. `creative` no importa `proposals` para no invertir el
    grafo de dependencias (plan.md §4); `composition` cablea el adaptador
    real."""

    async def propose_creative_publication(
        self,
        *,
        asset_id: AssetId,
        business_id: BusinessId,
        ad_set_ref: str,
        ad_copy: ProposedAdCopy,
        extra_asset_ids: Sequence[AssetId],
    ) -> CreativePublicationProposal: ...

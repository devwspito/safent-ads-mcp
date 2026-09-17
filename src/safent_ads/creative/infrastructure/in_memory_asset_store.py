"""`InMemoryAssetStore`: implementa `AssetStorePort` sin tocar disco --
unico consumidor: `ads-broker` (`broker/application/render_image.py`,
threat-model.md C-29 "claves cloud en el broker"). `FalImageRenderer`/
`OpenAiImageRenderer` exigen un `AssetStorePort` para escribir el resultado
(`ImageRendererPort.render` no devuelve bytes, devuelve `RenderedAsset.
storage_uri`); el broker nunca puede escribir en el directorio de activos
de `ads-api` (procesos distintos, sin disco compartido por contrato), asi
que este almacen guarda los bytes en un `dict` en memoria del propio
proceso y `pop` los entrega una unica vez -- `render_image` responde con
ellos por el socket y los descarta, nunca se acumulan entre peticiones."""

from __future__ import annotations

import uuid

from safent_ads.creative.domain.enums import MediaKind
from safent_ads.creative.domain.storage import StorageUri
from safent_ads.shared.errors import InfrastructureError


class PreviewNotSupportedByBrokerStoreError(InfrastructureError):
    """El broker nunca sirve una previsualizacion HTTP (esa superficie vive
    en `ads-api` sobre `LocalAssetStorage`); estos dos metodos solo existen
    para cumplir la forma estructural de `AssetStorePort`."""


class UnknownStorageUriError(InfrastructureError):
    """`pop` pidio una clave que este almacen nunca guardo (ya se entrego
    antes, o pertenece a otro proceso) -- fail loud, nunca bytes vacios."""


class InMemoryAssetStore:
    def __init__(self) -> None:
        self._payloads: dict[str, bytes] = {}

    async def put(self, payload: bytes, media_kind: MediaKind) -> StorageUri:
        key = f"{media_kind.value}/{uuid.uuid4().hex}"
        self._payloads[key] = payload
        return StorageUri(key)

    def pop(self, uri: StorageUri) -> bytes:
        """Entrega los bytes guardados por `put` y los descarta -- unico
        punto de lectura, para que este almacen nunca retenga un activo mas
        alla de la peticion `render_image` que lo produjo."""
        try:
            return self._payloads.pop(uri.key)
        except KeyError as exc:
            raise UnknownStorageUriError(uri.key) from exc

    async def signed_preview_url(self, uri: StorageUri, ttl_s: int) -> str:  # noqa: ARG002
        raise PreviewNotSupportedByBrokerStoreError(uri.key)

    async def open_preview(
        self, key: str, expires_at: int, signature: str  # noqa: ARG002
    ) -> bytes:
        raise PreviewNotSupportedByBrokerStoreError(key)

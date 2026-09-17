"""`HttpAssetFetcher` implementa `AssetFetchPort`: descarga el activo que
una herramienta nativa del agente produjo, para `ImportCreativeAsset`
(threat-model.md C-11/C-12). El llamador YA valido host+esquema con
`domain/asset_import.validate_import_source_url` antes de construir la
peticion; este adaptador anade la segunda capa de defensa que exige I/O
real, sobre `shared/net/safe_egress.py` (F-3/F-4): resolver el DNS,
rechazar si cae en un rango bloqueado y fijar la conexion a esa misma IP
ya validada (mismo principio que `broker/infrastructure/egress_guard.py`,
repetido aqui a proposito — `creative` no puede importar `broker`,
plan.md §4) y cortar la descarga en cuanto el cuerpo supera el tope de
tamano, sin esperar a que termine (streaming, no `len(response.content)`
al final)."""

from __future__ import annotations

import httpx

from safent_ads.shared.errors import InfrastructureError
from safent_ads.shared.net.safe_egress import (
    BlockedEgressAddressError,
    Resolver,
    default_resolver,
    open_pinned_stream,
)

_MAX_IMPORT_BYTES = 200 * 1024 * 1024  # 200 MiB, mismo tope que LocalAssetStorage


class AssetFetchDeniedError(InfrastructureError):
    """El host resuelve a un rango bloqueado, o el servidor devolvio un
    error HTTP."""


class AssetFetchTooLargeError(InfrastructureError):
    """El cuerpo de la respuesta supera `_MAX_IMPORT_BYTES` — la descarga
    se corta en cuanto se detecta, no se deja terminar."""


class HttpAssetFetcher:
    def __init__(
        self,
        http_client: httpx.AsyncClient,
        *,
        resolver: Resolver | None = None,
        max_bytes: int = _MAX_IMPORT_BYTES,
        timeout_s: float = 30.0,
    ) -> None:
        self._http_client = http_client
        self._resolver = resolver or default_resolver
        self._max_bytes = max_bytes
        self._timeout_s = timeout_s

    async def fetch(self, url: str) -> bytes:
        response = await self._open_stream(url)
        try:
            response.raise_for_status()
            return await self._read_within_limit(response)
        finally:
            await response.aclose()

    async def _open_stream(self, url: str) -> httpx.Response:
        try:
            return await open_pinned_stream(
                self._http_client, "GET", url, resolver=self._resolver, timeout=self._timeout_s
            )
        except BlockedEgressAddressError as exc:
            raise AssetFetchDeniedError(str(exc)) from exc

    async def _read_within_limit(self, response: httpx.Response) -> bytes:
        chunks: list[bytes] = []
        total = 0
        async for chunk in response.aiter_bytes():
            total += len(chunk)
            if total > self._max_bytes:
                raise AssetFetchTooLargeError(f"{total} bytes > {self._max_bytes}")
            chunks.append(chunk)
        return b"".join(chunks)

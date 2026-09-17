"""`HttpAssetFetcher` contra un transporte HTTP falso (`httpx.MockTransport`,
sin red real): bloqueo de rangos privados/loopback tras resolucion DNS
(inyectada, determinista) y corte en cuanto el cuerpo supera el tope de
tamano."""

from __future__ import annotations

import asyncio

import httpx
import pytest

from safent_ads.creative.infrastructure.http_asset_fetcher import (
    AssetFetchDeniedError,
    AssetFetchTooLargeError,
    HttpAssetFetcher,
)


def _resolver_returning(*addresses: str):
    async def _resolve(hostname: str) -> list[str]:
        return list(addresses)

    return _resolve


def test_fetch_returns_body_when_host_resolves_to_public_ip() -> None:
    async def _run() -> bytes:
        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(200, content=b"asset-bytes")

        fetcher = HttpAssetFetcher(
            httpx.AsyncClient(transport=httpx.MockTransport(handler)),
            resolver=_resolver_returning("93.184.216.34"),
        )
        return await fetcher.fetch("https://v3.fal.media/files/out.png")

    assert asyncio.run(_run()) == b"asset-bytes"


def test_fetch_denies_loopback_resolution() -> None:
    async def _run() -> None:
        def handler(request: httpx.Request) -> httpx.Response:
            raise AssertionError("no deberia llegar a hacer la peticion HTTP")

        fetcher = HttpAssetFetcher(
            httpx.AsyncClient(transport=httpx.MockTransport(handler)),
            resolver=_resolver_returning("127.0.0.1"),
        )
        await fetcher.fetch("https://v3.fal.media/files/out.png")

    with pytest.raises(AssetFetchDeniedError, match="rango bloqueado"):
        asyncio.run(_run())


def test_fetch_denies_link_local_metadata_ip() -> None:
    async def _run() -> None:
        def handler(request: httpx.Request) -> httpx.Response:
            raise AssertionError("no deberia llegar a hacer la peticion HTTP")

        fetcher = HttpAssetFetcher(
            httpx.AsyncClient(transport=httpx.MockTransport(handler)),
            resolver=_resolver_returning("169.254.169.254"),
        )
        await fetcher.fetch("https://v3.fal.media/files/out.png")

    with pytest.raises(AssetFetchDeniedError, match="rango bloqueado"):
        asyncio.run(_run())


def test_fetch_denies_rfc1918_private_ip() -> None:
    async def _run() -> None:
        def handler(request: httpx.Request) -> httpx.Response:
            raise AssertionError("no deberia llegar a hacer la peticion HTTP")

        fetcher = HttpAssetFetcher(
            httpx.AsyncClient(transport=httpx.MockTransport(handler)),
            resolver=_resolver_returning("10.0.0.5"),
        )
        await fetcher.fetch("https://v3.fal.media/files/out.png")

    with pytest.raises(AssetFetchDeniedError, match="rango bloqueado"):
        asyncio.run(_run())


def test_fetch_raises_when_body_exceeds_max_bytes() -> None:
    async def _run() -> None:
        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(200, content=b"x" * 100)

        fetcher = HttpAssetFetcher(
            httpx.AsyncClient(transport=httpx.MockTransport(handler)),
            resolver=_resolver_returning("93.184.216.34"),
            max_bytes=50,
        )
        await fetcher.fetch("https://v3.fal.media/files/out.png")

    with pytest.raises(AssetFetchTooLargeError):
        asyncio.run(_run())


def test_fetch_propagates_http_error_status() -> None:
    async def _run() -> None:
        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(404)

        fetcher = HttpAssetFetcher(
            httpx.AsyncClient(transport=httpx.MockTransport(handler)),
            resolver=_resolver_returning("93.184.216.34"),
        )
        await fetcher.fetch("https://v3.fal.media/files/missing.png")

    with pytest.raises(httpx.HTTPStatusError):
        asyncio.run(_run())


def test_fetch_pins_the_connection_to_the_validated_resolution() -> None:
    """F-3: la peticion que llega al transporte debe ir dirigida a la
    MISMA IP que el resolver acaba de validar, no a un segundo `connect()`
    que `httpx`/`httpcore` resolverian por su cuenta (DNS rebinding,
    CWE-367)."""

    async def _run() -> str | None:
        seen_host: str | None = None

        def handler(request: httpx.Request) -> httpx.Response:
            nonlocal seen_host
            seen_host = request.url.host
            return httpx.Response(200, content=b"asset-bytes")

        fetcher = HttpAssetFetcher(
            httpx.AsyncClient(transport=httpx.MockTransport(handler)),
            resolver=_resolver_returning("93.184.216.34"),
        )
        await fetcher.fetch("https://v3.fal.media/files/out.png")
        return seen_host

    assert asyncio.run(_run()) == "93.184.216.34"

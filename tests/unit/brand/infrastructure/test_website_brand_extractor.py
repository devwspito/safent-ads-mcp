"""`WebsiteBrandExtractor` contra un transporte HTTP falso
(`httpx.MockTransport`, sin red real): bloqueo de rangos privados/loopback
tras resolucion DNS (inyectada, deterministica), redirecciones
re-validadas salto a salto, `robots.txt` y corte por tamano -- mismo
patron que `tests/unit/creative/infrastructure/test_http_asset_fetcher.py`
(threat-model.md C-11/C-12). Cualquier recurso que NO sea la pagina de
inicio es mejor esfuerzo: un fallo lo descarta sin abortar el rastreo."""

from __future__ import annotations

import asyncio
import hashlib
import io
from datetime import UTC, datetime

import httpx
import pytest
from PIL import Image

from safent_ads.brand.domain.discovery import DiscoverySource
from safent_ads.brand.infrastructure.website_brand_extractor import (
    WebsiteBrandExtractor,
    WebsiteFetchDeniedError,
    WebsiteFetchFailedError,
    _dominant_color,
)
from safent_ads.brand.testing.in_memory_brand_asset_storage import InMemoryBrandAssetStorage
from safent_ads.shared.clock import FixedClock
from safent_ads.shared.ids import BusinessId
from safent_ads.shared.image_limits import MAX_IMAGE_PIXELS as _MAX_IMAGE_PIXELS

_NOW = datetime(2026, 9, 9, 12, 0, tzinfo=UTC)
_PNG_BYTES = b"\x89PNG\r\n\x1a\n" + b"\x00" * 32

_HOME_HTML = """
<html><head>
<title>Example Business</title>
<meta property="og:site_name" content="Example Business">
<link rel="icon" href="/favicon.ico">
</head><body>
<h1>Prepara tu lanzamiento</h1>
<a href="/contacto">Contacto</a>
</body></html>
"""

_CONTACT_HTML = "<html><body><h1>Contacto</h1><form><input name=e></form></body></html>"


def _resolver_returning(mapping: dict[str, str]):
    async def _resolve(hostname: str) -> list[str]:
        return [mapping.get(hostname, "93.184.216.34")]

    return _resolve


def _handler_for(responses: dict[str, httpx.Response]):
    def _handle(request: httpx.Request) -> httpx.Response:
        # F-3: la peticion real va dirigida a la IP fijada
        # (`safe_egress.pin_request`), no al host logico -- enrutar por
        # `original_url` (mismo valor que antes llevaba `request.url`)
        # mantiene estos dobles centrados en la URL que el test declara,
        # sin acoplarse a que IP concreta resuelva cada host.
        key = str(request.extensions.get("original_url", request.url))
        if key in responses:
            return responses[key]
        return httpx.Response(404)

    return _handle


def _extractor(
    handler,
    *,
    resolver,
    max_page_bytes: int = 2 * 1024 * 1024,
    max_asset_bytes: int = 10 * 1024 * 1024,
) -> tuple[WebsiteBrandExtractor, InMemoryBrandAssetStorage]:
    storage = InMemoryBrandAssetStorage()
    extractor = WebsiteBrandExtractor(
        httpx.AsyncClient(transport=httpx.MockTransport(handler)),
        storage,
        FixedClock(_NOW),
        resolver=resolver,
        max_page_bytes=max_page_bytes,
        max_asset_bytes=max_asset_bytes,
    )
    return extractor, storage


async def test_discover_builds_draft_from_homepage_and_linked_contact_page() -> None:
    responses = {
        "https://example-business.test/": httpx.Response(200, content=_HOME_HTML.encode()),
        "https://example-business.test/robots.txt": httpx.Response(404),
        "https://example-business.test/favicon.ico": httpx.Response(200, content=_PNG_BYTES),
        "https://example-business.test/contacto": httpx.Response(
            200, content=_CONTACT_HTML.encode()
        ),
    }
    extractor, storage = _extractor(_handler_for(responses), resolver=_resolver_returning({}))

    draft = await extractor.discover(BusinessId.new(), "https://example-business.test/")

    assert draft.source_url == "https://example-business.test/"
    assert draft.business_name_candidates[0].name == "Example Business"
    assert len(draft.logo_candidates) == 1
    assert draft.logo_candidates[0].sha256 == hashlib.sha256(_PNG_BYTES).hexdigest()
    assert storage.stored  # el logo se almaceno de verdad
    assert any(c.kind.value == "contact_form" for c in draft.contact_channels)


async def test_discover_denies_homepage_host_resolving_to_private_ip() -> None:
    extractor, _storage = _extractor(
        _handler_for({}), resolver=_resolver_returning({"example-business.test": "10.0.0.5"})
    )

    with pytest.raises(WebsiteFetchDeniedError, match="rango bloqueado"):
        await extractor.discover(BusinessId.new(), "https://example-business.test/")


async def test_rejects_when_peer_address_is_blocked_even_if_dns_said_public() -> None:
    """F-3 (CWE-367): el chequeo previo de `discover()` resuelve una IP
    publica, pero la resolucion que de verdad fija la conexion
    (`pin_request` -- la MISMA llamada que valida y conecta, sin una
    segunda resolucion de por medio) ve una IP bloqueada en su lugar --
    exactamente lo que pasaria con un DNS de TTL 0 que cambia de
    respuesta entre el primer vistazo y el `connect()` real. Nunca debe
    colarse ni un byte del cuerpo, y el intento de conectar con la dana
    fija se envuelve como fallo de descarga de la pagina de inicio (mismo
    mapeo que cualquier otro `WebsiteFetchDeniedError` sobre un recurso
    que no es la portada)."""
    call_count = 0

    async def _rebinding_resolver(hostname: str) -> list[str]:  # noqa: ARG001
        nonlocal call_count
        call_count += 1
        return ["93.184.216.34"] if call_count == 1 else ["169.254.169.254"]

    extractor, storage = _extractor(_handler_for({}), resolver=_rebinding_resolver)

    with pytest.raises(WebsiteFetchFailedError, match="rango bloqueado"):
        await extractor.discover(BusinessId.new(), "https://example-business.test/")

    assert call_count >= 2
    assert not storage.stored


async def test_discover_fails_when_robots_txt_disallows_the_homepage() -> None:
    robots_txt = "User-agent: *\nDisallow: /\n"
    responses = {
        "https://example-business.test/": httpx.Response(200, content=_HOME_HTML.encode()),
        "https://example-business.test/robots.txt": httpx.Response(
            200, content=robots_txt.encode()
        ),
    }
    extractor, _storage = _extractor(_handler_for(responses), resolver=_resolver_returning({}))

    with pytest.raises(WebsiteFetchFailedError, match="robots.txt"):
        await extractor.discover(BusinessId.new(), "https://example-business.test/")


async def test_discover_fails_when_homepage_exceeds_max_page_bytes() -> None:
    oversized = _HOME_HTML.encode() + b"x" * 100
    responses = {
        "https://example-business.test/": httpx.Response(200, content=oversized),
        "https://example-business.test/robots.txt": httpx.Response(404),
    }
    extractor, _storage = _extractor(
        _handler_for(responses), resolver=_resolver_returning({}), max_page_bytes=50
    )

    with pytest.raises(WebsiteFetchFailedError):
        await extractor.discover(BusinessId.new(), "https://example-business.test/")


async def test_discover_drops_a_sub_resource_redirected_to_a_blocked_ip_without_aborting() -> None:
    responses = {
        "https://example-business.test/": httpx.Response(200, content=_HOME_HTML.encode()),
        "https://example-business.test/robots.txt": httpx.Response(404),
        "https://example-business.test/favicon.ico": httpx.Response(
            302, headers={"location": "https://internal.example-business.test/secret.png"}
        ),
    }
    resolver = _resolver_returning({"internal.example-business.test": "10.0.0.5"})
    extractor, storage = _extractor(_handler_for(responses), resolver=resolver)

    draft = await extractor.discover(BusinessId.new(), "https://example-business.test/")

    assert draft.logo_candidates == ()
    assert not storage.stored


def _decompression_bomb_png() -> bytes:
    """PNG genuino cuyo `IHDR` declara mas pixeles que `_MAX_IMAGE_PIXELS`
    (F-5): un color solido comprime a unos pocos cientos de bytes pese al
    tamano declarado, asi que el propio tope de descarga (`max_asset_bytes`)
    nunca lo cortaria antes de llegar a Pillow."""
    side = 5000  # 25_000_000 pixeles > _MAX_IMAGE_PIXELS (16_000_000)
    buffer = io.BytesIO()
    Image.new("RGB", (side, side), color=(10, 20, 30)).save(buffer, format="PNG")
    return buffer.getvalue()


async def test_dominant_color_rejects_decompression_bomb_without_raising() -> None:
    """F-5: `Image.DecompressionBombError` hereda de `Exception`, no de
    `OSError`/`ValueError` -- el `except` original no lo atrapaba y
    escapaba de `discover()` como 500. Debe devolver `None`, nunca
    lanzar, y sin bloquear el loop de eventos (`asyncio.to_thread`)."""
    payload = _decompression_bomb_png()
    assert len(payload) * 4 < _MAX_IMAGE_PIXELS  # el payload en si es minusculo

    result = await _dominant_color(payload, DiscoverySource.DOMINANT_COLOR_LOGO)

    assert result is None


async def test_discover_caps_logo_hints_and_total_bytes() -> None:
    """F-2 (CWE-770): presupuesto GLOBAL de descarga de logos, no solo un
    tope por hint -- 5 candidatos de ~7 MiB cada uno (por debajo del tope
    individual de 10 MiB) deben cortarse antes de sumar los ~35 MiB
    totales, respetando `_MAX_TOTAL_LOGO_BYTES` (24 MiB)."""
    image_bytes = b"\x89PNG\r\n\x1a\n" + b"\x00" * (7 * 1024 * 1024 - 8)
    home_html = "<html><body>" + "".join(
        f'<img src="/logo-{i}.png" alt="logo {i}">' for i in range(5)
    ) + "</body></html>"
    responses = {
        "https://example-business.test/": httpx.Response(200, content=home_html.encode()),
        "https://example-business.test/robots.txt": httpx.Response(404),
        **{
            f"https://example-business.test/logo-{i}.png": httpx.Response(
                200, content=image_bytes
            )
            for i in range(5)
        },
    }
    extractor, storage = _extractor(_handler_for(responses), resolver=_resolver_returning({}))

    draft = await extractor.discover(BusinessId.new(), "https://example-business.test/")

    assert len(draft.logo_candidates) <= 3  # 24 MiB / ~7 MiB por logo
    assert len(storage.stored) == len(draft.logo_candidates)


async def test_discover_times_out_the_entire_crawl() -> None:
    """F-2 (CWE-770): fecha limite del rastreo COMPLETO, no solo por
    peticion individual -- un sitio que responde muy despacio en cada
    recurso, pero siempre dentro de `timeout_s`, no debe poder mantener
    `discover()` vivo indefinidamente."""

    async def _slow_resolver(hostname: str) -> list[str]:  # noqa: ARG001
        await asyncio.sleep(1)
        return ["93.184.216.34"]

    extractor, _storage = _extractor(_handler_for({}), resolver=_slow_resolver)
    extractor._crawl_timeout_s = 0.01  # noqa: SLF001 - mas corto que cualquier timeout real

    with pytest.raises(WebsiteFetchFailedError, match="agoto el tiempo"):
        await extractor.discover(BusinessId.new(), "https://example-business.test/")


async def test_discover_drops_a_malicious_svg_logo_without_storing_it() -> None:
    """F-6 (CWE-79 latente): un `<img>` "logo" que en realidad sirve un
    SVG con `<script>` nunca debe llegar a `asset_storage.put` -- se
    descarta como cualquier otro candidato de mejor esfuerzo, sin abortar
    el resto del rastreo."""
    malicious_svg = b'<svg xmlns="http://www.w3.org/2000/svg"><script>alert(1)</script></svg>'
    responses = {
        "https://example-business.test/": httpx.Response(200, content=_HOME_HTML.encode()),
        "https://example-business.test/robots.txt": httpx.Response(404),
        "https://example-business.test/favicon.ico": httpx.Response(200, content=malicious_svg),
    }
    extractor, storage = _extractor(_handler_for(responses), resolver=_resolver_returning({}))

    draft = await extractor.discover(BusinessId.new(), "https://example-business.test/")

    assert draft.logo_candidates == ()
    assert not storage.stored

"""`WebsiteBrandExtractor` implementa `WebsiteBrandDiscoveryPort`: unico
punto de I/O de `IngestBrandFromWebsite` (owner request: "el usuario ...
pone el enlace y que se rastree desde la web"). Orquesta el rastreo con
`html_brand_parser.py` (puro) y aplica, para CADA URL que sigue -- pagina
de inicio, paginas enlazadas, hojas de estilo, manifest, imagenes -- las
mismas defensas SSRF de `shared/net/safe_egress.py` (F-3/F-4) que
`creative.infrastructure.http_asset_fetcher.HttpAssetFetcher`: sin
allow-list de host (el propietario apunta a cualquier sitio publico) pero
SI bloqueo de IP privada/loopback/link-local tras resolucion DNS, la
conexion fijada a esa misma IP validada, redirecciones re-validadas salto
a salto, corte de tamano en streaming y respeto de `robots.txt`.

Cada URL que NO es la pagina de inicio (enlaces internos, hojas de estilo,
manifest, imagenes -- todo contenido que un atacante controla si es dueno
del sitio rastreado) se trata en "mejor esfuerzo": un fallo (bloqueado,
404, tamano excedido) descarta ESE candidato, nunca aborta el rastreo
completo. Solo el fallo de la pagina de inicio hace fallar
`IngestBrandFromWebsite` entero."""

from __future__ import annotations

import asyncio
import hashlib
import io
from urllib.parse import urljoin
from urllib.robotparser import RobotFileParser

import httpx
from PIL import Image, UnidentifiedImageError
from playwright.async_api import Error as PlaywrightError
from playwright.async_api import TimeoutError as PlaywrightTimeoutError
from playwright.async_api import ViewportSize, async_playwright
from ulid import ULID

from safent_ads.brand.application.ports import BrandAssetStoragePort
from safent_ads.brand.domain.brand_asset import AssetKind
from safent_ads.brand.domain.discovery import (
    BrandDiscoveryDraft,
    ColorCandidate,
    ContactChannelCandidate,
    CopySample,
    DiscoverySource,
    LogoCandidate,
    validate_discovery_url,
)
from safent_ads.brand.domain.errors import InvalidDiscoveryUrlError
from safent_ads.brand.infrastructure.html_brand_parser import (
    LogoHint,
    dedupe_and_cap_logo_hints,
    extract_business_name_candidates,
    extract_contact_channels,
    extract_copy_samples,
    extract_css_custom_property_colors,
    extract_css_font_family_candidates,
    extract_css_most_used_colors,
    extract_inline_style_text,
    extract_linked_page_urls,
    extract_logo_hints,
    extract_manifest_icon_hints,
    extract_manifest_url,
    extract_social_links,
    extract_stylesheet_urls,
    extract_web_font_link_candidates,
)
from safent_ads.brand.infrastructure.svg_sanitizer import sanitize_remote_svg
from safent_ads.shared.clock import Clock
from safent_ads.shared.errors import InfrastructureError
from safent_ads.shared.ids import BusinessId
from safent_ads.shared.image_limits import MAX_IMAGE_PIXELS
from safent_ads.shared.net.safe_egress import (
    BlockedEgressAddressError,
    Resolver,
    default_resolver,
    open_pinned_stream,
    resolve_pinned_ip,
)

_USER_AGENT = "SafentAdsBrandBot/1.0 (+https://safentads.example/brand-bot)"
_REQUEST_HEADERS = {"User-Agent": _USER_AGENT}
_MAX_REDIRECTS = 3
_MAX_PAGE_BYTES = 2 * 1024 * 1024  # 2 MiB, una pagina HTML razonable
_MAX_ASSET_DOWNLOAD_BYTES = 10 * 1024 * 1024  # mismo tope que LocalBrandAssetStorage
_MAX_ROBOTS_BYTES = 64 * 1024
_MAX_MANIFEST_BYTES = 256 * 1024
_MAX_TOTAL_COPY_SAMPLES = 10
_MAX_TOTAL_LOGO_BYTES = 24 * 1024 * 1024  # F-2: presupuesto global, no solo por-hint
_MAX_CRAWL_SECONDS = 30  # F-2: fecha limite del rastreo entero, no solo por peticion
_DOMINANT_COLOR_SOURCES = frozenset({DiscoverySource.OG_IMAGE, DiscoverySource.IMG_LOGO_HINT})
_PLAYWRIGHT_LAUNCH_ARGS = ("--disable-gpu", "--no-sandbox", "--disable-dev-shm-usage")
_SCREENSHOT_VIEWPORT: ViewportSize = {"width": 1280, "height": 800}
_DOMINANT_COLOR_SAMPLE_SIZE = (32, 32)

# F-5/Bj-1: `Image.MAX_IMAGE_PIXELS` ya queda fijado por
# `shared/image_limits.py` (unica fuente de verdad, importada arriba) --
# sin esto, Pillow solo emite un `DecompressionBombWarning` hasta el doble
# del tope, y `DecompressionBombError` hereda de `Exception`, no de
# `OSError`/`ValueError`, asi que el `except` de abajo no lo atrapaba y
# escapaba de `discover()` como 500.


class WebsiteFetchDeniedError(InfrastructureError):
    """El host resuelve a un rango bloqueado."""


class WebsiteFetchTooLargeError(InfrastructureError):
    """El cuerpo de la respuesta supera el tope de tamano -- cortado en
    streaming, nunca se deja terminar."""


class WebsiteFetchFailedError(InfrastructureError):
    """La pagina de inicio no pudo descargarse: sin ella no hay nada que
    rastrear (a diferencia de cualquier otro recurso, que es mejor
    esfuerzo)."""


def _guess_asset_kind(source: DiscoverySource, payload: bytes) -> AssetKind:
    head = payload.lstrip()[:16].lower()
    if head.startswith((b"<?xml", b"<svg")):
        return AssetKind.LOGO_VECTOR
    icon_sources = (
        DiscoverySource.FAVICON,
        DiscoverySource.APPLE_TOUCH_ICON,
        DiscoverySource.MANIFEST_ICON,
    )
    return AssetKind.ICON if source in icon_sources else AssetKind.LOGO_RASTER


_RGB_CHANNELS = 3


def _decode_dominant_color_sync(payload: bytes, source: DiscoverySource) -> ColorCandidate | None:
    """Trabajo de Pillow real (F-5): CPU-bound, se ejecuta en un hilo
    aparte via `asyncio.to_thread` (`_dominant_color`) para no bloquear
    el loop de eventos mientras decodifica una imagen no confiable."""
    try:
        with Image.open(io.BytesIO(payload)) as image:
            if image.width * image.height > MAX_IMAGE_PIXELS:
                return None
            rgb_image = image.convert("RGB").resize(_DOMINANT_COLOR_SAMPLE_SIZE)
            colors = rgb_image.getcolors(rgb_image.width * rgb_image.height)
    except (UnidentifiedImageError, OSError, ValueError, Image.DecompressionBombError):
        return None
    if not colors:
        return None
    _, dominant = max(colors, key=lambda c: c[0])
    # Pillow 12 tipa `getcolors` como `float | tuple[int, ...]` (modo L vs
    # RGB); tras `convert("RGB")` siempre es una terna, pero se comprueba en
    # vez de asumirlo -- una imagen rara devuelve None, nunca un 500.
    if not isinstance(dominant, tuple) or len(dominant) < _RGB_CHANNELS:
        return None
    red, green, blue = (int(dominant[0]), int(dominant[1]), int(dominant[2]))
    return ColorCandidate(hex=f"#{red:02X}{green:02X}{blue:02X}", source=source, confidence=0.5)


async def _dominant_color(payload: bytes, source: DiscoverySource) -> ColorCandidate | None:
    return await asyncio.to_thread(_decode_dominant_color_sync, payload, source)


class WebsiteBrandExtractor:
    def __init__(
        self,
        http_client: httpx.AsyncClient,
        asset_storage: BrandAssetStoragePort,
        clock: Clock,
        *,
        resolver: Resolver | None = None,
        max_page_bytes: int = _MAX_PAGE_BYTES,
        max_asset_bytes: int = _MAX_ASSET_DOWNLOAD_BYTES,
        timeout_s: float = 10.0,
        crawl_timeout_s: float = _MAX_CRAWL_SECONDS,
        enable_playwright_pass: bool = False,
    ) -> None:
        self._http_client = http_client
        self._asset_storage = asset_storage
        self._clock = clock
        self._resolver = resolver or default_resolver
        self._max_page_bytes = max_page_bytes
        self._max_asset_bytes = max_asset_bytes
        self._timeout_s = timeout_s
        self._crawl_timeout_s = crawl_timeout_s
        self._enable_playwright_pass = enable_playwright_pass

    async def discover(self, business_id: BusinessId, url: str) -> BrandDiscoveryDraft:
        """Fecha limite del rastreo ENTERO (F-2, CWE-770): sin esto, un
        sitio con muchas paginas/hojas de estilo/imagenes lentas (o
        simplemente muchas, incluso dentro de los topes por-recurso)
        podia mantener la peticion viva indefinidamente."""
        try:
            async with asyncio.timeout(self._crawl_timeout_s):
                return await self._discover(business_id, url)
        except TimeoutError as exc:
            raise WebsiteFetchFailedError(f"el rastreo de {url!r} agoto el tiempo") from exc

    async def _discover(self, business_id: BusinessId, url: str) -> BrandDiscoveryDraft:
        host = validate_discovery_url(url)
        await self._assert_host_resolves_safely(host)
        robots = await self._load_robots(url)
        homepage_html = await self._fetch_page_or_raise(url, robots)

        pages = [(url, homepage_html), *await self._fetch_linked_pages(url, homepage_html, robots)]
        logos, logo_colors = await self._collect_logo_candidates(url, homepage_html, robots)
        css_text = await self._collect_css_text(url, homepage_html, robots)
        screenshot_colors = await self._maybe_screenshot_colors(url)

        return BrandDiscoveryDraft(
            business_id=business_id,
            source_url=url,
            discovered_at=self._clock.now(),
            logo_candidates=tuple(logos),
            color_candidates=(
                *extract_css_custom_property_colors(css_text),
                *extract_css_most_used_colors(css_text),
                *logo_colors,
                *screenshot_colors,
            ),
            typography_candidates=(
                *extract_css_font_family_candidates(css_text),
                *extract_web_font_link_candidates(homepage_html),
            ),
            business_name_candidates=tuple(extract_business_name_candidates(homepage_html)),
            social_links=tuple(extract_social_links(homepage_html)),
            contact_channels=tuple(_collect_contact_channels(pages)),
            copy_samples=tuple(_collect_copy_samples(pages)),
        )

    async def _fetch_linked_pages(
        self, base_url: str, homepage_html: str, robots: RobotFileParser
    ) -> list[tuple[str, str]]:
        pages: list[tuple[str, str]] = []
        for linked_url in extract_linked_page_urls(base_url, homepage_html):
            html = await self._fetch_page_best_effort(linked_url, robots)
            if html is not None:
                pages.append((linked_url, html))
        return pages

    async def _collect_css_text(
        self, base_url: str, homepage_html: str, robots: RobotFileParser
    ) -> str:
        parts = [extract_inline_style_text(homepage_html)]
        for sheet_url in extract_stylesheet_urls(base_url, homepage_html):
            body = await self._fetch_asset_best_effort(
                sheet_url, robots, max_bytes=self._max_page_bytes
            )
            if body is not None:
                parts.append(body.decode("utf-8", errors="ignore"))
        return "\n".join(parts)

    async def _collect_logo_candidates(
        self, base_url: str, homepage_html: str, robots: RobotFileParser
    ) -> tuple[list[LogoCandidate], list[ColorCandidate]]:
        """Presupuesto global de bytes ademas del tope por hint (F-2,
        CWE-770): `_MAX_TOTAL_LOGO_BYTES` entre TODOS los candidatos, no
        solo `_MAX_ASSET_DOWNLOAD_BYTES` por uno -- sin esto, 10 hints de
        10 MiB cada uno (tope individual) siguen siendo 100 MiB por
        rastreo. Se detiene en cuanto se agota, best-effort como el resto
        de recursos que no son la portada."""
        hints = await self._logo_hints_including_manifest(base_url, homepage_html, robots)
        logos: list[LogoCandidate] = []
        colors: list[ColorCandidate] = []
        remaining_budget = _MAX_TOTAL_LOGO_BYTES
        for hint in hints:
            if remaining_budget <= 0:
                break
            stored = await self._store_logo_hint(
                hint, robots, max_bytes=min(self._max_asset_bytes, remaining_budget)
            )
            if stored is None:
                continue
            logo, color, downloaded_bytes = stored
            logos.append(logo)
            if color is not None:
                colors.append(color)
            remaining_budget -= downloaded_bytes
        return logos, colors

    async def _logo_hints_including_manifest(
        self, base_url: str, homepage_html: str, robots: RobotFileParser
    ) -> list[LogoHint]:
        hints = extract_logo_hints(base_url, homepage_html)
        manifest_url = extract_manifest_url(base_url, homepage_html)
        if manifest_url is None:
            return hints
        body = await self._fetch_asset_best_effort(
            manifest_url, robots, max_bytes=_MAX_MANIFEST_BYTES
        )
        if body is not None:
            hints.extend(
                extract_manifest_icon_hints(base_url, body.decode("utf-8", errors="ignore"))
            )
        # F-2: el manifest puede anadir sus propios iconos -- el tope de
        # 10 y la deduplicacion por URL se re-aplican aqui sobre la suma,
        # no solo dentro de `extract_logo_hints`.
        return dedupe_and_cap_logo_hints(hints)

    async def _store_logo_hint(
        self, hint: LogoHint, robots: RobotFileParser, *, max_bytes: int
    ) -> tuple[LogoCandidate, ColorCandidate | None, int] | None:
        payload = await self._fetch_asset_best_effort(hint.url, robots, max_bytes=max_bytes)
        if payload is None:
            return None
        kind = _guess_asset_kind(hint.source, payload)
        if kind == AssetKind.LOGO_VECTOR:
            sanitized = sanitize_remote_svg(payload)
            if sanitized is None:
                return None
            payload = sanitized
        try:
            storage_uri = await self._asset_storage.put(payload, kind)
        except InfrastructureError:
            return None
        logo = LogoCandidate(
            asset_id=str(ULID()),
            kind=kind,
            storage_uri=storage_uri,
            sha256=hashlib.sha256(payload).hexdigest(),
            source=hint.source,
            confidence=hint.confidence,
        )
        wants_dominant_color = (
            hint.source in _DOMINANT_COLOR_SOURCES and kind != AssetKind.LOGO_VECTOR
        )
        dominant = (
            await _dominant_color(payload, DiscoverySource.DOMINANT_COLOR_LOGO)
            if wants_dominant_color
            else None
        )
        return logo, dominant, len(payload)

    async def _maybe_screenshot_colors(self, url: str) -> list[ColorCandidate]:
        if not self._enable_playwright_pass:
            return []
        screenshot = await self._capture_screenshot(url)
        if screenshot is None:
            return []
        color = await _dominant_color(screenshot, DiscoverySource.DOMINANT_COLOR_SCREENSHOT)
        return [color] if color is not None else []

    async def _capture_screenshot(self, url: str) -> bytes | None:
        try:
            async with async_playwright() as pw:
                browser = await pw.chromium.launch(args=list(_PLAYWRIGHT_LAUNCH_ARGS))
                try:
                    page = await browser.new_page(viewport=_SCREENSHOT_VIEWPORT)
                    await page.goto(url, timeout=self._timeout_s * 1000, wait_until="load")
                    return await page.screenshot(type="png")
                finally:
                    await browser.close()
        except (PlaywrightError, PlaywrightTimeoutError):
            return None

    async def _load_robots(self, base_url: str) -> RobotFileParser:
        robots_url = urljoin(base_url, "/robots.txt")
        parser = RobotFileParser()
        body = await self._fetch_asset_best_effort(robots_url, None, max_bytes=_MAX_ROBOTS_BYTES)
        parser.parse(body.decode("utf-8", errors="ignore").splitlines() if body else [])
        return parser

    def _allowed(self, robots: RobotFileParser | None, url: str) -> bool:
        return robots is None or robots.can_fetch(_USER_AGENT, url)

    async def _fetch_page_or_raise(self, url: str, robots: RobotFileParser) -> str:
        if not self._allowed(robots, url):
            raise WebsiteFetchFailedError(f"robots.txt prohibe rastrear: {url!r}")
        try:
            body = await self._request_bytes_safely(url, max_bytes=self._max_page_bytes)
        except (
            InvalidDiscoveryUrlError,
            WebsiteFetchDeniedError,
            WebsiteFetchTooLargeError,
            httpx.HTTPError,
        ) as exc:
            raise WebsiteFetchFailedError(f"no se pudo descargar {url!r}: {exc}") from exc
        return body.decode("utf-8", errors="ignore")

    async def _fetch_page_best_effort(self, url: str, robots: RobotFileParser) -> str | None:
        body = await self._fetch_asset_best_effort(url, robots, max_bytes=self._max_page_bytes)
        return None if body is None else body.decode("utf-8", errors="ignore")

    async def _fetch_asset_best_effort(
        self, url: str, robots: RobotFileParser | None, *, max_bytes: int
    ) -> bytes | None:
        if not self._allowed(robots, url):
            return None
        try:
            return await self._request_bytes_safely(url, max_bytes=max_bytes)
        except (
            InvalidDiscoveryUrlError,
            WebsiteFetchDeniedError,
            WebsiteFetchTooLargeError,
            WebsiteFetchFailedError,
            httpx.HTTPError,
        ):
            return None

    async def _request_bytes_safely(self, url: str, *, max_bytes: int) -> bytes:
        current_url = url
        for _ in range(_MAX_REDIRECTS + 1):
            response = await self._open_pinned_stream(current_url)
            try:
                if response.is_redirect:
                    location = response.headers.get("location")
                    if not location:
                        raise WebsiteFetchFailedError(f"redirect sin Location: {current_url!r}")
                    current_url = urljoin(current_url, location)
                    continue
                response.raise_for_status()
                return await self._read_within_limit(response, max_bytes)
            finally:
                await response.aclose()
        raise WebsiteFetchFailedError(f"demasiadas redirecciones: {url!r}")

    async def _open_pinned_stream(self, url: str) -> httpx.Response:
        """Valida la URL (host, esquema, puerto -- F-11) y fija la
        conexion a la IP ya validada (F-3, `safe_egress.pin_request`):
        resolver y conectar son la MISMA llamada, sin ventana para que un
        DNS con TTL 0 cambie de respuesta entre ambas (CWE-367)."""
        validate_discovery_url(url)
        try:
            return await open_pinned_stream(
                self._http_client,
                "GET",
                url,
                resolver=self._resolver,
                headers=_REQUEST_HEADERS,
                timeout=self._timeout_s,
            )
        except BlockedEgressAddressError as exc:
            raise WebsiteFetchDeniedError(str(exc)) from exc

    async def _assert_host_resolves_safely(self, hostname: str) -> None:
        try:
            await resolve_pinned_ip(hostname, resolver=self._resolver)
        except BlockedEgressAddressError as exc:
            raise WebsiteFetchDeniedError(str(exc)) from exc

    async def _read_within_limit(self, response: httpx.Response, max_bytes: int) -> bytes:
        chunks: list[bytes] = []
        total = 0
        async for chunk in response.aiter_bytes():
            total += len(chunk)
            if total > max_bytes:
                raise WebsiteFetchTooLargeError(f"{total} bytes > {max_bytes}")
            chunks.append(chunk)
        return b"".join(chunks)


def _collect_contact_channels(pages: list[tuple[str, str]]) -> list[ContactChannelCandidate]:
    channels: list[ContactChannelCandidate] = []
    for page_url, html in pages:
        channels.extend(extract_contact_channels(page_url, html))
    return channels


def _collect_copy_samples(pages: list[tuple[str, str]]) -> list[CopySample]:
    samples: list[CopySample] = []
    for _page_url, html in pages:
        samples.extend(extract_copy_samples(html))
    return samples[:_MAX_TOTAL_COPY_SAMPLES]

"""`PlaywrightBannerComposer` implementa `BannerComposerPort`: plantilla
HTML en `assets/creative-templates/` (Jinja2) + captura con Playwright
(research/content-generation-stack.md §3). El area segura y los limites de
texto se aplican en la composicion, nunca despues (creative-port.md
§"Reglas invariables").

`--disable-gpu --no-sandbox --disable-dev-shm-usage` verificado en la
propia DGX Spark aarch64 de este carril: sin esos tres flags,
`Page.captureScreenshot` falla con "Unable to capture screenshot" (sin
mas detalle) incluso con Chromium arrancando con normalidad."""

from __future__ import annotations

import base64
import hashlib
from collections.abc import Sequence
from contextlib import AbstractAsyncContextManager
from datetime import UTC, datetime
from pathlib import Path

from jinja2 import Environment, FileSystemLoader, Template, select_autoescape
from playwright.async_api import Browser, Playwright, async_playwright

from safent_ads.creative.application.errors import CreativeAssetNotFoundError
from safent_ads.creative.application.ports import (
    AssetRetrievalPort,
    AssetStorePort,
    CreativeAssetRepository,
)
from safent_ads.creative.domain.copy import cta_label_es
from safent_ads.creative.domain.enums import MediaKind, RendererName
from safent_ads.creative.domain.money import Money
from safent_ads.creative.domain.render_specs import BannerSpec, RenderedAsset
from safent_ads.shared.errors import InfrastructureError

_LAUNCH_ARGS = ("--disable-gpu", "--no-sandbox", "--disable-dev-shm-usage")
_BACKGROUND_MIME_TYPE = "image/png"  # LocalAssetStorage siempre escribe PNG para IMAGE/BANNER
_LOCAL_RENDER_COST = Money.zero("USD")
_WIDE_ASPECT_RATIO_THRESHOLD = 3.0
_TALL_ASPECT_RATIO_THRESHOLD = 1.3


class BannerTemplateNotFoundError(InfrastructureError):
    """La plantilla referenciada en `BannerSpec.template_name` no existe."""


def _layout_class(width: int, height: int) -> str:
    aspect = width / height
    if aspect >= _WIDE_ASPECT_RATIO_THRESHOLD:
        return "layout-wide"
    if height / width >= _TALL_ASPECT_RATIO_THRESHOLD:
        return "layout-tall"
    return "layout-square"


def _scaled_font_px(width: int, height: int, ratio: float, minimum: int) -> int:
    return max(minimum, round(min(width, height) * ratio))


class PlaywrightBannerComposer:
    def __init__(
        self,
        templates_dir: Path,
        asset_store: AssetStorePort,
        asset_retrieval: AssetRetrievalPort,
        assets: CreativeAssetRepository,
    ) -> None:
        self._env = Environment(
            loader=FileSystemLoader(str(templates_dir)), autoescape=select_autoescape()
        )
        self._asset_store = asset_store
        self._asset_retrieval = asset_retrieval
        self._assets = assets
        self._browser: Browser | None = None
        self._playwright_cm: AbstractAsyncContextManager[Playwright] | None = None

    async def compose(self, spec: BannerSpec) -> Sequence[RenderedAsset]:
        template = self._load_template(spec.template_name)
        background_data_uri = await self._resolve_background(spec)
        browser = await self._ensure_browser()
        rendered: list[RenderedAsset] = []
        for fmt in spec.formats:
            html = self._render_html(template, spec, fmt.width, fmt.height, background_data_uri)
            payload = await self._screenshot(browser, html, fmt.width, fmt.height)
            checksum = hashlib.sha256(payload).hexdigest()
            storage_uri = await self._asset_store.put(payload, MediaKind.BANNER)
            rendered.append(
                RenderedAsset(
                    storage_uri=storage_uri,
                    media_kind=MediaKind.BANNER,
                    format=fmt,
                    checksum=checksum,
                    renderer_used=RendererName.HTML_BANNER_COMPOSER,
                    cost_estimate=_LOCAL_RENDER_COST,
                    duration_s=None,
                    generated_at=datetime.now(UTC),
                )
            )
        return rendered

    async def aclose(self) -> None:
        if self._browser is not None:
            await self._browser.close()
            self._browser = None
        if self._playwright_cm is not None:
            await self._playwright_cm.__aexit__(None, None, None)
            self._playwright_cm = None

    def _load_template(self, template_name: str) -> Template:
        try:
            return self._env.get_template(template_name)
        except Exception as exc:  # jinja2.TemplateNotFound entre otros
            raise BannerTemplateNotFoundError(template_name) from exc

    async def _resolve_background(self, spec: BannerSpec) -> str | None:
        if spec.background_image is None:
            return None
        asset = await self._assets.get(spec.background_image)
        if asset is None:
            raise CreativeAssetNotFoundError(str(spec.background_image))
        payload = await self._asset_retrieval.get(asset.storage_uri)
        encoded = base64.b64encode(payload).decode("ascii")
        return f"data:{_BACKGROUND_MIME_TYPE};base64,{encoded}"

    def _render_html(
        self, template: Template, spec: BannerSpec, width: int, height: int, background: str | None
    ) -> str:
        return template.render(
            width=width,
            height=height,
            layout_class=_layout_class(width, height),
            headline=spec.ad_copy.headline,
            primary_text=spec.ad_copy.primary_text,
            cta_label=cta_label_es(spec.ad_copy.cta),
            brand_kit=spec.brand_kit,
            safe_area=spec.brand_kit.safe_area,
            background_image_data_uri=background,
            headline_font_px=_scaled_font_px(width, height, 0.09, 12),
            body_font_px=_scaled_font_px(width, height, 0.045, 10),
            cta_font_px=_scaled_font_px(width, height, 0.05, 10),
        )

    async def _ensure_browser(self) -> Browser:
        if self._browser is not None:
            return self._browser
        self._playwright_cm = async_playwright()
        playwright = await self._playwright_cm.__aenter__()
        self._browser = await playwright.chromium.launch(args=list(_LAUNCH_ARGS))
        return self._browser

    async def _screenshot(self, browser: Browser, html: str, width: int, height: int) -> bytes:
        page = await browser.new_page(viewport={"width": width, "height": height})
        try:
            await page.set_content(html)
            return await page.screenshot()
        finally:
            await page.close()

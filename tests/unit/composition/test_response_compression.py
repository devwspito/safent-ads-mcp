"""Item 1 (perf 16-sep, measured on ads.example.com): the panel's JS/CSS
and any JSON API response of a meaningful size travel uncompressed even
though the client advertises `Accept-Encoding: br, gzip`. `create_app`
wires Starlette's `GZipMiddleware` (minimum_size=1024) as the outermost
layer -- see `composition/app.py::create_app` -- so this covers companion
mode too (no Caddy in front, same factory)."""

from __future__ import annotations

from pathlib import Path

from fastapi.testclient import TestClient
from starlette.middleware.gzip import GZipMiddleware

from safent_ads.composition.app import _GZIP_MINIMUM_SIZE_BYTES, create_app
from safent_ads.composition.settings import ApiSettings

_LARGE_INDEX_HTML = "<html><body>" + ("panel " * 300) + "</body></html>"


def test_create_app_registers_gzip_middleware_with_expected_threshold(
    api_settings: ApiSettings,
) -> None:
    app = create_app(api_settings)

    gzip_middlewares = [m for m in app.user_middleware if m.cls is GZipMiddleware]

    assert len(gzip_middlewares) == 1
    assert gzip_middlewares[0].kwargs["minimum_size"] == _GZIP_MINIMUM_SIZE_BYTES == 1024


def test_large_html_response_is_gzip_encoded_for_a_client_that_accepts_it(
    tmp_path: Path, api_settings: ApiSettings
) -> None:
    dist_dir = tmp_path / "dist"
    dist_dir.mkdir()
    (dist_dir / "index.html").write_text(_LARGE_INDEX_HTML, encoding="utf-8")
    settings = api_settings.model_copy(update={"panel_dist_dir": dist_dir})
    assert len(_LARGE_INDEX_HTML.encode()) >= _GZIP_MINIMUM_SIZE_BYTES

    app = create_app(settings)
    with TestClient(app) as client:
        response = client.get("/campaigns/123", headers={"accept-encoding": "gzip"})

    assert response.status_code == 200
    assert response.headers["content-encoding"] == "gzip"
    assert "panel" in response.text  # httpx decodes gzip transparently


def test_small_json_response_is_not_gzip_encoded(api_settings: ApiSettings) -> None:
    app = create_app(api_settings)

    with TestClient(app) as client:
        response = client.get("/api/v1/health", headers={"accept-encoding": "gzip"})

    assert response.status_code == 200
    assert "content-encoding" not in response.headers

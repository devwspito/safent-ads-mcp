"""`_mount_panel_spa` (composition/app.py): sirve `panel/dist/` con
fallback de SPA cuando el directorio existe, y no rompe el arranque
cuando no (build del panel aparte, ausente en CI/dev por defecto)."""

from __future__ import annotations

from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from safent_ads.composition.app import create_app
from safent_ads.composition.settings import ApiSettings


def test_missing_panel_dist_leaves_api_routes_working(api_settings: ApiSettings) -> None:
    app = create_app(api_settings)

    with TestClient(app) as client:
        response = client.get("/api/v1/health")

    assert response.status_code == 200


def test_panel_dist_serves_index_html_for_unknown_route(
    tmp_path: Path, api_settings: ApiSettings
) -> None:
    dist_dir = tmp_path / "dist"
    dist_dir.mkdir()
    (dist_dir / "index.html").write_text("<html>panel</html>", encoding="utf-8")
    settings = api_settings.model_copy(update={"panel_dist_dir": dist_dir})

    app = create_app(settings)
    with TestClient(app) as client:
        response = client.get("/campaigns/123")

    assert response.status_code == 200
    assert response.text == "<html>panel</html>"


def test_panel_dist_serves_real_asset_file_when_present(
    tmp_path: Path, api_settings: ApiSettings
) -> None:
    dist_dir = tmp_path / "dist"
    (dist_dir / "assets").mkdir(parents=True)
    (dist_dir / "index.html").write_text("<html>panel</html>", encoding="utf-8")
    (dist_dir / "assets" / "app.js").write_text("console.log('ok')", encoding="utf-8")
    settings = api_settings.model_copy(update={"panel_dist_dir": dist_dir})

    app = create_app(settings)
    with TestClient(app) as client:
        response = client.get("/assets/app.js")

    assert response.status_code == 200
    assert "console.log" in response.text


def test_hashed_asset_gets_immutable_cache_control(
    tmp_path: Path, api_settings: ApiSettings
) -> None:
    """Item 1 (perf 16-sep): Vite re-hashes the filename on every content
    change, so `/assets/*` is safe to cache for a year without revalidation."""
    dist_dir = tmp_path / "dist"
    (dist_dir / "assets").mkdir(parents=True)
    (dist_dir / "index.html").write_text("<html>panel</html>", encoding="utf-8")
    (dist_dir / "assets" / "index-abc123.js").write_text("console.log('ok')", encoding="utf-8")
    settings = api_settings.model_copy(update={"panel_dist_dir": dist_dir})

    app = create_app(settings)
    with TestClient(app) as client:
        response = client.get("/assets/index-abc123.js")

    assert response.status_code == 200
    assert response.headers["cache-control"] == "public, max-age=31536000, immutable"


def test_non_hashed_static_file_keeps_no_store_cache_control(
    tmp_path: Path, api_settings: ApiSettings
) -> None:
    """A file served straight from `dist/` (not `dist/assets/`) can change
    contents at the same path on the next deploy, so it must not be cached."""
    dist_dir = tmp_path / "dist"
    dist_dir.mkdir()
    (dist_dir / "index.html").write_text("<html>panel</html>", encoding="utf-8")
    (dist_dir / "favicon.ico").write_bytes(b"\x00\x01")
    settings = api_settings.model_copy(update={"panel_dist_dir": dist_dir})

    app = create_app(settings)
    with TestClient(app) as client:
        response = client.get("/favicon.ico")

    assert response.status_code == 200
    assert response.headers["cache-control"] == "no-store"


def test_api_routes_still_win_over_panel_spa_catch_all(
    tmp_path: Path, api_settings: ApiSettings
) -> None:
    dist_dir = tmp_path / "dist"
    dist_dir.mkdir()
    (dist_dir / "index.html").write_text("<html>panel</html>", encoding="utf-8")
    settings = api_settings.model_copy(update={"panel_dist_dir": dist_dir})

    app = create_app(settings)
    with TestClient(app) as client:
        response = client.get("/api/v1/health")

    assert response.status_code == 200
    assert response.json() == {"status": "ok"}


@pytest.mark.parametrize("with_build", [False, True])
@pytest.mark.parametrize("method", ["GET", "POST"])
@pytest.mark.parametrize("path", ["/api/v1/auth/exchange", "/api/v1/absent", "/mcp/absent"])
def test_panel_fallback_never_claims_absent_api_or_mcp_routes(
    tmp_path: Path, api_settings: ApiSettings, with_build: bool, method: str, path: str
) -> None:
    dist_dir = tmp_path / "dist"
    if with_build:
        dist_dir.mkdir()
        (dist_dir / "index.html").write_text("<html>panel</html>", encoding="utf-8")
    settings = api_settings.model_copy(update={"panel_dist_dir": dist_dir})

    with TestClient(create_app(settings)) as client:
        client.cookies.set("ads_csrf", "test-csrf")
        response = client.request(method, path, headers={"x-csrf-token": "test-csrf"})

    assert response.status_code == 404
    assert "<html>panel</html>" not in response.text


def test_panel_fallback_preserves_method_not_allowed_on_existing_api(
    tmp_path: Path, api_settings: ApiSettings
) -> None:
    dist_dir = tmp_path / "dist"
    dist_dir.mkdir()
    (dist_dir / "index.html").write_text("<html>panel</html>", encoding="utf-8")
    settings = api_settings.model_copy(update={"panel_dist_dir": dist_dir})

    with TestClient(create_app(settings)) as client:
        client.cookies.set("ads_csrf", "test-csrf")
        response = client.post("/api/v1/health", headers={"x-csrf-token": "test-csrf"})

    assert response.status_code == 405

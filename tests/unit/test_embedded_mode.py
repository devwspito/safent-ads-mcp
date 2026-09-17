"""Modo empotrado (026, tasks.md T003, contracts/cockpit-read-model.md §5,
contracts/sso.md §5/§7 T-1/E-2): cabeceras por modo, `X-Forwarded-Prefix`
validado contra la allow-list, y el mismo build sirviendo en `/` y en
`/ads/`."""

from __future__ import annotations

from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from safent_ads.composition.app import create_app
from safent_ads.composition.settings import ApiSettings
from tests.unit.composition.factories import build_api_settings


@pytest.fixture
def companion_tls(tmp_path: Path) -> dict[str, Path]:
    certfile = tmp_path / "leaf.crt"
    certfile.write_text("cert")
    keyfile = tmp_path / "leaf.key"
    keyfile.write_text("key")
    return {"tls_certfile": certfile, "tls_keyfile": keyfile}


@pytest.fixture
def panel_dist(tmp_path: Path) -> Path:
    dist_dir = tmp_path / "dist"
    dist_dir.mkdir()
    (dist_dir / "index.html").write_text(
        "<!doctype html><html><head><title>Safent Ads</title>"
        '<script type="module" src="./assets/app.js"></script>'
        '<link rel="stylesheet" href="./assets/app.css"></head>'
        "<body><div id='root'></div></body></html>",
        encoding="utf-8",
    )
    return dist_dir


def test_default_mode_keeps_frame_ancestors_none_and_deny(api_settings: ApiSettings) -> None:
    app = create_app(api_settings)

    with TestClient(app, base_url="https://testserver") as client:
        response = client.get("/api/v1/health")

    assert "frame-ancestors 'none'" in response.headers["content-security-policy"]
    assert response.headers["x-frame-options"] == "DENY"


def test_companion_mode_relaxes_to_self_and_sameorigin(companion_tls: dict[str, Path]) -> None:
    settings = build_api_settings(companion_mode=True, **companion_tls)
    app = create_app(settings)

    with TestClient(app, base_url="https://testserver") as client:
        response = client.get("/api/v1/health")

    assert "frame-ancestors 'self'" in response.headers["content-security-policy"]
    assert response.headers["x-frame-options"] == "SAMEORIGIN"


def test_root_path_is_not_touched_outside_companion_mode(
    api_settings: ApiSettings, panel_dist: Path
) -> None:
    settings = api_settings.model_copy(update={"panel_dist_dir": panel_dist})
    app = create_app(settings)

    with TestClient(app, base_url="https://testserver") as client:
        response = client.get("/campaigns/123", headers={"X-Forwarded-Prefix": "/ads"})

    assert '<meta name="safent-ads-base-path" content="">' in response.text
    assert 'src="/assets/app.js"' in response.text
    assert "<base " not in response.text
    assert "window.__ADS_" not in response.text


def test_companion_mode_serves_the_same_build_at_root(
    companion_tls: dict[str, Path], panel_dist: Path
) -> None:
    settings = build_api_settings(companion_mode=True, panel_dist_dir=panel_dist, **companion_tls)
    app = create_app(settings)

    with TestClient(app, base_url="https://testserver") as client:
        response = client.get("/campaigns/123")

    assert response.status_code == 200
    assert '<meta name="safent-ads-base-path" content="">' in response.text
    assert 'src="/assets/app.js"' in response.text


@pytest.mark.parametrize("path", ["/", "/index.html", "/campaigns/123"])
def test_companion_mode_serves_the_same_build_under_ads_prefix(
    companion_tls: dict[str, Path], panel_dist: Path, path: str
) -> None:
    settings = build_api_settings(companion_mode=True, panel_dist_dir=panel_dist, **companion_tls)
    app = create_app(settings)

    with TestClient(app, base_url="https://testserver") as client:
        response = client.get(path, headers={"X-Forwarded-Prefix": "/ads"})

    assert response.status_code == 200
    assert '<meta name="safent-ads-base-path" content="/ads">' in response.text
    assert 'src="/ads/assets/app.js"' in response.text
    assert 'href="/ads/assets/app.css"' in response.text
    assert "<base " not in response.text
    assert "window.__ADS_" not in response.text
    assert "script-src 'self'" in response.headers["content-security-policy"]
    assert "base-uri 'none'" in response.headers["content-security-policy"]
    assert "unsafe-inline" not in response.headers["content-security-policy"]
    assert response.headers["cache-control"] == "no-store"


def test_hostile_forwarded_prefix_defaults_to_root(
    companion_tls: dict[str, Path], panel_dist: Path
) -> None:
    settings = build_api_settings(companion_mode=True, panel_dist_dir=panel_dist, **companion_tls)
    app = create_app(settings)

    with TestClient(app, base_url="https://testserver") as client:
        response = client.get(
            "/campaigns/123", headers={"X-Forwarded-Prefix": "https://evil.example/phish"}
        )

    assert '<meta name="safent-ads-base-path" content="">' in response.text
    assert "https://evil.example" not in response.text

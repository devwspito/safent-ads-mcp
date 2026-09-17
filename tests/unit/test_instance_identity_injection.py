"""`_render_embedded_index_html` fills the `__ADS_INSTANCE_NAME__`/
`__ADS_PANEL_HOST__` placeholders that `panel/index.html` declares
(contracts/instance-identity.d.ts) -- same non-executable channel that
already injects the embedded prefix (`safent-ads-base-path`)."""

from __future__ import annotations

from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from safent_ads.composition.app import create_app
from tests.unit.composition.factories import build_api_settings


@pytest.fixture
def panel_dist_with_placeholders(tmp_path: Path) -> Path:
    dist_dir = tmp_path / "dist"
    dist_dir.mkdir()
    (dist_dir / "index.html").write_text(
        "<!doctype html><html><head><title>__ADS_INSTANCE_NAME__ · Panel</title></head>"
        "<body><div id='root'>"
        "<p>permite __ADS_PANEL_HOST__ y recarga.</p>"
        "</div></body></html>",
        encoding="utf-8",
    )
    return dist_dir


def test_default_identity_fills_the_placeholders(panel_dist_with_placeholders: Path) -> None:
    settings = build_api_settings(panel_dist_dir=panel_dist_with_placeholders)
    app = create_app(settings)

    with TestClient(app, base_url="https://testserver") as client:
        response = client.get("/")

    assert "<title>Ads MCP · Panel</title>" in response.text
    assert "permite ads.test.ts.net y recarga." in response.text
    assert "__ADS_INSTANCE_NAME__" not in response.text
    assert "__ADS_PANEL_HOST__" not in response.text


def test_configured_instance_name_flows_into_the_placeholders(
    panel_dist_with_placeholders: Path,
) -> None:
    settings = build_api_settings(
        panel_dist_dir=panel_dist_with_placeholders,
        instance_name="Acme Ads MCP",
        public_base_url="https://ads.acme.example",
    )
    app = create_app(settings)

    with TestClient(app, base_url="https://testserver") as client:
        response = client.get("/")

    assert "<title>Acme Ads MCP · Panel</title>" in response.text
    assert "permite ads.acme.example y recarga." in response.text


def test_identity_meta_tags_are_present_for_the_panel_to_read(
    panel_dist_with_placeholders: Path,
) -> None:
    settings = build_api_settings(
        panel_dist_dir=panel_dist_with_placeholders, instance_name="Acme Ads MCP"
    )
    app = create_app(settings)

    with TestClient(app, base_url="https://testserver") as client:
        response = client.get("/")

    assert '<meta name="ads-instance-name" content="Acme Ads MCP">' in response.text
    assert '<meta name="ads-panel-host" content="ads.test.ts.net">' in response.text
    # No inline script sets window globals -- CSP forbids it (test_embedded_mode.py).
    assert "window.__ADS_" not in response.text

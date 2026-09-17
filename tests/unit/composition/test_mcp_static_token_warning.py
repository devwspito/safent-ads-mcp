"""M6 de la revision de seguridad (16-sep, threat-model.md C-53):
`ADS_MCP_STATIC_TOKEN_ENABLED=true` deja rastro en el arranque -- no solo
en `.env` -- para que encenderlo en produccion no pase desapercibido."""

from __future__ import annotations

import pytest
import structlog.testing

from safent_ads.composition import app as app_module
from safent_ads.composition.app import create_app
from safent_ads.composition.settings import ApiSettings


def _capture_startup_logs(
    settings: ApiSettings, monkeypatch: pytest.MonkeyPatch
) -> list[dict[str, object]]:
    """`create_app` llama a `configure_logging()` (produccion, JSON a
    stdout) antes de construir nada -- eso reemplaza por completo la
    lista de procesadores que `capture_logs()` acaba de mutar. Se
    neutraliza aqui porque esta prueba no ejercita `configure_logging`,
    solo el aviso de `_build_mcp_oauth_wiring`."""
    monkeypatch.setattr(app_module, "configure_logging", lambda: None)
    with structlog.testing.capture_logs() as logs:
        app_module.logger = structlog.get_logger()
        create_app(settings)
    return logs


def test_enabling_the_static_token_logs_a_warning_at_startup(
    api_settings: ApiSettings, monkeypatch: pytest.MonkeyPatch
) -> None:
    settings = api_settings.model_copy(update={"mcp_static_token_enabled": True})

    logs = _capture_startup_logs(settings, monkeypatch)

    assert any(entry["event"] == "mcp_static_token_enabled_at_startup" for entry in logs)


def test_default_settings_never_log_the_static_token_warning(
    api_settings: ApiSettings, monkeypatch: pytest.MonkeyPatch
) -> None:
    assert api_settings.mcp_static_token_enabled is False

    logs = _capture_startup_logs(api_settings, monkeypatch)

    assert all(entry["event"] != "mcp_static_token_enabled_at_startup" for entry in logs)

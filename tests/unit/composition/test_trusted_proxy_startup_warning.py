"""I-1 (revision de seguridad 17-sep): todo C-76/C-79 (limite de tasa y
tope por IP de `/api/v1/auth/*`) depende de `ADS_TRUSTED_PROXY_HOPS=1`
detras de un proxy de confianza -- su defecto es `0`. Un despliegue
publico (`ADS_PUBLIC_BASE_URL` en `https://`) con ese defecto sin tocar
particiona esos limites por la IP del proxy, no la del llamante real
(R-19). El arranque deja rastro -- no solo `.env` -- para que quede claro
antes de que alguien tenga que investigar por que el limite de tasa no
sirve de nada."""

from __future__ import annotations

import pytest
import structlog.testing

from safent_ads.composition import api as api_module
from safent_ads.composition import app as app_module
from safent_ads.composition.app import create_app
from safent_ads.composition.settings import ApiSettings

_EVENT = "public_https_without_a_trusted_proxy_at_startup"


def _capture_startup_logs(
    settings: ApiSettings, monkeypatch: pytest.MonkeyPatch
) -> list[dict[str, object]]:
    """Mismo patron que `test_mcp_static_token_warning.py`: `create_app`
    llama a `configure_logging()` (produccion, JSON a stdout) antes de
    construir nada, lo que reemplazaria la lista de procesadores que
    `capture_logs()` acaba de mutar -- neutralizado con el monkeypatch.
    El aviso de este fichero lo emite `composition/api.py`, no `app.py`
    (mcp_static_token_warning): su logger de modulo tambien se reasigna,
    o un `structlog.configure` real de OTRO test en la misma sesion (que
    no pase por este helper) deja cacheado un logger que `capture_logs()`
    ya no intercepta."""
    monkeypatch.setattr(app_module, "configure_logging", lambda: None)
    with structlog.testing.capture_logs() as logs:
        app_module.logger = structlog.get_logger()
        api_module.logger = structlog.get_logger()
        create_app(settings)
    return logs


def test_https_without_a_trusted_proxy_logs_a_warning_at_startup(
    api_settings: ApiSettings, monkeypatch: pytest.MonkeyPatch
) -> None:
    settings = api_settings.model_copy(update={"trusted_proxy_hops": 0})
    assert settings.public_base_url.startswith("https://")

    logs = _capture_startup_logs(settings, monkeypatch)

    assert any(entry["event"] == _EVENT for entry in logs)


def test_a_trusted_proxy_silences_the_warning(
    api_settings: ApiSettings, monkeypatch: pytest.MonkeyPatch
) -> None:
    settings = api_settings.model_copy(update={"trusted_proxy_hops": 1})

    logs = _capture_startup_logs(settings, monkeypatch)

    assert all(entry["event"] != _EVENT for entry in logs)


def test_a_local_http_deployment_never_warns(
    api_settings: ApiSettings, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Companion/desarrollo (`http://localhost`, conexion directa):
    `trusted_proxy_hops=0` es lo CORRECTO ahi, no una configuracion
    peligrosa que avisar."""
    settings = api_settings.model_copy(
        update={"public_base_url": "http://localhost", "trusted_proxy_hops": 0}
    )

    logs = _capture_startup_logs(settings, monkeypatch)

    assert all(entry["event"] != _EVENT for entry in logs)

"""`composition/app.py::create_app` debe pasar `ApiSettings.mcp_extra_
allowed_hosts` a `build_mcp_asgi_apps` (mcp/presentation/http.py) -- sin
este cable, `ADS_MCP_EXTRA_ALLOWED_HOSTS` no tiene ningun efecto en
produccion aunque `ApiSettings` lo parsee bien (defecto real en la
instancia de produccion, 0.2.21: `_transport_security_for` solo conocia
`ADS_PUBLIC_BASE_URL`)."""

from __future__ import annotations

from starlette.routing import Route

from safent_ads.composition.app import create_app
from safent_ads.composition.settings import ApiSettings
from safent_ads.mcp.presentation.http import MCP_ENDPOINT_PATH, ContentLengthLimitMiddleware


def _mcp_route(app) -> Route:  # type: ignore[no-untyped-def]
    for route in app.routes:
        if isinstance(route, Route) and route.path == MCP_ENDPOINT_PATH:
            return route
    raise AssertionError(f"no se encontro la ruta exacta {MCP_ENDPOINT_PATH}")


def test_extra_allowed_host_reaches_the_seat_credential_router(
    api_settings: ApiSettings,
) -> None:
    extra_host = "ads.example.test"
    settings = api_settings.model_copy(update={"mcp_extra_allowed_hosts": [extra_host]})

    app = create_app(settings)

    route = _mcp_route(app)
    endpoint = route.endpoint
    assert isinstance(endpoint, ContentLengthLimitMiddleware)
    router = endpoint._app  # noqa: SLF001 - introspeccion de blanco-caja, mismo criterio que el resto del suite
    assert extra_host in router._allowed_hosts  # noqa: SLF001


def test_without_extra_allowed_hosts_only_the_public_domain_is_allowed(
    api_settings: ApiSettings,
) -> None:
    app = create_app(api_settings)

    route = _mcp_route(app)
    router = route.endpoint._app  # noqa: SLF001

    assert router._allowed_hosts == frozenset(  # noqa: SLF001
        {"ads.test.ts.net", "127.0.0.1", "localhost"}
    )

"""`ADS_CAMPAIGN_PACKAGES_ENABLED` (composition/settings.py) gatea TODA la
superficie de `/api/v1/packages/**` (composition/app.py), lectura incluida
-- igual que gatea `propose_campaign_package` en el catalogo MCP
(tests/unit/mcp/presentation/test_catalog_registries_by_permission.py) --
la saga de publicacion del paquete todavia no existe, Meta falla con
`PLATFORM_NATIVE_INCOMPLETE`.

H2 (revision de codigo, saga de publicacion 2026-09-15): las rutas de
LECTURA dejaron de quedarse siempre montadas -- un catalogo de lectura de
un feature que no se puede operar no tiene proposito, y la version
anterior de este test fijaba ese comportamiento como contrato.

`create_app` no toca la base de datos al construirse (mismo patron que
`tests/unit/composition/test_managed_app.py`): `Container.build` es
sincrono y solo arma el `async_sessionmaker`, nunca abre una conexion."""

from __future__ import annotations

from collections.abc import Iterable
from typing import Any

from fastapi import FastAPI

from safent_ads.composition.app import (
    _build_creative_generation_services,
    _build_mcp_registry_and_dispatcher,
    create_app,
)
from safent_ads.composition.container import Container
from tests.unit.composition.factories import build_api_settings

_WRITE_ROUTES: frozenset[tuple[str, str]] = frozenset(
    {
        ("POST", "/api/v1/packages/{package_id}/approve"),
        ("POST", "/api/v1/packages/{package_id}/reject"),
        ("PUT", "/api/v1/packages/{package_id}/owner-context"),
        ("GET", "/api/v1/packages/{package_id}/creative-candidates"),
        ("PATCH", "/api/v1/packages/{package_id}/ads/{ad_local_ref:path}/creative"),
    }
)
_READ_ROUTES: frozenset[tuple[str, str]] = frozenset(
    {
        ("GET", "/api/v1/packages"),
        ("GET", "/api/v1/packages/{package_id}"),
    }
)


def _route_signatures(app: FastAPI) -> set[tuple[str, str]]:
    """Aplana `app.routes`: FastAPI envuelve cada `include_router` en un
    `_IncludedRouter` perezoso (`original_router`) en vez de exponer las
    `APIRoute` directamente -- sin aplanar, ninguna ruta montada via
    `include_router` aparece aqui."""
    signatures: set[tuple[str, str]] = set()
    pending: list[Any] = list(app.routes)
    while pending:
        route = pending.pop()
        nested_router = getattr(route, "original_router", None)
        if nested_router is not None:
            pending.extend(nested_router.routes)
            continue
        path = getattr(route, "path", None)
        methods: Iterable[str] | None = getattr(route, "methods", None)
        if path is None or methods is None:
            continue
        signatures.update((method, path) for method in methods)
    return signatures


def test_write_and_read_routes_are_both_absent_when_flag_is_off() -> None:
    app = create_app(build_api_settings(campaign_packages_enabled=False))

    routes = _route_signatures(app)

    assert routes.isdisjoint(_WRITE_ROUTES)
    assert routes.isdisjoint(_READ_ROUTES)


def test_write_routes_are_present_when_flag_is_on() -> None:
    app = create_app(build_api_settings(campaign_packages_enabled=True))

    routes = _route_signatures(app)

    assert _WRITE_ROUTES <= routes
    assert _READ_ROUTES <= routes


def _mcp_tool_names(*, campaign_packages_enabled: bool) -> set[str]:
    settings = build_api_settings(campaign_packages_enabled=campaign_packages_enabled)
    container = Container.build(settings)
    creative_generation = _build_creative_generation_services(container, settings)
    registry, _dispatcher = _build_mcp_registry_and_dispatcher(
        container, settings, creative_generation
    )
    return {definition.name for definition in registry}


def test_propose_campaign_package_mcp_tool_is_not_registered_when_flag_is_off() -> None:
    assert "propose_campaign_package" not in _mcp_tool_names(campaign_packages_enabled=False)


def test_propose_campaign_package_mcp_tool_is_registered_when_flag_is_on() -> None:
    assert "propose_campaign_package" in _mcp_tool_names(campaign_packages_enabled=True)

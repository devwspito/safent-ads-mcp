"""`create_app` monta `build_brand_router` (composition/app.py) sobre
`RequestScopedBrandKitRepository`/`RequestScopedBrandDiscoveryDraftRepository`
reales: a diferencia de `creative` (repos en memoria), `brand` tiene
persistencia SQL real desde `0014_brand`/`0015_brand_discovery`, asi que
no hay razon para dejarlo fuera de la app. Solo prueba el cableado (las 7
rutas registradas, todas fallan cerrado sin sesion) --
`tests/unit/brand/presentation/test_router.py` ya cubre el CRUD del caso
de uso, `tests/integration/brand/test_sql_brand_kit_repository.py` y
`tests/integration/brand/test_sql_brand_discovery_draft_repository.py` ya
cubren los repositorios SQL, `tests/integration/brand/
test_authorization_integration.py` cubre `require_business_access` contra
Postgres real (401/404).

Las rutas mutantes (`POST`) pasan primero por `CsrfMiddleware`
(`composition/api.py`, C-26): sin el par cookie/cabecera de doble envio
responden 403 antes de llegar a la sesion, igual que documenta
`tests/unit/test_api_hardening.py`. Este archivo satisface el CSRF
primero para que el 401 que prueba sea inequivocamente el de
`require_business_access`, no el de `CsrfMiddleware`."""

from __future__ import annotations

import inspect
import uuid

import pytest
from fastapi.testclient import TestClient

from safent_ads.brand.application.ingest_brand_from_website import IngestBrandFromWebsite
from safent_ads.brand.infrastructure.website_brand_extractor import WebsiteBrandExtractor
from safent_ads.composition import app as app_module
from safent_ads.composition.app import create_app
from safent_ads.composition.settings import ApiSettings

_PLACEHOLDER_ASSET_ID = "01ARZ3NDEKTSV4RRFFQ69G5FAV"

_ROUTE_CASES: list[tuple[str, str]] = [
    ("GET", "/api/v1/brand"),
    ("GET", "/api/v1/brand/assets"),
    ("POST", "/api/v1/brand/assets"),
    ("GET", f"/api/v1/brand/assets/{_PLACEHOLDER_ASSET_ID}/preview"),
    ("POST", "/api/v1/brand/discover"),
    ("GET", "/api/v1/brand/draft"),
    ("POST", "/api/v1/brand/confirm"),
]


@pytest.mark.parametrize(("method", "path"), _ROUTE_CASES)
def test_brand_route_is_mounted_and_requires_a_session(
    api_settings: ApiSettings, method: str, path: str
) -> None:
    app = create_app(api_settings)

    with TestClient(app, base_url="https://testserver") as client:
        client.get("/api/v1/health")
        csrf_token = client.cookies.get("ads_csrf")

        response = client.request(
            method,
            path,
            params={"business_id": str(uuid.uuid4())},
            headers={"X-CSRF-Token": csrf_token} if csrf_token else {},
        )

    assert response.status_code == 401, f"{method} {path} devolvio {response.status_code}"


def test_not_yet_wired_website_brand_discovery_placeholder_is_gone() -> None:
    """`_NotYetWiredWebsiteBrandDiscovery` (composition/app.py) existia
    solo hasta que `security-engineer` revisara la superficie SSRF de
    `WebsiteBrandExtractor` -- esa revision ya paso (verdict "WIRE AFTER
    FIXES") y el adaptador real esta cableado; el placeholder no debe
    seguir en el modulo."""
    assert not hasattr(app_module, "_NotYetWiredWebsiteBrandDiscovery")


def test_ingest_brand_from_website_is_wired_to_the_real_extractor(
    api_settings: ApiSettings,
) -> None:
    discovery = app_module._build_website_brand_discovery(  # noqa: SLF001
        storage=object(),  # type: ignore[arg-type]
        clock=object(),  # type: ignore[arg-type]
    )

    assert isinstance(discovery, WebsiteBrandExtractor)


def test_build_brand_router_source_never_mentions_the_placeholder() -> None:
    source = inspect.getsource(app_module._build_brand_router)  # noqa: SLF001

    assert "NotYetWired" not in source
    assert IngestBrandFromWebsite.__name__ in source

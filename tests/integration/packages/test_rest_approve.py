"""ME-6 (data-model.md Revision 2, medios): `POST .../approve` debe ser
idempotente por `(package_id, package_hash)` -- una segunda llamada con la
MISMA huella devuelve la MISMA `publication_id`, nunca un error. Sin esto,
un doble clic o un reintento de red del panel (misma peticion, dos veces)
rompe el flujo de "Aprobar y publicar" justo despues de haber aprobado de
verdad la primera vez."""

from __future__ import annotations

import uuid
from collections.abc import AsyncIterator
from dataclasses import dataclass
from pathlib import Path

import httpx
import pytest
from fastapi import FastAPI
from sqlalchemy import text

from safent_ads.composition.api import _handle_api_error
from safent_ads.composition.container import Container
from safent_ads.creative.infrastructure.local_asset_storage import LocalAssetStorage
from safent_ads.iam.presentation.errors import ApiError
from safent_ads.packages.domain.identifiers import OfferingId
from safent_ads.packages.infrastructure.sql_package_repository import SqlCampaignPackageRepository
from safent_ads.packages.presentation.panel_read import build_package_read_router
from safent_ads.packages.presentation.rest import build_package_admin_router
from safent_ads.shared.clock import SystemClock
from tests.integration.composition.conftest import AuthenticatedSession
from tests.integration.composition.conftest import (
    authenticated_session as authenticated_session,  # noqa: PLC0414 - pytest fixture re-export
)
from tests.integration.packages.conftest import SeededScope, seed_package_scope
from tests.unit.composition.factories import build_api_settings
from tests.unit.packages.domain.conftest import propose_meta_package

pytestmark = pytest.mark.integration

_GUARDRAIL_SQL = text("""
    INSERT INTO guardrails
        (scope, business_id, currency, daily_cap_minor, monthly_cap_minor, budget_floor_minor,
         budget_ceiling_minor, max_step_pct, max_changes_per_entity_per_day)
    VALUES ('business', :business_id, 'EUR', 3000, 90000, 0, 3000, 30, 10)
""")

# `isolated_database_url` (tests/conftest.py) es una unica base por sesion de
# pytest, no por test: un freno GLOBAL enganchado por otro fichero de la
# misma sesion (p. ej. `test_telegram_brake_flow.py`) sigue activo aqui --
# mismo criterio de aislamiento que `test_rest_endpoints.py`.
_RELEASE_STALE_BRAKES_SQL = text("""
    UPDATE emergency_brakes SET released_at = now(), released_by = 'test-isolation-reset'
     WHERE released_at IS NULL
""")


@dataclass(frozen=True, slots=True)
class _Seeded:
    container: Container
    scope: SeededScope


@pytest.fixture
async def seeded(
    isolated_database_url: str, authenticated_session: AuthenticatedSession
) -> AsyncIterator[_Seeded]:
    settings = build_api_settings(database_url=isolated_database_url)
    container = Container.build(settings)
    business_id = uuid.uuid4()
    async with container.session_factory() as session:
        await session.execute(_RELEASE_STALE_BRAKES_SQL)
        await session.execute(
            text(
                "INSERT INTO businesses (id, slug, name, timezone, reference_currency) "
                "VALUES (:id, :slug, 'Negocio ME-6', 'Europe/Madrid', 'EUR')"
            ),
            {"id": business_id, "slug": f"pkg-me6-{uuid.uuid4().hex[:10]}"},
        )
        scope = await seed_package_scope(
            session, owner_id=authenticated_session.owner_id, business_id=business_id
        )
        await session.execute(_GUARDRAIL_SQL, {"business_id": business_id})
        await session.commit()
    try:
        yield _Seeded(container=container, scope=scope)
    finally:
        await container.aclose()


def _app(container: Container, asset_store: LocalAssetStorage) -> FastAPI:
    application = FastAPI()
    application.state.container = container
    application.add_exception_handler(ApiError, _handle_api_error)
    application.include_router(build_package_read_router(container.session_factory, asset_store))
    application.include_router(
        build_package_admin_router(
            container.session_factory,
            SystemClock(),
            asset_store,
            container.approval_key_pair.signer,
            lambda session: container.build_execution_use_cases(session).pause_entity,
        )
    )
    return application


def _client(
    container: Container, asset_store: LocalAssetStorage, cookies: dict[str, str]
) -> httpx.AsyncClient:
    transport = httpx.ASGITransport(app=_app(container, asset_store))
    return httpx.AsyncClient(transport=transport, base_url="http://test", cookies=cookies)


async def _seed_package(container: Container, scope: SeededScope):
    package = propose_meta_package(
        # La fabrica usa un NOW fijo (14-sep) + 72 h: con el reloj REAL del caso de uso
        # el paquete nacia caducado desde el 17-sep 10:00 UTC. Aqui nace ahora.
        now=SystemClock().now(),
        business=scope.business_id,
        account=scope.account_ref,
        offering_id=OfferingId(scope.offering_id),
    )
    async with container.session_factory() as session:
        await SqlCampaignPackageRepository(session).add(package)
        await session.commit()
    return package


async def test_doble_aprobacion_devuelve_la_misma_publicacion(
    seeded: _Seeded, authenticated_session: AuthenticatedSession, tmp_path: Path
) -> None:
    package = await _seed_package(seeded.container, seeded.scope)
    asset_store = LocalAssetStorage(tmp_path, b"0" * 32, SystemClock())

    async with _client(seeded.container, asset_store, authenticated_session.cookies) as client:
        first = await client.post(
            f"/api/v1/packages/{package.package_id}/approve"
            f"?business_id={seeded.scope.business_id}",
            json={"package_hash": package.package_hash.value},
        )
        assert first.status_code == 200, first.text

        # Doble clic / reintento de red del panel: la MISMA peticion, otra
        # vez, con la MISMA huella -- nunca un 409, nunca una segunda
        # publicacion.
        second = await client.post(
            f"/api/v1/packages/{package.package_id}/approve"
            f"?business_id={seeded.scope.business_id}",
            json={"package_hash": package.package_hash.value},
        )

    assert second.status_code == 200, second.text
    assert second.json()["publication_id"] == first.json()["publication_id"]

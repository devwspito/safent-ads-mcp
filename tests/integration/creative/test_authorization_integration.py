"""`build_creative_router` sobre adaptadores SQL reales y `Container`
(mismo patron que `tests/integration/composition/test_idor_sweep_new_routes.py`):
IDOR entre dos negocios reales, 404 de recurso ajeno, 202/409 de
`POST /creative-jobs` segun haya o no un `ImageRendererPort` inyectado, y
`POST /creatives/{id}/propose-publication` creando una `Proposal` de
verdad via `ContainerCreativeProposalGateway` (la unica puerta hacia
`proposals`)."""

from __future__ import annotations

import uuid
from collections.abc import AsyncIterator, Mapping
from datetime import UTC, datetime
from pathlib import Path

import httpx
import pytest
from fastapi import FastAPI
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine

from safent_ads.composition.api import _handle_api_error
from safent_ads.composition.container import Container
from safent_ads.composition.creative_proposal_gateway import ContainerCreativeProposalGateway
from safent_ads.creative.application.generate_creative_assets import GenerateCreativeAssets
from safent_ads.creative.application.ports import ImageRendererPort
from safent_ads.creative.application.propose_creative import ProposeCreative
from safent_ads.creative.application.run_policy_check import RunPolicyCheck
from safent_ads.creative.domain.enums import MediaKind, PolicyVerdictResult, RendererName
from safent_ads.creative.domain.identifiers import AssetId, BriefId
from safent_ads.creative.domain.money import Money
from safent_ads.creative.domain.policy import PolicyVerdict
from safent_ads.creative.domain.render_specs import ImageSpec, RenderedAsset
from safent_ads.creative.domain.renderer_selector import RendererSelector
from safent_ads.creative.domain.storage import StorageUri
from safent_ads.creative.infrastructure.in_process_gpu_queue import InProcessGpuQueue
from safent_ads.creative.infrastructure.local_asset_storage import LocalAssetStorage
from safent_ads.creative.infrastructure.policy_checker import LocalPolicyChecker
from safent_ads.creative.infrastructure.sql_repositories import (
    RequestScopedCreativeAssetRepository,
    RequestScopedCreativeBriefRepository,
    RequestScopedCreativeJobRepository,
    SqlCreativeAssetRepository,
    SqlCreativeBriefRepository,
)
from safent_ads.creative.presentation.router import build_creative_router
from safent_ads.iam.presentation.dependencies import SESSION_COOKIE_NAME
from safent_ads.iam.presentation.errors import ApiError
from safent_ads.panel.presentation.deps import AuthenticatedCaller, get_authenticated_caller
from safent_ads.shared.ids import BusinessId
from tests.contracts.sql_fixtures import campaign_ref, seed_entity
from tests.unit.composition.factories import build_api_settings
from tests.unit.creative.domain.factories import make_ad_copy, make_brief
from tests.unit.creative.infrastructure.fakes import make_creative_asset

pytestmark = pytest.mark.integration

_RAW_TOKEN = "creative-integration-test-raw-session-token"  # noqa: S105 - fixture
_BRIEF_PAYLOAD_SHOTS: list[dict[str, object]] = [
    {"order": 1, "description": "Aula"},
    {"order": 2, "description": "Presentador"},
    {"order": 3, "description": "Estudiante feliz"},
]


class _FakeImageRenderer:
    def __init__(self) -> None:
        self.name = RendererName.GPT_IMAGE_1_5

    async def render(self, spec: ImageSpec) -> RenderedAsset:
        return RenderedAsset(
            storage_uri=StorageUri("image/fake.png"),
            media_kind=MediaKind.IMAGE,
            format=spec.format,
            checksum="a" * 64,
            renderer_used=self.name,
            cost_estimate=Money.zero("USD"),
            duration_s=None,
            generated_at=datetime.now(UTC),
        )


def _brief_payload(business_id: str) -> dict[str, object]:
    return {
        "business_id": business_id,
        "calendar_event_id": None,
        "objective": "lead",
        "audience_summary": "Adultos 25-45",
        "hook": "Tu plaza empieza aqui",
        "shots": _BRIEF_PAYLOAD_SHOTS,
        "on_screen_text": [],
        "cta": "Apúntate ya",
        "voiceover_lines": [],
        "brand_kit": {
            "primary_font": "Inter",
            "secondary_font": "Inter",
            "primary_color_hex": "#112233",
            "secondary_color_hex": "#FFFFFF",
            "logo_asset_id": str(AssetId.new()),
        },
        "source_signal_id": None,
        "variant_count": 1,
    }


def _build_app(
    container: Container,
    *,
    image_renderers: Mapping[RendererName, ImageRendererPort] | None = None,
) -> FastAPI:
    briefs = RequestScopedCreativeBriefRepository(container.session_factory)
    assets = RequestScopedCreativeAssetRepository(container.session_factory)
    jobs = RequestScopedCreativeJobRepository(container.session_factory)
    asset_store = LocalAssetStorage(
        Path("/tmp/creative-integration-assets"),
        signing_key=b"integration-test-signing-key",
        clock=container.clock,
    )
    generate = GenerateCreativeAssets(
        briefs=briefs,
        jobs=jobs,
        assets=assets,
        image_renderers=image_renderers or {},
        renderer_selector=RendererSelector(),
        gpu_lease=InProcessGpuQueue(container.clock),
    )
    router = build_creative_router(
        briefs=briefs,
        assets=assets,
        jobs=jobs,
        asset_store=asset_store,
        generate_creative_assets=generate,
        run_policy_check=RunPolicyCheck(LocalPolicyChecker(assets), assets),
        propose_creative=ProposeCreative(ContainerCreativeProposalGateway(container), assets),
        clock=container.clock,
    )
    app = FastAPI()
    app.state.container = container
    app.add_exception_handler(ApiError, _handle_api_error)
    app.include_router(router)
    return app


def _client_scoped_to(app: FastAPI, business_id: uuid.UUID) -> httpx.AsyncClient:
    async def _restricted() -> AuthenticatedCaller:
        return AuthenticatedCaller(allowed_business_ids=frozenset({str(business_id)}))

    app.dependency_overrides[get_authenticated_caller] = _restricted
    transport = httpx.ASGITransport(app=app)
    return httpx.AsyncClient(
        transport=transport, base_url="http://test", cookies={SESSION_COOKIE_NAME: _RAW_TOKEN}
    )


class _TwoBusinesses:
    def __init__(self, *, business_a: uuid.UUID, business_b: uuid.UUID) -> None:
        self.business_a = business_a
        self.business_b = business_b


@pytest.fixture
async def two_businesses(isolated_database_url: str) -> AsyncIterator[_TwoBusinesses]:
    """Siembra directamente contra `ads_isolated` (autocommit, motor
    propio): `Container.build(settings)` en cada test abre SU PROPIO motor
    contra la misma URL, asi que las filas tienen que estar confirmadas
    de verdad, no en un savepoint de `db_session` (que apunta a la base
    COMPARTIDA, `database_url`) -- mismo patron que
    `tests/integration/composition/test_idor_sweep_new_routes.py`."""
    engine = create_async_engine(isolated_database_url, pool_pre_ping=True)
    business_a = uuid.uuid4()
    business_b = uuid.uuid4()
    async with engine.begin() as connection:
        for business_id in (business_a, business_b):
            await connection.execute(
                text(
                    "INSERT INTO businesses (id, slug, name, timezone, reference_currency) "
                    "VALUES (:id, :slug, 'Fixture', 'Europe/Madrid', 'EUR')"
                ),
                {"id": str(business_id), "slug": f"fixture-{business_id.hex[:8]}"},
            )
    try:
        yield _TwoBusinesses(business_a=business_a, business_b=business_b)
    finally:
        await engine.dispose()


async def _seed_asset(
    session: AsyncSession, *, business_id: BusinessId, ad_copy: object = None
) -> AssetId:
    brief_id = BriefId.new()
    await SqlCreativeBriefRepository(session).add(brief_id, make_brief(business_id=business_id))
    asset = make_creative_asset(business_id=business_id, brief_id=brief_id, ad_copy=ad_copy)
    await SqlCreativeAssetRepository(session).add(asset)
    return asset.asset_id


async def test_get_creative_returns_200_shape_over_real_postgres(
    two_businesses: _TwoBusinesses, isolated_database_url: str
) -> None:
    settings = build_api_settings(database_url=isolated_database_url)
    container = Container.build(settings)
    try:
        async with container.session_factory() as session:
            asset_id = await _seed_asset(
                session, business_id=BusinessId(two_businesses.business_a)
            )
            await session.commit()
        app = _build_app(container)
        async with _client_scoped_to(app, two_businesses.business_a) as client:
            response = await client.get(f"/api/v1/creatives/{asset_id}")
        assert response.status_code == 200
        body = response.json()
        assert body["asset_id"] == str(asset_id)
        assert body["business_id"] == str(two_businesses.business_a)
        assert "preview_url" in body
    finally:
        await container.aclose()


async def test_get_creative_404_for_unknown_asset(
    two_businesses: _TwoBusinesses, isolated_database_url: str
) -> None:
    settings = build_api_settings(database_url=isolated_database_url)
    container = Container.build(settings)
    try:
        app = _build_app(container)
        async with _client_scoped_to(app, two_businesses.business_a) as client:
            response = await client.get(f"/api/v1/creatives/{AssetId.new()}")
        assert response.status_code == 404
    finally:
        await container.aclose()


async def test_get_creative_idor_returns_404_for_a_business_the_caller_cannot_see(
    two_businesses: _TwoBusinesses, isolated_database_url: str
) -> None:
    settings = build_api_settings(database_url=isolated_database_url)
    container = Container.build(settings)
    try:
        async with container.session_factory() as session:
            asset_id = await _seed_asset(
                session, business_id=BusinessId(two_businesses.business_b)
            )
            await session.commit()
        app = _build_app(container)
        async with _client_scoped_to(app, two_businesses.business_a) as client:
            response = await client.get(f"/api/v1/creatives/{asset_id}")
        assert response.status_code == 404
    finally:
        await container.aclose()


async def test_create_creative_job_returns_202_and_persists_when_renderer_configured(
    two_businesses: _TwoBusinesses, isolated_database_url: str
) -> None:
    settings = build_api_settings(database_url=isolated_database_url)
    container = Container.build(settings)
    try:
        app = _build_app(
            container, image_renderers={RendererName.GPT_IMAGE_1_5: _FakeImageRenderer()}
        )
        async with _client_scoped_to(app, two_businesses.business_a) as client:
            response = await client.post(
                "/api/v1/creative-jobs",
                json={"brief": _brief_payload(str(two_businesses.business_a))},
            )
        assert response.status_code == 202
        job_id = response.json()["job_id"]
        async with container.session_factory() as session:
            row = (
                await session.execute(
                    text("SELECT state FROM creative_jobs WHERE id = :id"), {"id": job_id}
                )
            ).one_or_none()
        assert row is not None
        assert row.state == "ready"
    finally:
        await container.aclose()


async def test_create_creative_job_returns_409_when_no_renderer_configured(
    two_businesses: _TwoBusinesses, isolated_database_url: str
) -> None:
    """Cablea `image_renderers={}` -- mismo estado que produccion cuando el
    broker no tiene ninguna clave de proveedor configurada
    (`composition/app.py::_build_broker_image_renderers`, `BrokerSettings`
    las guarda aparte, threat-model.md C-29): la cascada se agota siempre."""
    settings = build_api_settings(database_url=isolated_database_url)
    container = Container.build(settings)
    try:
        app = _build_app(container)
        async with _client_scoped_to(app, two_businesses.business_a) as client:
            response = await client.post(
                "/api/v1/creative-jobs",
                json={"brief": _brief_payload(str(two_businesses.business_a))},
            )
        assert response.status_code == 409
        assert response.json()["error"]["code"] == "CREATIVE_RENDERER_UNAVAILABLE"
    finally:
        await container.aclose()


async def test_reject_creative_persists_over_real_postgres(
    two_businesses: _TwoBusinesses, isolated_database_url: str
) -> None:
    settings = build_api_settings(database_url=isolated_database_url)
    container = Container.build(settings)
    try:
        async with container.session_factory() as session:
            asset_id = await _seed_asset(
                session, business_id=BusinessId(two_businesses.business_a)
            )
            await session.commit()
        app = _build_app(container)
        async with _client_scoped_to(app, two_businesses.business_a) as client:
            response = await client.post(
                f"/api/v1/creatives/{asset_id}/reject", json={"reason": "marca incorrecta"}
            )
        assert response.status_code == 200
        assert response.json() == {"asset_id": str(asset_id), "review_state": "rejected"}
        async with container.session_factory() as session:
            row = (
                await session.execute(
                    text("SELECT state FROM creative_assets WHERE id = :id"),
                    {"id": str(asset_id)},
                )
            ).one_or_none()
        assert row is not None
        assert row.state == "rejected"
    finally:
        await container.aclose()


async def test_propose_publication_creates_a_real_proposal(
    isolated_database_url: str,
) -> None:
    settings = build_api_settings(database_url=isolated_database_url)
    container = Container.build(settings)
    try:
        # `seed_entity` solo siembra la jerarquia completa (cuenta ->
        # entidad) para el nivel CAMPAIGN (tests/contracts/sql_fixtures.py);
        # `ad_entities_parent_level_check` exige un `parent_ref` real para
        # AD_SET/AD/CREATIVE que no es el objeto de esta prueba -- el
        # gateway solo necesita resolver CUALQUIER `AdEntity` por
        # `EntityRef` y comparar su `business_id`, sea cual sea su nivel.
        ad_set_ref = campaign_ref(f"as-{uuid.uuid4().hex[:10]}")
        async with container.session_factory() as session:
            business_id = await seed_entity(session, ad_set_ref)
            asset_repository = SqlCreativeAssetRepository(session)
            asset_id = await _seed_asset(
                session, business_id=BusinessId(business_id), ad_copy=make_ad_copy()
            )
            asset = await asset_repository.get(asset_id)
            assert asset is not None
            asset.mark_ready(PolicyVerdict(verdict=PolicyVerdictResult.PASS_, findings=()))
            await asset_repository.update(asset)
            await session.commit()
        app = _build_app(container)
        async with _client_scoped_to(app, business_id) as client:
            response = await client.post(
                f"/api/v1/creatives/{asset_id}/propose-publication",
                json={
                    "ad_set_ref": str(ad_set_ref),
                    "ad_copy": {
                        "headline": "Tu plaza empieza aqui",
                        "primary_text": "Prepárate con nosotros.",
                        "cta": "Apúntate",
                    },
                    "typed_confirmation": "PUBLICAR",
                },
            )
        assert response.status_code == 201, response.text
        body = response.json()
        async with container.session_factory() as session:
            row = (
                await session.execute(
                    text("SELECT state, classification FROM proposals WHERE id = :id"),
                    {"id": body["proposal_id"]},
                )
            ).one_or_none()
        assert row is not None
        assert row.state == "pending"
        assert row.classification == "important"
    finally:
        await container.aclose()

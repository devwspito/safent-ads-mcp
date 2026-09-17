"""`build_creative_router`: rutas REST de lectura + las cinco escrituras
que `contracts/rest-api.md §Creatividades` agrupa con ellas. `TestClient`
sobre una `FastAPI` desnuda con `panel.presentation.deps` overriden (mismo
patron que `tests/unit/panel/presentation/test_rest.py`) -- la sesion real
de `iam`/`Container` se prueba aparte en
`tests/integration/creative/test_authorization_integration.py`."""

from __future__ import annotations

import asyncio
import uuid
from collections.abc import Sequence
from datetime import UTC, datetime

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from starlette.responses import JSONResponse

from safent_ads.creative.application.errors import AdSetReferenceNotFoundError
from safent_ads.creative.application.generate_creative_assets import GenerateCreativeAssets
from safent_ads.creative.application.ports import CreativePublicationProposal, ProposedAdCopy
from safent_ads.creative.application.propose_creative import ProposeCreative
from safent_ads.creative.application.run_policy_check import RunPolicyCheck
from safent_ads.creative.domain.creative_asset import CreativeAsset
from safent_ads.creative.domain.enums import MediaKind, PolicyVerdictResult, RendererName
from safent_ads.creative.domain.identifiers import AssetId, JobId
from safent_ads.creative.domain.money import Money
from safent_ads.creative.domain.policy import PolicyVerdict
from safent_ads.creative.domain.render_specs import ImageSpec, RenderedAsset
from safent_ads.creative.domain.renderer_selector import RendererSelector
from safent_ads.creative.domain.storage import StorageUri
from safent_ads.creative.infrastructure.in_memory_repositories import (
    InMemoryCreativeAssetRepository,
    InMemoryCreativeBriefRepository,
    InMemoryCreativeJobRepository,
)
from safent_ads.creative.infrastructure.in_process_gpu_queue import InProcessGpuQueue
from safent_ads.creative.infrastructure.policy_checker import LocalPolicyChecker
from safent_ads.creative.presentation.router import build_creative_router
from safent_ads.iam.presentation.errors import ApiError
from safent_ads.panel.presentation.deps import (
    AuthenticatedCaller,
    ensure_business_access,
    get_authenticated_caller,
    require_business_access,
)
from safent_ads.shared.clock import FixedClock
from safent_ads.shared.ids import BusinessId
from tests.unit.creative.domain.factories import make_ad_copy, make_brief
from tests.unit.creative.infrastructure.fakes import FakeAssetStore, make_creative_asset

_BUSINESS_A = str(uuid.uuid4())
_BUSINESS_B = str(uuid.uuid4())
_FIXED_NOW = datetime(2026, 9, 10, tzinfo=UTC)


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
            generated_at=_FIXED_NOW,
        )


class _FakeProposalGateway:
    def __init__(self, *, error: Exception | None = None) -> None:
        self._error = error
        self.calls: list[dict[str, object]] = []

    async def propose_creative_publication(
        self,
        *,
        asset_id: AssetId,
        business_id: BusinessId,
        ad_set_ref: str,
        ad_copy: ProposedAdCopy,
        extra_asset_ids: Sequence[AssetId],
    ) -> CreativePublicationProposal:
        if self._error is not None:
            raise self._error
        self.calls.append(
            {
                "asset_id": asset_id,
                "business_id": business_id,
                "ad_set_ref": ad_set_ref,
                "ad_copy": ad_copy,
                "extra_asset_ids": tuple(extra_asset_ids),
            }
        )
        return CreativePublicationProposal(
            proposal_id="01H0000000000000000000PROP",
            diff_hash="a" * 64,
            expires_at=datetime(2026, 9, 11, tzinfo=UTC),
        )


def _valid_brief_payload(business_id: str) -> dict[str, object]:
    return {
        "business_id": business_id,
        "calendar_event_id": None,
        "objective": "lead",
        "audience_summary": "Adultos 25-45",
        "hook": "Tu plaza empieza aqui",
        "shots": [
            {"order": 1, "description": "Aula"},
            {"order": 2, "description": "Presentador"},
            {"order": 3, "description": "Estudiante feliz"},
        ],
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


class _Client:
    def __init__(
        self,
        client: TestClient,
        assets: InMemoryCreativeAssetRepository,
        briefs: InMemoryCreativeBriefRepository,
        jobs: InMemoryCreativeJobRepository,
        proposal_gateway: _FakeProposalGateway,
    ) -> None:
        self.client = client
        self.assets = assets
        self.briefs = briefs
        self.jobs = jobs
        self.proposal_gateway = proposal_gateway


def _build_client(
    *,
    allowed_business_ids: frozenset[str] | None = None,
    register_renderer: bool = True,
    proposal_gateway: _FakeProposalGateway | None = None,
) -> _Client:
    briefs = InMemoryCreativeBriefRepository()
    jobs = InMemoryCreativeJobRepository()
    assets = InMemoryCreativeAssetRepository()
    asset_store = FakeAssetStore()
    image_renderers = (
        {RendererName.GPT_IMAGE_1_5: _FakeImageRenderer()} if register_renderer else {}
    )
    generate = GenerateCreativeAssets(
        briefs=briefs,
        jobs=jobs,
        assets=assets,
        image_renderers=image_renderers,  # type: ignore[arg-type]
        renderer_selector=RendererSelector(),
        gpu_lease=InProcessGpuQueue(FixedClock(_FIXED_NOW)),
    )
    policy_check = RunPolicyCheck(LocalPolicyChecker(assets), assets)
    gateway = proposal_gateway or _FakeProposalGateway()
    propose_creative = ProposeCreative(gateway, assets)  # type: ignore[arg-type]
    router = build_creative_router(
        briefs=briefs,
        assets=assets,
        jobs=jobs,
        asset_store=asset_store,  # type: ignore[arg-type]
        generate_creative_assets=generate,
        run_policy_check=policy_check,
        propose_creative=propose_creative,
        clock=FixedClock(_FIXED_NOW),
    )
    app = FastAPI()
    app.include_router(router)
    app.add_exception_handler(ApiError, _handle_api_error)

    async def _caller() -> AuthenticatedCaller:
        return AuthenticatedCaller(allowed_business_ids=allowed_business_ids)

    async def _require_business_access_without_db(business_id: str) -> str:
        # Mismo criterio que `tests/unit/panel/presentation/test_rest.py`:
        # sin `Container`/Postgres en este archivo, se salta solo la
        # comprobacion de EXISTENCIA (`SqlBusinessDirectory.exists`), nunca
        # el alcance del caller (`ensure_business_access`) ni el formato
        # (mismo 404 que produciria `_ensure_business_exists` en produccion).
        try:
            uuid.UUID(business_id)
        except ValueError as exc:
            raise ApiError(
                status_code=404, code="NOT_FOUND", message="No encontrado."
            ) from exc
        ensure_business_access(business_id, await _caller())
        return business_id

    app.dependency_overrides[get_authenticated_caller] = _caller
    app.dependency_overrides[require_business_access] = _require_business_access_without_db
    return _Client(TestClient(app), assets, briefs, jobs, gateway)


async def _handle_api_error(_request: object, exc: Exception) -> JSONResponse:
    assert isinstance(exc, ApiError)
    return JSONResponse(status_code=exc.status_code, content={"error": exc.detail})


def _client() -> _Client:
    return _build_client(allowed_business_ids=None)


# --- GET /creatives ----------------------------------------------------


def test_list_creatives_requires_valid_business_id() -> None:
    ctx = _client()

    response = ctx.client.get("/api/v1/creatives", params={"business_id": "not-a-uuid"})

    assert response.status_code == 404


def test_list_creatives_returns_only_matching_business() -> None:
    ctx = _client()
    business_id = uuid.uuid4()

    asyncio.run(ctx.assets.add(make_creative_asset(business_id=BusinessId(business_id))))
    asyncio.run(ctx.assets.add(make_creative_asset()))

    response = ctx.client.get("/api/v1/creatives", params={"business_id": str(business_id)})

    assert response.status_code == 200
    body = response.json()["items"]
    assert len(body) == 1
    item = body[0]
    assert item["business_id"] == str(business_id)
    assert item["review_state"] == "pending"
    assert item["signal"] == "LEARNING"
    assert item["spend"] == {"amount": 0.0, "currency": "USD"}
    assert item["ads_running_on"] == []


def test_list_creatives_filters_by_pending_approval() -> None:
    ctx = _client()
    business_id = uuid.uuid4()
    pending = make_creative_asset(business_id=BusinessId(business_id))
    approved = make_creative_asset(business_id=BusinessId(business_id))
    approved.mark_ready(PolicyVerdict(verdict=PolicyVerdictResult.PASS_, findings=()))
    approved.propose()
    approved.approve()
    asyncio.run(ctx.assets.add(pending))
    asyncio.run(ctx.assets.add(approved))

    response = ctx.client.get(
        "/api/v1/creatives",
        params={"business_id": str(business_id), "pending_approval": "true"},
    )

    body = response.json()["items"]
    assert [item["asset_id"] for item in body] == [str(pending.asset_id)]


# --- GET /creatives/{asset_id} ------------------------------------------


def test_get_creative_not_found_returns_404_not_403() -> None:
    ctx = _client()

    response = ctx.client.get(f"/api/v1/creatives/{AssetId.new()}")

    assert response.status_code == 404
    assert response.json()["error"]["code"] == "NOT_FOUND"


def test_get_creative_returns_full_zod_shape_with_signed_preview_url() -> None:
    ctx = _client()
    asset = make_creative_asset()
    asyncio.run(ctx.assets.add(asset))

    response = ctx.client.get(f"/api/v1/creatives/{asset.asset_id}")

    assert response.status_code == 200
    body = response.json()
    assert "preview_url" in body
    assert body["label"] == "Creatividad"
    assert body["days_in_rotation"] >= 0
    assert body["policy_findings"] == []
    assert body["policy_verdict"] == "PENDING"


def test_get_creative_idor_returns_404_for_foreign_business() -> None:
    ctx = _build_client(allowed_business_ids=frozenset({_BUSINESS_A}))
    asset = make_creative_asset(business_id=BusinessId.parse(_BUSINESS_B))
    asyncio.run(ctx.assets.add(asset))

    response = ctx.client.get(f"/api/v1/creatives/{asset.asset_id}")

    assert response.status_code == 404


# --- POST /creative-jobs -------------------------------------------------


def test_create_creative_job_returns_202_with_job_id() -> None:
    ctx = _client()

    response = ctx.client.post(
        "/api/v1/creative-jobs", json={"brief": _valid_brief_payload(str(uuid.uuid4()))}
    )

    assert response.status_code == 202
    assert "job_id" in response.json()


def test_create_creative_job_rejects_extra_field() -> None:
    ctx = _client()
    payload = {"brief": _valid_brief_payload(str(uuid.uuid4())), "not_allowed": "x"}

    response = ctx.client.post("/api/v1/creative-jobs", json=payload)

    assert response.status_code == 422


def test_create_creative_job_idor_returns_404_for_foreign_business() -> None:
    ctx = _build_client(allowed_business_ids=frozenset({_BUSINESS_A}))

    response = ctx.client.post(
        "/api/v1/creative-jobs", json={"brief": _valid_brief_payload(_BUSINESS_B)}
    )

    assert response.status_code == 404


def test_create_creative_job_returns_409_when_no_renderer_configured() -> None:
    ctx = _build_client(register_renderer=False)

    response = ctx.client.post(
        "/api/v1/creative-jobs", json={"brief": _valid_brief_payload(str(uuid.uuid4()))}
    )

    assert response.status_code == 409
    body = response.json()["error"]
    assert body["code"] == "CREATIVE_RENDERER_UNAVAILABLE"
    assert "capability_report" in body["details"]


# --- GET /creative-jobs/{job_id} ----------------------------------------


def test_get_creative_job_after_create() -> None:
    ctx = _client()
    create_response = ctx.client.post(
        "/api/v1/creative-jobs", json={"brief": _valid_brief_payload(str(uuid.uuid4()))}
    )
    job_id = create_response.json()["job_id"]

    response = ctx.client.get(f"/api/v1/creative-jobs/{job_id}")

    assert response.status_code == 200
    body = response.json()
    assert body["state"] == "READY"
    assert body["progress"] == pytest.approx(100.0)
    assert body["renderer_used"] == RendererName.GPT_IMAGE_1_5.value
    assert body["cost_estimate"] == {"amount": 0.0, "currency": "USD"}
    assert body["assets"][0]["asset_id"]


def test_get_creative_job_not_found() -> None:
    ctx = _client()

    response = ctx.client.get(f"/api/v1/creative-jobs/{JobId.new()}")

    assert response.status_code == 404


# --- POST /creatives/{asset_id}/policy-check ----------------------------


def test_policy_check_route_rejects_asset_without_copy() -> None:
    ctx = _client()
    asset = make_creative_asset()
    asyncio.run(ctx.assets.add(asset))

    response = ctx.client.post(
        f"/api/v1/creatives/{asset.asset_id}/policy-check",
        json={"platform": "meta", "placement": "feed"},
    )

    assert response.status_code == 422


def test_policy_check_route_passes_for_clean_copy() -> None:
    ctx = _client()
    asset = make_creative_asset(ad_copy=make_ad_copy())
    asyncio.run(ctx.assets.add(asset))

    response = ctx.client.post(
        f"/api/v1/creatives/{asset.asset_id}/policy-check",
        json={"platform": "meta", "placement": "feed"},
    )

    assert response.status_code == 200
    body = response.json()
    assert body["verdict"] == "PASS"
    assert body["findings"] == []


def test_policy_check_idor_returns_404_for_foreign_business() -> None:
    ctx = _build_client(allowed_business_ids=frozenset({_BUSINESS_A}))
    asset = make_creative_asset(
        business_id=BusinessId.parse(_BUSINESS_B), ad_copy=make_ad_copy()
    )
    asyncio.run(ctx.assets.add(asset))

    response = ctx.client.post(
        f"/api/v1/creatives/{asset.asset_id}/policy-check",
        json={"platform": "meta", "placement": "feed"},
    )

    assert response.status_code == 404


# --- POST /creatives/{asset_id}/reject -----------------------------------


def test_reject_creative_marks_review_state_rejected() -> None:
    ctx = _client()
    asset = make_creative_asset()
    asyncio.run(ctx.assets.add(asset))

    response = ctx.client.post(
        f"/api/v1/creatives/{asset.asset_id}/reject", json={"reason": "Marca incorrecta"}
    )

    assert response.status_code == 200
    assert response.json() == {"asset_id": str(asset.asset_id), "review_state": "rejected"}
    reloaded = asyncio.run(ctx.assets.get(asset.asset_id))
    assert reloaded is not None
    assert reloaded.state.value == "rejected"


def test_reject_creative_conflicts_when_already_published() -> None:
    ctx = _client()
    asset = make_creative_asset()
    asset.mark_ready(PolicyVerdict(verdict=PolicyVerdictResult.PASS_, findings=()))
    asset.propose()
    asset.approve()
    asset.mark_published()
    asyncio.run(ctx.assets.add(asset))

    response = ctx.client.post(
        f"/api/v1/creatives/{asset.asset_id}/reject", json={"reason": "tarde"}
    )

    assert response.status_code == 409
    assert response.json()["error"]["code"] == "INVALID_CREATIVE_STATE"


def test_reject_creative_not_found() -> None:
    ctx = _client()

    response = ctx.client.post(
        f"/api/v1/creatives/{AssetId.new()}/reject", json={"reason": "x"}
    )

    assert response.status_code == 404


def test_reject_creative_idor_returns_404_for_foreign_business() -> None:
    ctx = _build_client(allowed_business_ids=frozenset({_BUSINESS_A}))
    asset = make_creative_asset(business_id=BusinessId.parse(_BUSINESS_B))
    asyncio.run(ctx.assets.add(asset))

    response = ctx.client.post(
        f"/api/v1/creatives/{asset.asset_id}/reject", json={"reason": "x"}
    )

    assert response.status_code == 404


# --- POST /creatives/{asset_id}/regenerate -------------------------------


def test_regenerate_creative_returns_202_with_job_id() -> None:
    ctx = _client()
    asset = make_creative_asset()
    asyncio.run(ctx.briefs.add(asset.provenance.brief_id, _matching_brief(asset)))
    asyncio.run(ctx.assets.add(asset))

    response = ctx.client.post(
        f"/api/v1/creatives/{asset.asset_id}/regenerate", json={"reason": "Cambia el fondo"}
    )

    assert response.status_code == 202
    assert "job_id" in response.json()


def test_regenerate_creative_returns_409_when_no_renderer_configured() -> None:
    ctx = _build_client(register_renderer=False)
    asset = make_creative_asset()
    asyncio.run(ctx.briefs.add(asset.provenance.brief_id, _matching_brief(asset)))
    asyncio.run(ctx.assets.add(asset))

    response = ctx.client.post(
        f"/api/v1/creatives/{asset.asset_id}/regenerate", json={"reason": "x"}
    )

    assert response.status_code == 409
    assert response.json()["error"]["code"] == "CREATIVE_RENDERER_UNAVAILABLE"


def test_regenerate_creative_idor_returns_404_for_foreign_business() -> None:
    ctx = _build_client(allowed_business_ids=frozenset({_BUSINESS_A}))
    asset = make_creative_asset(business_id=BusinessId.parse(_BUSINESS_B))
    asyncio.run(ctx.assets.add(asset))

    response = ctx.client.post(
        f"/api/v1/creatives/{asset.asset_id}/regenerate", json={"reason": "x"}
    )

    assert response.status_code == 404


# --- POST /creatives/{asset_id}/propose-publication ----------------------


def _publishable_asset() -> CreativeAsset:
    asset = make_creative_asset(ad_copy=make_ad_copy())
    asset.mark_ready(PolicyVerdict(verdict=PolicyVerdictResult.PASS_, findings=()))
    return asset


def _matching_brief(asset: CreativeAsset) -> object:
    return make_brief(business_id=asset.business_id)


def _propose_payload(**overrides: object) -> dict[str, object]:
    payload: dict[str, object] = {
        "ad_set_ref": "meta:ad_set:123456",
        "ad_copy": {
            "headline": "Tu plaza empieza aqui",
            "primary_text": "Prepárate con nosotros.",
            "cta": "Apúntate",
        },
        "typed_confirmation": "PUBLICAR",
    }
    payload.update(overrides)
    return payload


def test_propose_publication_returns_201_with_proposal() -> None:
    ctx = _client()
    asset = _publishable_asset()
    asyncio.run(ctx.assets.add(asset))

    response = ctx.client.post(
        f"/api/v1/creatives/{asset.asset_id}/propose-publication",
        json=_propose_payload(),
    )

    assert response.status_code == 201
    body = response.json()
    assert body["proposal_id"]
    assert body["diff_hash"]
    assert body["expires_at"]
    assert len(ctx.proposal_gateway.calls) == 1


def test_propose_publication_requires_typed_confirmation() -> None:
    ctx = _client()
    asset = _publishable_asset()
    asyncio.run(ctx.assets.add(asset))

    response = ctx.client.post(
        f"/api/v1/creatives/{asset.asset_id}/propose-publication",
        json=_propose_payload(typed_confirmation="si"),
    )

    assert response.status_code == 422
    assert response.json()["error"]["code"] == "TYPED_CONFIRMATION_REQUIRED"
    assert ctx.proposal_gateway.calls == []


def test_propose_publication_requires_policy_check_pass() -> None:
    ctx = _client()
    asset = make_creative_asset(ad_copy=make_ad_copy())  # sin policy-check todavia
    asyncio.run(ctx.assets.add(asset))

    response = ctx.client.post(
        f"/api/v1/creatives/{asset.asset_id}/propose-publication",
        json=_propose_payload(),
    )

    assert response.status_code == 422
    assert response.json()["error"]["code"] == "POLICY_CHECK_REQUIRED"


def test_propose_publication_returns_404_for_unknown_ad_set_ref() -> None:
    gateway = _FakeProposalGateway(error=AdSetReferenceNotFoundError("nope"))
    ctx = _build_client(proposal_gateway=gateway)
    asset = _publishable_asset()
    asyncio.run(ctx.assets.add(asset))

    response = ctx.client.post(
        f"/api/v1/creatives/{asset.asset_id}/propose-publication",
        json=_propose_payload(),
    )

    assert response.status_code == 404


def test_propose_publication_idor_returns_404_for_foreign_business() -> None:
    ctx = _build_client(allowed_business_ids=frozenset({_BUSINESS_A}))
    asset = make_creative_asset(business_id=BusinessId.parse(_BUSINESS_B))
    asyncio.run(ctx.assets.add(asset))

    response = ctx.client.post(
        f"/api/v1/creatives/{asset.asset_id}/propose-publication",
        json=_propose_payload(),
    )

    assert response.status_code == 404

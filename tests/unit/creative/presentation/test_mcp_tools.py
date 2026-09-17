"""`CreativeMcpTools`: manejadores puros pydantic-estrictos (threat-model.md
C-11). No hay `ToolRegistry` aqui — se prueban como funciones normales."""

from __future__ import annotations

import asyncio
import uuid
from datetime import UTC, datetime

import pytest
from pydantic import ValidationError

from safent_ads.creative.application.draft_brief import DraftBrief, DraftBriefRequest
from safent_ads.creative.application.errors import (
    CreativeAssetNotFoundError,
    CreativeBriefNotFoundError,
    CreativeJobNotFoundError,
)
from safent_ads.creative.application.generate_creative_assets import GenerateCreativeAssets
from safent_ads.creative.application.import_creative_asset import ImportCreativeAsset
from safent_ads.creative.application.run_policy_check import RunPolicyCheck
from safent_ads.creative.domain.enums import (
    CampaignObjective,
    MediaKind,
    Placement,
    PolicyVerdictResult,
    RendererName,
)
from safent_ads.creative.domain.identifiers import AssetId, BriefId, JobId
from safent_ads.creative.domain.money import Money
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
from safent_ads.creative.presentation.mcp_tools import (
    CreativeMcpTools,
    GenerateCreativeAssetsArgs,
    GetCreativeArgs,
    GetCreativeJobArgs,
    ImportCreativeAssetArgs,
    ImportCreativeAssetResult,
    ListCreativeBriefsArgs,
    ListCreativesArgs,
    RunCreativePolicyCheckArgs,
)
from safent_ads.shared.clock import FixedClock
from safent_ads.shared.ids import BusinessId, PlatformCode
from tests.unit.creative.domain.factories import make_ad_copy, make_brand_kit, make_shots
from tests.unit.creative.infrastructure.fakes import FakeAssetStore, make_creative_asset


class _FakeAssetFetch:
    def __init__(self, payload: bytes = b"\x89PNG\r\n\x1a\nrest-of-file") -> None:
        self.payload = payload

    async def fetch(self, url: str) -> bytes:  # noqa: ARG002
        return self.payload


class _FakeImageRenderer:
    """Simula un proveedor directo (BYOK) ya configurado — el candidato
    delegado siempre lanza `RendererDelegationRequiredError` primero, la
    cascada de `GenerateCreativeAssets` cae en este."""

    def __init__(self) -> None:
        self.name = RendererName.GPT_IMAGE_1_5
        self.calls = 0

    async def render(self, spec: ImageSpec) -> RenderedAsset:
        self.calls += 1
        return RenderedAsset(
            storage_uri=StorageUri(f"image/fake-{self.calls}.png"),
            media_kind=MediaKind.IMAGE,
            format=spec.format,
            checksum="a" * 64,
            renderer_used=self.name,
            cost_estimate=Money.zero("USD"),
            duration_s=None,
            generated_at=datetime.now(UTC),
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
        "on_screen_text": ["Plazas limitadas"],
        "cta": "Apúntate ya",
        "voiceover_lines": ["Prepárate con Negocio Ejemplo."],
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


def _tools(
    briefs: InMemoryCreativeBriefRepository,
    jobs: InMemoryCreativeJobRepository,
    assets: InMemoryCreativeAssetRepository,
    fake_renderer: _FakeImageRenderer,
) -> CreativeMcpTools:
    generate = GenerateCreativeAssets(
        briefs=briefs,
        jobs=jobs,
        assets=assets,
        image_renderers={RendererName.GPT_IMAGE_1_5: fake_renderer},  # type: ignore[dict-item]
        renderer_selector=RendererSelector(),
        gpu_lease=InProcessGpuQueue(FixedClock(datetime.now(UTC))),
    )
    policy_check = RunPolicyCheck(LocalPolicyChecker(assets), assets)
    import_creative_asset = ImportCreativeAsset(
        briefs=briefs,
        assets=assets,
        asset_fetch=_FakeAssetFetch(),
        asset_store=FakeAssetStore(),
        allowed_hosts=frozenset({"v3.fal.media"}),
        clock=FixedClock(datetime.now(UTC)),
    )
    return CreativeMcpTools(
        briefs=briefs,
        jobs=jobs,
        assets=assets,
        generate_creative_assets=generate,
        run_policy_check=policy_check,
        import_creative_asset=import_creative_asset,
    )


def test_generate_creative_assets_rejects_extra_field() -> None:
    payload = _valid_brief_payload(str(uuid.uuid4()))
    payload["not_allowed"] = "x"

    with pytest.raises(ValidationError):
        GenerateCreativeAssetsArgs(brief=payload)  # type: ignore[arg-type]


def test_generate_creative_assets_rejects_bad_asset_id_pattern() -> None:
    payload = _valid_brief_payload(str(uuid.uuid4()))
    brand_kit = payload["brand_kit"]
    assert isinstance(brand_kit, dict)
    brand_kit["logo_asset_id"] = "not-a-ulid"

    with pytest.raises(ValidationError):
        GenerateCreativeAssetsArgs(brief=payload)  # type: ignore[arg-type]


def test_get_creative_rejects_free_form_string() -> None:
    with pytest.raises(ValidationError):
        GetCreativeArgs(asset_id="../../etc/passwd")


def test_generate_creative_assets_creates_job_and_asset() -> None:
    async def _run() -> None:
        business_id = str(uuid.uuid4())
        briefs = InMemoryCreativeBriefRepository()
        jobs = InMemoryCreativeJobRepository()
        assets = InMemoryCreativeAssetRepository()
        fake_renderer = _FakeImageRenderer()
        tools = _tools(briefs, jobs, assets, fake_renderer)

        args = GenerateCreativeAssetsArgs(brief=_valid_brief_payload(business_id))
        result = await tools.generate_creative_assets(args)

        assert result.estimated_seconds > 0
        job = await jobs.get(JobId.parse(result.job_id))
        assert job is not None
        assert job.state.value == "ready"

    asyncio.run(_run())


def test_get_creative_not_found_raises() -> None:
    async def _run() -> None:
        assets = InMemoryCreativeAssetRepository()
        tools = _tools(
            InMemoryCreativeBriefRepository(),
            InMemoryCreativeJobRepository(),
            assets,
            _FakeImageRenderer(),
        )
        await tools.get_creative(GetCreativeArgs(asset_id=str(AssetId.new())))

    with pytest.raises(CreativeAssetNotFoundError):
        asyncio.run(_run())


def test_list_creatives_filters_by_business() -> None:
    async def _run() -> list[dict[str, object]]:
        business_id = BusinessId.new()
        assets = InMemoryCreativeAssetRepository()
        matching = make_creative_asset(business_id=business_id)
        other = make_creative_asset()
        await assets.add(matching)
        await assets.add(other)
        tools = _tools(
            InMemoryCreativeBriefRepository(),
            InMemoryCreativeJobRepository(),
            assets,
            _FakeImageRenderer(),
        )
        return await tools.list_creatives(ListCreativesArgs(business_id=str(business_id)))

    result = asyncio.run(_run())
    assert len(result) == 1


def test_get_creative_job_not_found_raises() -> None:
    async def _run() -> None:
        jobs = InMemoryCreativeJobRepository()
        tools = _tools(
            InMemoryCreativeBriefRepository(),
            jobs,
            InMemoryCreativeAssetRepository(),
            _FakeImageRenderer(),
        )
        await tools.get_creative_job(GetCreativeJobArgs(job_id=str(JobId.new())))

    with pytest.raises(CreativeJobNotFoundError):
        asyncio.run(_run())


def test_list_creative_briefs_returns_only_own_business() -> None:
    async def _run() -> list[dict[str, object]]:
        briefs = InMemoryCreativeBriefRepository()
        business_id = BusinessId.new()
        draft = DraftBrief(briefs)
        await draft.execute(
            DraftBriefRequest(
                business_id=business_id,
                calendar_event_id=None,
                objective=CampaignObjective.LEAD,
                audience_summary="x",
                hook="hook",
                shots=make_shots(),
                on_screen_text=[],
                cta="cta",
                voiceover_lines=[],
                brand_kit=make_brand_kit(),
                source_signal_id=None,
                variant_count=1,
            )
        )
        tools = _tools(
            briefs,
            InMemoryCreativeJobRepository(),
            InMemoryCreativeAssetRepository(),
            _FakeImageRenderer(),
        )
        return await tools.list_creative_briefs(
            ListCreativeBriefsArgs(business_id=str(business_id))
        )

    result = asyncio.run(_run())
    assert len(result) == 1


def test_import_creative_asset_rejects_non_https_url() -> None:
    with pytest.raises(ValidationError):
        ImportCreativeAssetArgs(
            brief_id=str(BriefId.new()),
            source_url="http://v3.fal.media/files/out.png",
            media_kind="image",
            native_tool_used="image_generate",
        )


async def _draft_brief(briefs: InMemoryCreativeBriefRepository, business_id: BusinessId) -> BriefId:
    draft = DraftBrief(briefs)
    return await draft.execute(
        DraftBriefRequest(
            business_id=business_id,
            calendar_event_id=None,
            objective=CampaignObjective.LEAD,
            audience_summary="x",
            hook="hook",
            shots=make_shots(),
            on_screen_text=[],
            cta="cta",
            voiceover_lines=[],
            brand_kit=make_brand_kit(),
            source_signal_id=None,
            variant_count=1,
        )
    )


def test_import_creative_asset_stores_asset_and_returns_checksum() -> None:
    async def _run() -> ImportCreativeAssetResult:
        briefs = InMemoryCreativeBriefRepository()
        business_id = BusinessId.new()
        brief_id = await _draft_brief(briefs, business_id)
        tools = _tools(
            briefs,
            InMemoryCreativeJobRepository(),
            InMemoryCreativeAssetRepository(),
            _FakeImageRenderer(),
        )
        return await tools.import_creative_asset(
            ImportCreativeAssetArgs(
                brief_id=str(brief_id),
                source_url="https://v3.fal.media/files/out.png",
                media_kind="image",
                native_tool_used="image_generate",
            )
        )

    result = asyncio.run(_run())
    assert len(result.checksum) == 64


def test_import_creative_asset_raises_when_brief_missing() -> None:
    async def _run() -> None:
        tools = _tools(
            InMemoryCreativeBriefRepository(),
            InMemoryCreativeJobRepository(),
            InMemoryCreativeAssetRepository(),
            _FakeImageRenderer(),
        )
        await tools.import_creative_asset(
            ImportCreativeAssetArgs(
                brief_id=str(BriefId.new()),
                source_url="https://v3.fal.media/files/out.png",
                media_kind="image",
                native_tool_used="image_generate",
            )
        )

    with pytest.raises(CreativeBriefNotFoundError):
        asyncio.run(_run())


def test_run_creative_policy_check_delegates() -> None:
    async def _run() -> PolicyVerdictResult:
        assets = InMemoryCreativeAssetRepository()
        asset = make_creative_asset(ad_copy=make_ad_copy())
        await assets.add(asset)
        tools = _tools(
            InMemoryCreativeBriefRepository(),
            InMemoryCreativeJobRepository(),
            assets,
            _FakeImageRenderer(),
        )
        result = await tools.run_creative_policy_check(
            RunCreativePolicyCheckArgs(
                asset_id=str(asset.asset_id), platform=PlatformCode.META, placement=Placement.FEED
            )
        )
        return PolicyVerdictResult(result["verdict"])  # type: ignore[arg-type]

    assert asyncio.run(_run()) == PolicyVerdictResult.PASS_

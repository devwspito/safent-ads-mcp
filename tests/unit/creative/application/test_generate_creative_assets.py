"""`GenerateCreativeAssets`: cascada delegado -> proveedor directo -> local
(`RendererSelector.candidates_for`). Un candidato que lanza
`RendererDelegationRequiredError` (el nivel `DELEGATED`, siempre) no
detiene el trabajo — cae al siguiente. Si TODOS fallan,
`ImageGenerationUnavailableError` lleva un mensaje accionable
(correccion del propietario 2026-09-09: nunca degradacion silenciosa)."""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime

import pytest

from safent_ads.creative.application.delegation import (
    DelegationInstruction,
    RendererDelegationRequiredError,
)
from safent_ads.creative.application.errors import (
    CreativeBriefNotFoundError,
    ImageGenerationUnavailableError,
)
from safent_ads.creative.application.generate_creative_assets import GenerateCreativeAssets
from safent_ads.creative.domain.enums import (
    CreativeJobState,
    GenerationStatus,
    MediaKind,
    RendererName,
)
from safent_ads.creative.domain.identifiers import BriefId
from safent_ads.creative.domain.money import Money
from safent_ads.creative.domain.render_specs import ImageSpec, RenderedAsset
from safent_ads.creative.domain.renderer_selector import RendererSelector, build_registry
from safent_ads.creative.domain.storage import StorageUri
from safent_ads.creative.infrastructure.hermes_tool_renderer import HermesToolRenderer
from safent_ads.creative.infrastructure.in_memory_repositories import (
    InMemoryCreativeAssetRepository,
    InMemoryCreativeBriefRepository,
    InMemoryCreativeJobRepository,
)
from safent_ads.creative.infrastructure.in_process_gpu_queue import InProcessGpuQueue
from safent_ads.shared.clock import FixedClock
from tests.unit.creative.domain.factories import make_brief


class _FakeDirectProviderRenderer:
    def __init__(self) -> None:
        self.name = RendererName.GPT_IMAGE_1_5
        self.calls = 0

    async def render(self, spec: ImageSpec) -> RenderedAsset:
        self.calls += 1
        return RenderedAsset(
            storage_uri=StorageUri(f"image/direct-{self.calls}.png"),
            media_kind=MediaKind.IMAGE,
            format=spec.format,
            checksum="a" * 64,
            renderer_used=self.name,
            cost_estimate=Money.zero("USD"),
            duration_s=None,
            generated_at=datetime.now(UTC),
        )


class _AlwaysDeclinesRenderer:
    """Simula un proveedor directo configurado que rechaza toda peticion
    (p.ej. clave invalida, politica de contenido)."""

    def __init__(self, name: RendererName) -> None:
        self.name = name

    async def render(self, spec: ImageSpec) -> RenderedAsset:  # noqa: ARG002
        raise RuntimeError("proveedor rechazo la generacion")


def _build_use_case(
    *, image_renderers: dict[RendererName, object], registry: object = None
) -> tuple[GenerateCreativeAssets, InMemoryCreativeBriefRepository, InMemoryCreativeJobRepository]:
    briefs = InMemoryCreativeBriefRepository()
    jobs = InMemoryCreativeJobRepository()
    assets = InMemoryCreativeAssetRepository()
    selector = RendererSelector(registry) if registry is not None else RendererSelector()
    use_case = GenerateCreativeAssets(
        briefs=briefs,
        jobs=jobs,
        assets=assets,
        image_renderers=image_renderers,  # type: ignore[arg-type]
        renderer_selector=selector,
        gpu_lease=InProcessGpuQueue(FixedClock(datetime.now(UTC))),
    )
    return use_case, briefs, jobs


def test_delegated_candidate_falls_back_to_direct_provider() -> None:
    async def _run() -> CreativeJobState:
        direct = _FakeDirectProviderRenderer()
        use_case, briefs, jobs = _build_use_case(
            image_renderers={
                RendererName.HERMES_NATIVE_DELEGATED: HermesToolRenderer(),
                RendererName.GPT_IMAGE_1_5: direct,
            }
        )
        brief_id = BriefId.new()
        await briefs.add(brief_id, make_brief(variant_count=1))

        job_ids = await use_case.execute(brief_id)

        job = await jobs.get(job_ids[0])
        assert job is not None
        assert direct.calls == 1
        return job.state

    assert asyncio.run(_run()) == CreativeJobState.READY


def test_generation_status_is_model_generated_when_a_candidate_completes() -> None:
    async def _run() -> GenerationStatus:
        direct = _FakeDirectProviderRenderer()
        use_case, briefs, jobs = _build_use_case(
            image_renderers={RendererName.GPT_IMAGE_1_5: direct}
        )
        brief_id = BriefId.new()
        await briefs.add(brief_id, make_brief(variant_count=1))

        job_ids = await use_case.execute(brief_id)
        job = await jobs.get(job_ids[0])
        assert job is not None
        asset_id = job.asset_ids[0]
        assets = use_case._assets  # type: ignore[attr-defined]
        asset = await assets.get(asset_id)
        assert asset is not None
        return asset.provenance.generation_status

    assert asyncio.run(_run()) == GenerationStatus.MODEL_GENERATED


def test_all_candidates_exhausted_raises_actionable_unavailable_error() -> None:
    async def _run() -> None:
        use_case, briefs, _ = _build_use_case(image_renderers={})
        brief_id = BriefId.new()
        await briefs.add(brief_id, make_brief(variant_count=1))

        await use_case.execute(brief_id)

    with pytest.raises(ImageGenerationUnavailableError, match="image_generate"):
        asyncio.run(_run())


def test_unavailable_error_names_the_missing_credential() -> None:
    async def _run() -> None:
        use_case, briefs, _ = _build_use_case(image_renderers={})
        brief_id = BriefId.new()
        await briefs.add(brief_id, make_brief(variant_count=1))

        await use_case.execute(brief_id)

    with pytest.raises(ImageGenerationUnavailableError, match="OPENAI_API_KEY"):
        asyncio.run(_run())


def test_direct_provider_failure_after_delegation_also_falls_through_to_local() -> None:
    async def _run() -> CreativeJobState:
        local = _FakeDirectProviderRenderer()
        local.name = RendererName.QWEN_IMAGE_2512
        use_case, briefs, jobs = _build_use_case(
            image_renderers={
                RendererName.HERMES_NATIVE_DELEGATED: HermesToolRenderer(),
                RendererName.GPT_IMAGE_1_5: _AlwaysDeclinesRenderer(RendererName.GPT_IMAGE_1_5),
                RendererName.QWEN_IMAGE_2512: local,
            },
            registry=build_registry(local_enabled=True),
        )
        brief_id = BriefId.new()
        await briefs.add(brief_id, make_brief(variant_count=1))

        job_ids = await use_case.execute(brief_id)
        job = await jobs.get(job_ids[0])
        assert job is not None
        return job.state

    assert asyncio.run(_run()) == CreativeJobState.READY


def test_raises_brief_not_found() -> None:
    async def _run() -> None:
        use_case, _, _ = _build_use_case(image_renderers={})
        await use_case.execute(BriefId.new())

    with pytest.raises(CreativeBriefNotFoundError):
        asyncio.run(_run())


def test_delegation_instruction_type_is_importable_for_presentation_mapping() -> None:
    instruction = DelegationInstruction(native_tool="image_generate", arguments={"a": 1})

    error = RendererDelegationRequiredError(instruction)

    assert error.instruction is instruction

"""`GenerateCreativeAssets` (plan.md §5 `creative`; contracts/mcp-tools.md
`generate_creative_assets`): matriz de variantes visuales de un brief.

Capa visual / capa copy (evidencia DGX 2026-09-09,
`infra/creative/workflows/README.md`): el prompt de imagen viene siempre de
`build_visual_prompt` sobre la descripcion de un plano — nunca del copy
exacto del anuncio. El copy lo compone `ComposeBanner` despues.

**Cascada de candidatos 2026-09-09** (`RendererSelector.candidates_for`,
`hermes_tool_renderer.py`): para cada intencion se prueban los
renderizadores registrados en orden de `RendererTier` — delegado primero
(siempre lanza `RendererDelegationRequiredError`, ver docstring de
`hermes_tool_renderer.py`), proveedor directo despues (solo si esta
inyectado, es decir, solo si el propietario configuro una clave), local al
final (normalmente ausente del registro por defecto). Si la cascada se
agota sin producir un activo, `ImageGenerationUnavailableError` lleva un
mensaje accionable — nunca una degradacion silenciosa
(`creative-port.md`, correccion del propietario 2026-09-09)."""

from __future__ import annotations

from collections.abc import Mapping, Sequence

import structlog

from safent_ads.creative.application.delegation import RendererDelegationRequiredError
from safent_ads.creative.application.errors import (
    CreativeBriefNotFoundError,
    ImageGenerationUnavailableError,
    RenderBudgetExceededError,
    RenderQuotaExceededError,
)
from safent_ads.creative.application.ports import (
    CreativeAssetRepository,
    CreativeBriefRepository,
    CreativeJobRepository,
    GpuLeasePort,
    ImageRendererPort,
    render_call_scope,
)
from safent_ads.creative.domain.brief import CreativeBrief
from safent_ads.creative.domain.creative_asset import CreativeAsset, Provenance
from safent_ads.creative.domain.creative_job import CreativeJob, CreativeJobIdempotencyKey
from safent_ads.creative.domain.enums import (
    Format,
    GenerationStatus,
    JobWeight,
    RendererIntent,
    RendererName,
)
from safent_ads.creative.domain.identifiers import AssetId, BriefId, JobId
from safent_ads.creative.domain.money import Money
from safent_ads.creative.domain.render_specs import ImageSpec, RenderedAsset
from safent_ads.creative.domain.renderer_selector import MODEL_NAME_BY_RENDERER, RendererSelector
from safent_ads.creative.domain.visual_prompt import build_visual_prompt

logger = structlog.get_logger(__name__)

_DEFAULT_GPU_TIMEOUT_S = 120
_DEFAULT_VARIANT_FORMAT = Format.SQUARE_1080


class GenerateCreativeAssets:
    def __init__(
        self,
        *,
        briefs: CreativeBriefRepository,
        jobs: CreativeJobRepository,
        assets: CreativeAssetRepository,
        image_renderers: Mapping[RendererName, ImageRendererPort],
        renderer_selector: RendererSelector,
        gpu_lease: GpuLeasePort,
        max_cost_per_variant: Money | None = None,
        gpu_timeout_s: int = _DEFAULT_GPU_TIMEOUT_S,
    ) -> None:
        self._briefs = briefs
        self._jobs = jobs
        self._assets = assets
        self._image_renderers = image_renderers
        self._renderer_selector = renderer_selector
        self._gpu_lease = gpu_lease
        self._max_cost_per_variant = max_cost_per_variant
        self._gpu_timeout_s = gpu_timeout_s

    async def execute(self, brief_id: BriefId) -> Sequence[JobId]:
        brief = await self._briefs.get(brief_id)
        if brief is None:
            raise CreativeBriefNotFoundError(str(brief_id))
        brief_hash = brief.content_hash()
        return [
            await self._generate_variant(brief, brief_id, brief_hash, variant_index)
            for variant_index in range(brief.variant_count)
        ]

    async def _generate_variant(
        self, brief: CreativeBrief, brief_id: BriefId, brief_hash: str, variant_index: int
    ) -> JobId:
        key = CreativeJobIdempotencyKey(brief_hash=brief_hash, variant_index=variant_index)
        existing = await self._jobs.get_by_idempotency_key(key)
        if existing is not None:
            logger.info("creative_job_reused", job_id=str(existing.job_id), key=str(key))
            return existing.job_id

        job = CreativeJob(
            job_id=JobId.new(),
            business_id=brief.business_id,
            brief_id=brief_id,
            idempotency_key=key,
        )
        await self._jobs.add(job)
        try:
            await self._render_variant(brief, brief_id, variant_index, job)
        except Exception as exc:
            job.mark_failed(str(exc))
            await self._jobs.update(job)
            logger.warning("creative_job_failed", job_id=str(job.job_id), reason=str(exc))
            raise
        return job.job_id

    async def _render_variant(
        self, brief: CreativeBrief, brief_id: BriefId, variant_index: int, job: CreativeJob
    ) -> None:
        job.start_rendering()
        await self._jobs.update(job)
        rendered = await self._render_image(brief, variant_index)

        job.start_composing()
        await self._jobs.update(job)
        job.start_checking()
        await self._jobs.update(job)

        asset = self._build_asset(brief, brief_id, variant_index, rendered)
        await self._assets.add(asset)
        job.mark_ready((asset.asset_id,))
        await self._jobs.update(job)

    async def _render_image(self, brief: CreativeBrief, variant_index: int) -> RenderedAsset:
        shot = brief.shots[variant_index % len(brief.shots)]
        prompt = build_visual_prompt(shot.description)
        spec = ImageSpec(
            prompt=prompt.text,
            reference_assets=(),
            format=_DEFAULT_VARIANT_FORMAT,
            seed=variant_index,
            brand_kit=brief.brand_kit,
        )
        candidates = self._renderer_selector.candidates_for(RendererIntent.IMAGE_WITH_TEXT)
        # M-2 (revision de seguridad 0.2.22): `business_id` viaja al broker
        # via contextvar, no via `ImageSpec` (`creative.application.ports.
        # render_call_scope`) -- `RenderImageService` lo exige para aplicar
        # cuota por negocio, fail closed si falta.
        with render_call_scope(str(brief.business_id)):
            rendered = await self._render_first_available(candidates, spec)
        self._require_within_budget(rendered)
        return rendered

    def _require_within_budget(self, rendered: RenderedAsset) -> None:
        budget = self._max_cost_per_variant
        if budget is not None and rendered.cost_estimate > budget:
            raise RenderBudgetExceededError(f"{rendered.cost_estimate} supera max_cost={budget}")

    async def _render_first_available(
        self, candidates: Sequence[RendererName], spec: ImageSpec
    ) -> RenderedAsset:
        """Recorre `candidates` (delegado -> proveedor directo -> local) y
        devuelve el primero que complete. Un candidato sin adaptador
        inyectado (sin clave configurada) se salta en silencio; uno que
        lanza `RendererDelegationRequiredError` o cualquier otro fallo se
        registra y se intenta el siguiente — nunca se detiene la cascada
        por un solo fallo (creative-port.md correccion 2026-09-09)."""
        attempted: list[str] = []
        for renderer_name in candidates:
            renderer = self._image_renderers.get(renderer_name)
            if renderer is None:
                continue
            outcome = await self._try_render(renderer_name, renderer, spec, attempted)
            if outcome is not None:
                return outcome
        raise ImageGenerationUnavailableError(self._unavailable_message(attempted))

    async def _try_render(
        self,
        renderer_name: RendererName,
        renderer: ImageRendererPort,
        spec: ImageSpec,
        attempted: list[str],
    ) -> RenderedAsset | None:
        try:
            return await self._render_via(renderer, spec)
        except RendererDelegationRequiredError as exc:
            attempted.append(f"{renderer_name.value} (delegado a '{exc.instruction.native_tool}')")
            logger.info(
                "creative_render_delegated",
                renderer=renderer_name.value,
                native_tool=exc.instruction.native_tool,
                arguments=dict(exc.instruction.arguments),
            )
            return None
        except (RenderQuotaExceededError, RenderBudgetExceededError):
            # M-2 (revision de seguridad 0.2.22): parada definitiva, nunca
            # "prueba el siguiente candidato" -- probar otro renderizador
            # no libera la cuota de ESTE negocio ni evita el tope de coste
            # (el mismo `business_id` los agoto), y seguir la cascada solo
            # ocultaria el motivo real tras `ImageGenerationUnavailableError`.
            raise
        except Exception as exc:  # noqa: BLE001 - cascada de candidatos, ver docstring
            attempted.append(f"{renderer_name.value} ({exc})")
            logger.warning(
                "creative_render_failed_trying_next_candidate",
                renderer=renderer_name.value,
                reason=str(exc),
            )
            return None

    async def _render_via(self, renderer: ImageRendererPort, spec: ImageSpec) -> RenderedAsset:
        lease = await self._gpu_lease.acquire(JobWeight.HEAVY, self._gpu_timeout_s)
        try:
            return await renderer.render(spec)
        finally:
            await self._gpu_lease.release(lease)

    def _unavailable_message(self, attempted: Sequence[str]) -> str:
        tried = "; ".join(attempted) if attempted else "ningun renderizador configurado"
        return (
            f"Generación de imagen no disponible ({tried}). Habilita `image_generate` "
            "en el overlay del Agente de Anuncios (delegado, sin coste adicional) o "
            "configura `OPENAI_API_KEY`/`FAL_API_KEY` en el broker (proveedor directo)."
        )

    def _build_asset(
        self,
        brief: CreativeBrief,
        brief_id: BriefId,
        variant_index: int,
        rendered: RenderedAsset,
    ) -> CreativeAsset:
        provenance = Provenance(
            renderer_used=rendered.renderer_used,
            model_name=MODEL_NAME_BY_RENDERER[rendered.renderer_used],
            seed=variant_index,
            brief_id=brief_id,
            source_signal_id=brief.source_signal_id,
            generation_status=GenerationStatus.MODEL_GENERATED,
            generated_at=rendered.generated_at,
        )
        return CreativeAsset(
            asset_id=AssetId.new(),
            business_id=brief.business_id,
            media_kind=rendered.media_kind,
            format=rendered.format,
            duration_seconds=rendered.duration_s,
            storage_uri=rendered.storage_uri,
            checksum=rendered.checksum,
            cost_estimate=rendered.cost_estimate,
            provenance=provenance,
        )

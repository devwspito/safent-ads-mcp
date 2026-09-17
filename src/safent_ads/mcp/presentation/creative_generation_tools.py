"""Las 2 herramientas MCP de `creative` que la revision final (B-1) marco
como no cableadas: `generate_creative_assets` y `run_creative_policy_check`
(`checklists/final-review.md`). Las otras 4 de `creative` (`list_creatives`,
`get_creative`, `list_creative_briefs`, `get_creative_job`) ya viven en
`catalog.py::_CATALOG` sobre `ReadModelPorts` -- esta lane no las toca.

`creative.presentation.mcp_tools.CreativeMcpTools` agrupa las 6 mas
`import_creative_asset` (7) sobre un unico constructor; esta lane no lo
reutiliza para no arrastrar `ImportCreativeAsset` (fuera de las 11 del
bloqueante) solo para satisfacer su `__init__`. En su lugar, este modulo
llama directamente a `GenerateCreativeAssets`/`RunPolicyCheck` (mismos
casos de uso, mismo resultado) con dos diferencias deliberadas frente al
REST (`creative/presentation/router.py`), exigidas por la regla 4 del
contrato ("todo argumento resuelve a un `business_id`", que MCP declara
explicito y REST deriva del `asset_id` de la ruta):

1. `business_id` viaja en el propio argumento (el REST lo toma de la
   entidad ya cargada); un validador rechaza un `brief.business_id`
   distinto al declarado antes de tocar ningun repositorio (evita que un
   llamador autorizado en un negocio cree un activo en otro).
2. `run_creative_policy_check` comprueba que el activo pertenezca al
   `business_id` declarado ANTES de correr el veredicto -- mismo patron
   IDOR-safe que `EntityNotFoundError` en el resto del catalogo: si no
   coincide, `ENTITY_NOT_FOUND`, nunca una diferenciacion que filtre
   existencia entre negocios (mismo criterio que
   `router.py::run_creative_policy_check`, que lo resuelve con
   `ensure_business_access` porque alli el `business_id` no es un
   argumento sino un dato ya cargado)."""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Any

from pydantic import Field, model_validator

from safent_ads.creative.application.errors import (
    CreativeAssetMissingCopyError,
    CreativeAssetNotFoundError,
    CreativeBriefNotFoundError,
    CreativeJobNotFoundError,
    ImageGenerationUnavailableError,
    RenderBudgetExceededError,
    RenderQuotaExceededError,
)
from safent_ads.creative.application.generate_creative_assets import GenerateCreativeAssets
from safent_ads.creative.application.ports import CreativeAssetRepository, CreativeBriefRepository
from safent_ads.creative.application.run_policy_check import RunPolicyCheck
from safent_ads.creative.domain.enums import Placement
from safent_ads.creative.domain.identifiers import AssetId, BriefId
from safent_ads.creative.presentation.payloads import (
    ULID_PATTERN,
    CreativeBriefPayload,
    brief_from_payload,
)
from safent_ads.mcp.application.caller_scope import CallerScope
from safent_ads.mcp.application.errors import (
    CreativeRenderBudgetExceededError,
    CreativeRendererUnavailableError,
    CreativeRenderQuotaExceededError,
    EntityNotFoundError,
    ToolValidationError,
)
from safent_ads.mcp.presentation.args import BusinessId, ToolArgs
from safent_ads.mcp.presentation.creative_upload_tools import (
    GENERATE_CREATIVE_ASSETS_FALLBACK_DESCRIPTION,
)
from safent_ads.mcp.presentation.registry import ToolClass, ToolDefinition
from safent_ads.shared.ids import BusinessId as BusinessIdVo
from safent_ads.shared.ids import PlatformCode

__all__ = ["CreativeGenerationToolServices", "build_creative_generation_tool_definitions"]

type Handler[ArgsT, ReturnT] = Callable[[ArgsT, CallerScope], Awaitable[ReturnT]]

# research/content-generation-stack.md: imagen ~15-40s por variante.
_ESTIMATED_SECONDS_PER_VARIANT = 45


@dataclass(frozen=True, slots=True)
class CreativeGenerationToolServices:
    """`composition/app.py` construye esto UNA vez y se lo pasa tanto a
    esta lane como a `_build_creative_router`: comparten el mismo
    `InProcessGpuQueue` que serializa el acceso a GPU (mismo
    `GenerateCreativeAssets`), nunca dos colas independientes para la
    misma superficie fisica."""

    briefs: CreativeBriefRepository
    assets: CreativeAssetRepository
    generate_creative_assets: GenerateCreativeAssets
    run_policy_check: RunPolicyCheck


class GenerateCreativeAssetsArgs(ToolArgs):
    business_id: BusinessId
    brief: CreativeBriefPayload

    @model_validator(mode="after")
    def _brief_matches_declared_business(self) -> GenerateCreativeAssetsArgs:
        # `BusinessIdVo.parse` normaliza el UUID (mayusculas/minusculas)
        # antes de comparar -- dos formas del mismo id nunca deben chocar
        # con este validador.
        if BusinessIdVo.parse(self.brief.business_id) != BusinessIdVo.parse(self.business_id):
            raise ValueError("business_id debe coincidir con brief.business_id")
        return self


class RunCreativePolicyCheckArgs(ToolArgs):
    business_id: BusinessId
    asset_id: str = Field(pattern=ULID_PATTERN)
    platform: PlatformCode
    placement: Placement


def build_creative_generation_tool_definitions(
    services: CreativeGenerationToolServices,
) -> list[ToolDefinition[Any]]:
    return [
        ToolDefinition(
            name="generate_creative_assets",
            description=GENERATE_CREATIVE_ASSETS_FALLBACK_DESCRIPTION,
            args_model=GenerateCreativeAssetsArgs,
            tool_class=ToolClass.PROPOSAL,
            handler=_generate_creative_assets(services),
            business_id_of=_by_business_id,
        ),
        ToolDefinition(
            name="run_creative_policy_check",
            description="Normas de Meta/Google sobre un activo ya existente antes de proponerlo.",
            args_model=RunCreativePolicyCheckArgs,
            tool_class=ToolClass.READ,
            handler=_run_creative_policy_check(services),
            business_id_of=_by_business_id,
        ),
    ]


def _generate_creative_assets(
    services: CreativeGenerationToolServices,
) -> Handler[GenerateCreativeAssetsArgs, dict[str, object]]:
    async def handler(
        args: GenerateCreativeAssetsArgs, _caller_scope: CallerScope
    ) -> dict[str, object]:
        brief = brief_from_payload(args.brief)
        brief_id = BriefId.new()
        await services.briefs.add(brief_id, brief)
        try:
            job_ids = await services.generate_creative_assets.execute(brief_id)
        except ImageGenerationUnavailableError as exc:
            # correccion del propietario 2026-09-09: capacidad reportada,
            # nunca fingida -- mismo mensaje accionable que
            # `router.py::_renderer_unavailable` (REST, 409).
            raise CreativeRendererUnavailableError(str(exc)) from exc
        except RenderQuotaExceededError as exc:
            raise CreativeRenderQuotaExceededError(str(exc)) from exc
        except RenderBudgetExceededError as exc:
            raise CreativeRenderBudgetExceededError(str(exc)) from exc
        return {
            "job_id": str(job_ids[0]),
            "estimated_seconds": _ESTIMATED_SECONDS_PER_VARIANT * len(job_ids),
        }

    return handler


def _run_creative_policy_check(
    services: CreativeGenerationToolServices,
) -> Handler[RunCreativePolicyCheckArgs, dict[str, object]]:
    async def handler(
        args: RunCreativePolicyCheckArgs, _caller_scope: CallerScope
    ) -> dict[str, object]:
        asset_id = AssetId.parse(args.asset_id)
        asset = await services.assets.get(asset_id)
        # Comparar `BusinessId` (no el string crudo): `uuid.UUID` normaliza
        # mayusculas/minusculas, y `ToolArgs` solo valida el patron, nunca
        # el caso -- comparar strings dejaria pasar un falso negativo para
        # el dueño legitimo si envia el UUID en mayusculas.
        if asset is None or asset.business_id != BusinessIdVo.parse(args.business_id):
            raise EntityNotFoundError(args.asset_id)
        try:
            verdict = await services.run_policy_check.execute(
                asset_id, args.platform, args.placement
            )
        except (
            CreativeAssetNotFoundError,
            CreativeBriefNotFoundError,
            CreativeJobNotFoundError,
        ) as exc:
            raise EntityNotFoundError(args.asset_id) from exc
        except CreativeAssetMissingCopyError as exc:
            raise ToolValidationError(str(exc)) from exc
        return {
            "verdict": verdict.verdict.value,
            "findings": [
                {"code": f.code, "severity": f.severity.value, "human_message": f.human_message}
                for f in verdict.findings
            ],
        }

    return handler


def _by_business_id(args: Any) -> str:  # noqa: ANN401 - extractor generico por posicion
    return str(args.business_id)

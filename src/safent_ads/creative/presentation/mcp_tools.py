"""Herramientas MCP de `creative` (contracts/mcp-tools.md): funciones de
manejador puras en pydantic estricto — enums cerrados, IDs con patron, sin
URLs libres (threat-model.md C-11) — que el `ToolRegistry` del carril de
superficie (US1, todavia no implementado en este snapshot) registrara.
Este modulo no monta nada: no hay `ToolRegistry` aqui.

Lecturas auto-ejecutables: `list_creatives`, `get_creative`,
`list_creative_briefs`, `get_creative_job`. `generate_creative_assets` y
`run_creative_policy_check` no son lecturas (verbo `generate_`/`run_`) pero
tampoco tocan una plataforma (contracts/mcp-tools.md regla 3): la primera
encola un trabajo GPU, la segunda evalua normas sobre un activo ya existente.

`import_creative_asset` es la UNICA excepcion deliberada a "sin URLs
libres": es literalmente su proposito, traer el activo que una herramienta
nativa del agente acaba de producir (`application/delegation.py`,
`infrastructure/hermes_tool_renderer.py`). El patron del campo exige
`https://` como defensa en profundidad en la propia superficie; el
allow-list de host real vive en `application/import_creative_asset.py`
(threat-model.md C-11/C-12)."""

from __future__ import annotations

from pydantic import Field

from safent_ads.creative.application.errors import (
    CreativeAssetNotFoundError,
    CreativeBriefNotFoundError,
    CreativeJobNotFoundError,
)
from safent_ads.creative.application.generate_creative_assets import GenerateCreativeAssets
from safent_ads.creative.application.import_creative_asset import (
    ImportCreativeAsset,
    ImportCreativeAssetRequest,
)
from safent_ads.creative.application.ports import (
    CreativeAssetRepository,
    CreativeBriefRepository,
    CreativeJobRepository,
)
from safent_ads.creative.application.run_policy_check import RunPolicyCheck
from safent_ads.creative.domain.creative_asset import CreativeAsset
from safent_ads.creative.domain.creative_job import CreativeJob
from safent_ads.creative.domain.enums import MediaKind, Placement
from safent_ads.creative.domain.identifiers import AssetId, BriefId, JobId
from safent_ads.creative.presentation.payloads import (
    ULID_PATTERN,
    UUID_PATTERN,
    CreativeBriefPayload,
    StrictModel,
    brief_from_payload,
)
from safent_ads.shared.ids import BusinessId, PlatformCode

_ESTIMATED_SECONDS_PER_VARIANT = 45  # research/content-generation-stack.md: imagen ~15-40s
_HTTPS_URL_PATTERN = r"^https://[^\s]+$"
_MAX_SOURCE_URL_LEN = 2000
_MAX_NATIVE_TOOL_LEN = 100


class GenerateCreativeAssetsArgs(StrictModel):
    brief: CreativeBriefPayload


class GenerateCreativeAssetsResult(StrictModel):
    job_id: str
    estimated_seconds: int


class GetCreativeArgs(StrictModel):
    asset_id: str = Field(pattern=ULID_PATTERN)


class ListCreativesArgs(StrictModel):
    business_id: str = Field(pattern=UUID_PATTERN)
    media_kind: MediaKind | None = None
    limit: int = Field(default=50, ge=1, le=200)
    cursor: str | None = Field(default=None, max_length=200)


class ListCreativeBriefsArgs(StrictModel):
    business_id: str = Field(pattern=UUID_PATTERN)


class GetCreativeJobArgs(StrictModel):
    job_id: str = Field(pattern=ULID_PATTERN)


class RunCreativePolicyCheckArgs(StrictModel):
    asset_id: str = Field(pattern=ULID_PATTERN)
    platform: PlatformCode
    placement: Placement


class ImportCreativeAssetArgs(StrictModel):
    brief_id: str = Field(pattern=ULID_PATTERN)
    source_url: str = Field(pattern=_HTTPS_URL_PATTERN, max_length=_MAX_SOURCE_URL_LEN)
    media_kind: MediaKind
    native_tool_used: str = Field(min_length=1, max_length=_MAX_NATIVE_TOOL_LEN)


class ImportCreativeAssetResult(StrictModel):
    asset_id: str
    checksum: str


def _asset_summary(asset_id: AssetId, asset: CreativeAsset) -> dict[str, object]:
    """Forma de `contracts/mcp-tools.md` (distinta de la de `rest-api.md`
    que usa `router.py`/`presentation/serializers.py`, ver docstring de ese
    modulo): local a esta superficie, todavia sin `ToolRegistry` que la
    invoque."""
    verdict = asset.policy_verdict
    return {
        "asset_id": str(asset_id),
        "media_kind": asset.media_kind.value,
        "format": asset.format.value if asset.format is not None else None,
        "policy_verdict": verdict.verdict.value if verdict else None,
    }


def _asset_detail(asset_id: AssetId, asset: CreativeAsset) -> dict[str, object]:
    detail = _asset_summary(asset_id, asset)
    detail["signal_id"] = (
        str(asset.provenance.source_signal_id) if asset.provenance.source_signal_id else None
    )
    detail["state"] = asset.state.value
    detail["brief_id"] = str(asset.provenance.brief_id)
    detail["renderer_used"] = asset.provenance.renderer_used.value
    detail["cost_estimate"] = {
        "amount": str(asset.cost_estimate.amount),
        "currency": asset.cost_estimate.currency,
    }
    return detail


def _job_detail(job_id: JobId, job: CreativeJob) -> dict[str, object]:
    return {
        "job_id": str(job_id),
        "state": job.state.value,
        "progress": job.progress,
        "assets": [str(asset_id) for asset_id in job.asset_ids],
        "failure_reason": job.failure_reason,
    }


class CreativeMcpTools:
    """Agrupa las dependencias de los manejadores; el `ToolRegistry` real
    invoca cada metodo por nombre tras autorizar `business_id`
    (`RequireBusinessAccess`, contracts/mcp-tools.md regla 4 — fuera de
    este carril)."""

    def __init__(
        self,
        *,
        briefs: CreativeBriefRepository,
        jobs: CreativeJobRepository,
        assets: CreativeAssetRepository,
        generate_creative_assets: GenerateCreativeAssets,
        run_policy_check: RunPolicyCheck,
        import_creative_asset: ImportCreativeAsset,
    ) -> None:
        self._briefs = briefs
        self._jobs = jobs
        self._assets = assets
        self._generate_creative_assets = generate_creative_assets
        self._run_policy_check = run_policy_check
        self._import_creative_asset = import_creative_asset

    async def generate_creative_assets(
        self, args: GenerateCreativeAssetsArgs
    ) -> GenerateCreativeAssetsResult:
        brief = brief_from_payload(args.brief)
        brief_id = BriefId.new()
        await self._briefs.add(brief_id, brief)
        job_ids = await self._generate_creative_assets.execute(brief_id)
        return GenerateCreativeAssetsResult(
            job_id=str(job_ids[0]),
            estimated_seconds=_ESTIMATED_SECONDS_PER_VARIANT * len(job_ids),
        )

    async def get_creative(self, args: GetCreativeArgs) -> dict[str, object]:
        asset_id = AssetId.parse(args.asset_id)
        asset = await self._assets.get(asset_id)
        if asset is None:
            raise CreativeAssetNotFoundError(args.asset_id)
        return _asset_detail(asset_id, asset)

    async def list_creatives(self, args: ListCreativesArgs) -> list[dict[str, object]]:
        assets = await self._assets.list_for_business(
            BusinessId.parse(args.business_id), media_kind=args.media_kind
        )
        return [_asset_summary(asset.asset_id, asset) for asset in assets]

    async def list_creative_briefs(self, args: ListCreativeBriefsArgs) -> list[dict[str, object]]:
        briefs = await self._briefs.list_for_business(BusinessId.parse(args.business_id))
        return [
            {"brief_id": str(brief_id), "objective": brief.objective.value}
            for brief_id, brief in briefs
        ]

    async def get_creative_job(self, args: GetCreativeJobArgs) -> dict[str, object]:
        job_id = JobId.parse(args.job_id)
        job = await self._jobs.get(job_id)
        if job is None:
            raise CreativeJobNotFoundError(args.job_id)
        return _job_detail(job_id, job)

    async def run_creative_policy_check(
        self, args: RunCreativePolicyCheckArgs
    ) -> dict[str, object]:
        verdict = await self._run_policy_check.execute(
            AssetId.parse(args.asset_id), args.platform, args.placement
        )
        return {
            "verdict": verdict.verdict.value,
            "findings": [
                {"code": f.code, "severity": f.severity.value, "human_message": f.human_message}
                for f in verdict.findings
            ],
        }

    async def import_creative_asset(
        self, args: ImportCreativeAssetArgs
    ) -> ImportCreativeAssetResult:
        brief_id = BriefId.parse(args.brief_id)
        brief = await self._briefs.get(brief_id)
        if brief is None:
            raise CreativeBriefNotFoundError(args.brief_id)
        request = ImportCreativeAssetRequest(
            business_id=brief.business_id,
            brief_id=brief_id,
            source_signal_id=brief.source_signal_id,
            source_url=args.source_url,
            media_kind=args.media_kind,
            native_tool_used=args.native_tool_used,
        )
        asset_id = await self._import_creative_asset.execute(request)
        asset = await self._assets.get(asset_id)
        if asset is None:
            raise CreativeAssetNotFoundError(str(asset_id))
        return ImportCreativeAssetResult(asset_id=str(asset_id), checksum=asset.checksum)


__all__ = [
    "CreativeBriefNotFoundError",
    "CreativeMcpTools",
    "GenerateCreativeAssetsArgs",
    "GenerateCreativeAssetsResult",
    "GetCreativeArgs",
    "GetCreativeJobArgs",
    "ImportCreativeAssetArgs",
    "ImportCreativeAssetResult",
    "ListCreativeBriefsArgs",
    "ListCreativesArgs",
    "RunCreativePolicyCheckArgs",
]

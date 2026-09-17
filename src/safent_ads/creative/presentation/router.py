"""Router REST de `creative` (contracts/rest-api.md §Creatividades). Monta
las 3 lecturas mas las 5 escrituras que el contrato agrupa con ellas —
ninguna publica ni toca una plataforma, FR-33: `POST /creative-jobs` solo
encola un trabajo (proveedor cloud o delegado, nunca GPU local por
defecto), `POST /creatives/{id}/policy-check` solo evalua normas sobre un
activo ya existente, `POST /creatives/{id}/reject`/`regenerate` son estado
NUESTRO (nunca tocan plataforma) y `POST /creatives/{id}/propose-publication`
crea una `Proposal` pendiente de aprobacion humana, jamas publica ella
misma.

`require_business_access`/`ensure_business_access`
(`panel.presentation.deps`, mismo patron que `composition/execution_rest.py`)
autorizan CADA ruta: `GET /creatives` (unico endpoint con `business_id` de
query) usa `require_business_access`; el resto resuelve el recurso por su
`{id}` de ruta PRIMERO y comprueba `ensure_business_access` contra el
`business_id` que ya trae ese recurso — nunca un 403, siempre 404 (IDOR,
threat-model.md C-27). Unica excepcion: `GET /creative-previews/{key}`
(gap-creative-preview) — `StorageUri.key` no codifica `business_id`, asi
que solo exige `CallerDep` (sesion valida); la firma HMAC + TTL de
`signed_preview_url` es la puerta real de ESE recurso."""

from __future__ import annotations

import hashlib
from typing import Annotated

import structlog
from fastapi import APIRouter, Depends, Query, Request, Response, status
from pydantic import Field
from sqlalchemy.exc import IntegrityError

from safent_ads.creative.application.errors import (
    AdSetReferenceNotFoundError,
    CreativeAssetMissingCopyError,
    CreativeAssetNotFoundError,
    CreativeAssetPolicyCheckRequiredError,
    CreativeBriefNotFoundError,
    CreativeJobNotFoundError,
    ImageGenerationUnavailableError,
)
from safent_ads.creative.application.generate_creative_assets import GenerateCreativeAssets
from safent_ads.creative.application.ports import AssetStorePort, CreativeBriefRepository
from safent_ads.creative.application.ports import CreativeAssetRepository as CreativeAssetRepo
from safent_ads.creative.application.ports import CreativeJobRepository as CreativeJobRepo
from safent_ads.creative.application.propose_creative import ProposeCreative, ProposeCreativeCommand
from safent_ads.creative.application.run_policy_check import RunPolicyCheck
from safent_ads.creative.domain.creative_asset import (
    CreativeAsset,
    InvalidCreativeAssetTransitionError,
)
from safent_ads.creative.domain.creative_job import CreativeJob
from safent_ads.creative.domain.enums import MediaKind, Placement, ReviewState
from safent_ads.creative.domain.identifiers import AssetId, BriefId, JobId
from safent_ads.creative.infrastructure.creative_preview_content_type import (
    PreviewableCreativeAsset,
    UnpreviewableCreativeAssetError,
    resolve_creative_preview_content_type,
)
from safent_ads.creative.infrastructure.local_asset_storage import PreviewLinkRejectedError
from safent_ads.creative.presentation.payloads import (
    ULID_PATTERN,
    CreativeBriefPayload,
    ProposePublicationRequest,
    RegenerateCreativeRequest,
    RejectCreativeRequest,
    StrictModel,
    brief_from_payload,
    proposed_ad_copy_from_payload,
)
from safent_ads.creative.presentation.serializers import (
    asset_json,
    job_json,
    outcome_to_zod_signal,
    policy_findings_to_zod,
    policy_verdict_to_zod,
)
from safent_ads.iam.presentation.errors import ApiError
from safent_ads.panel.presentation.deps import (
    AuthenticatedCaller,
    CallerDep,
    ensure_business_access,
    require_business_access,
)
from safent_ads.shared.clock import Clock
from safent_ads.shared.ids import BusinessId, PlatformCode

logger = structlog.get_logger(__name__)

_PREVIEW_TTL_S = 600  # 10 minutos, rest-api.md
_DEFAULT_LABEL = "Creatividad"
_TYPED_CONFIRMATION_PUBLISH = "PUBLICAR"
_POLICY_VERDICT_QUERY_PATTERN = "^(PASS|FAIL|PENDING)$"
_SIGNAL_QUERY_PATTERN = "^(FATIGUE|WINNER|LOSER|LEARNING)$"
_UNPREVIEWABLE_ASSET_MESSAGE = "Este activo no se puede previsualizar."


def _not_found() -> ApiError:
    return ApiError(
        status_code=status.HTTP_404_NOT_FOUND, code="NOT_FOUND", message="No encontrado."
    )


def _unsupported_asset_type() -> ApiError:
    return ApiError(
        status_code=status.HTTP_415_UNSUPPORTED_MEDIA_TYPE,
        code="UNSUPPORTED_ASSET_TYPE",
        message=_UNPREVIEWABLE_ASSET_MESSAGE,
    )


def _validation_error(code: str, message: str) -> ApiError:
    return ApiError(status_code=status.HTTP_422_UNPROCESSABLE_CONTENT, code=code, message=message)


def _conflict(code: str, message: str) -> ApiError:
    return ApiError(status_code=status.HTTP_409_CONFLICT, code=code, message=message)


def _renderer_unavailable(exc: ImageGenerationUnavailableError) -> ApiError:
    """FR-33 + correccion del propietario 2026-09-09 ("capability is
    reported, never faked"): sin proveedor configurado, `ads-api` no tiene
    ningun `ImageRendererPort` inyectado (BYOK vive en `ads-broker`,
    threat-model.md C-29 -- este proceso nunca ve esas claves) y la
    cascada de `RendererSelector` se agota siempre. El mensaje de
    `ImageGenerationUnavailableError` ya trae que se probo y como
    habilitarlo; viaja tal cual como "informe de capacidad", nunca
    resumido ni escondido."""
    return ApiError(
        status_code=status.HTTP_409_CONFLICT,
        code="CREATIVE_RENDERER_UNAVAILABLE",
        message=str(exc),
        details={"capability_report": str(exc)},
    )


class CreateCreativeJobRequest(StrictModel):
    brief: CreativeBriefPayload


class CreateCreativeJobResponse(StrictModel):
    job_id: str


class PolicyCheckRequest(StrictModel):
    platform: PlatformCode
    placement: Placement


async def _label_for(asset: CreativeAsset, briefs: CreativeBriefRepository) -> str:
    """rest-api.md: "`asset_id` no es texto para un humano; lo genera el
    brief" -- el `hook` del brief de origen es lo unico que un propietario
    reconoce a simple vista en la galeria."""
    brief = await briefs.get(asset.provenance.brief_id)
    return brief.hook if brief is not None else _DEFAULT_LABEL


async def _asset_row(
    asset: CreativeAsset, briefs: CreativeBriefRepository, asset_store: AssetStorePort
) -> tuple[CreativeAsset, str, str]:
    label = await _label_for(asset, briefs)
    preview_url = await asset_store.signed_preview_url(asset.storage_uri, _PREVIEW_TTL_S)
    return asset, label, preview_url


def _matches_policy_verdict(asset: CreativeAsset, wanted: str | None) -> bool:
    return wanted is None or policy_verdict_to_zod(asset.policy_verdict) == wanted


def _matches_signal(asset: CreativeAsset, wanted: str | None) -> bool:
    return wanted is None or outcome_to_zod_signal(asset.outcome) == wanted


def _matches_pending_approval(asset: CreativeAsset, wanted: bool | None) -> bool:
    if wanted is None:
        return True
    return (asset.review_state == ReviewState.PENDING) == wanted


def build_creative_router(  # noqa: PLR0915 - raiz de router, cada handler es de 5-10 lineas
    *,
    briefs: CreativeBriefRepository,
    assets: CreativeAssetRepo,
    jobs: CreativeJobRepo,
    asset_store: AssetStorePort,
    generate_creative_assets: GenerateCreativeAssets,
    run_policy_check: RunPolicyCheck,
    propose_creative: ProposeCreative,
    clock: Clock,
) -> APIRouter:
    """Cablea las dependencias en closures: `composition` inyecta los
    adaptadores reales (SQL, proveedores cloud, etc.) sin que este modulo
    conozca su implementacion (DIP). Crea un `APIRouter` nuevo en cada
    llamada — nunca comparte uno a nivel de modulo — para que llamarla mas
    de una vez (p.ej. en tests) no registre las rutas por duplicado."""
    router = APIRouter(prefix="/api/v1", tags=["creatives"])

    @router.get("/creatives")
    async def list_creatives(
        business_id: Annotated[str, Depends(require_business_access)],
        media_kind: MediaKind | None = None,
        policy_verdict: Annotated[str | None, Query(pattern=_POLICY_VERDICT_QUERY_PATTERN)] = None,
        signal: Annotated[str | None, Query(pattern=_SIGNAL_QUERY_PATTERN)] = None,
        pending_approval: bool | None = None,
    ) -> dict[str, object]:
        found = await assets.list_for_business(BusinessId.parse(business_id), media_kind=media_kind)
        filtered = [
            asset
            for asset in found
            if _matches_policy_verdict(asset, policy_verdict)
            and _matches_signal(asset, signal)
            and _matches_pending_approval(asset, pending_approval)
        ]
        now = clock.now()
        items = [
            asset_json(asset.asset_id, asset, label=label, preview_url=preview_url, now=now)
            for asset, label, preview_url in [
                await _asset_row(asset, briefs, asset_store) for asset in filtered
            ]
        ]
        return {"items": items}

    @router.get("/creatives/{asset_id}")
    async def get_creative(
        asset_id: Annotated[str, Field(pattern=ULID_PATTERN)], caller: CallerDep
    ) -> dict[str, object]:
        parsed_id = AssetId.parse(asset_id)
        asset = await assets.get(parsed_id)
        if asset is None:
            raise _not_found()
        ensure_business_access(str(asset.business_id), caller)
        _, label, preview_url = await _asset_row(asset, briefs, asset_store)
        return asset_json(parsed_id, asset, label=label, preview_url=preview_url, now=clock.now())

    @router.post("/creative-jobs", status_code=status.HTTP_202_ACCEPTED)
    async def create_creative_job(
        body: CreateCreativeJobRequest, caller: CallerDep
    ) -> CreateCreativeJobResponse:
        try:
            brief = brief_from_payload(body.brief)
        except ValueError as exc:
            raise _validation_error("VALIDATION_ERROR", str(exc)) from exc
        ensure_business_access(str(brief.business_id), caller)
        brief_id = BriefId.new()
        try:
            await briefs.add(brief_id, brief)
        except IntegrityError as exc:
            # `creative_briefs.business_id` referencia `businesses(id)`:
            # un `business_id` que no existe llega hasta aqui, nunca antes
            # (el cuerpo no trae ningun otro dato que confirme su
            # existencia) -- el CHECK de la base es quien de verdad decide.
            raise _not_found() from exc
        try:
            job_ids = await generate_creative_assets.execute(brief_id)
        except ImageGenerationUnavailableError as exc:
            raise _renderer_unavailable(exc) from exc
        return CreateCreativeJobResponse(job_id=str(job_ids[0]))

    @router.get("/creative-jobs/{job_id}")
    async def get_creative_job(
        job_id: Annotated[str, Field(pattern=ULID_PATTERN)], caller: CallerDep
    ) -> dict[str, object]:
        parsed_id = JobId.parse(job_id)
        job = await jobs.get(parsed_id)
        if job is None:
            raise _not_found()
        ensure_business_access(str(job.business_id), caller)
        return await _job_response(job, assets, briefs, asset_store, clock)

    @router.get("/creative-previews/{key:path}")
    async def get_creative_preview(
        key: str, exp: int, sig: str, request: Request, _caller: CallerDep
    ) -> Response:
        """Sirve los bytes que emite `signed_preview_url` (`_asset_row`
        arriba): `_caller` solo exige sesion valida, no acceso a un
        `business_id` -- `StorageUri.key` (`media_kind/ULID.ext`) no
        codifica negocio ni propietario (modelo de un unico propietario,
        `panel.presentation.deps`), asi que la firma HMAC + la cookie de
        sesion son la unica puerta, a proposito."""
        try:
            payload = await asset_store.open_preview(key, exp, sig)
        except PreviewLinkRejectedError as exc:
            raise _not_found() from exc
        try:
            resolved = resolve_creative_preview_content_type(payload)
        except UnpreviewableCreativeAssetError as exc:
            raise _unsupported_asset_type() from exc
        remaining_ttl_s = max(exp - int(clock.now().timestamp()), 0)
        return _preview_response(
            resolved,
            if_none_match=request.headers.get("if-none-match"),
            max_age_s=remaining_ttl_s,
        )

    @router.post("/creatives/{asset_id}/policy-check")
    async def run_creative_policy_check(
        asset_id: Annotated[str, Field(pattern=ULID_PATTERN)],
        body: PolicyCheckRequest,
        caller: CallerDep,
    ) -> dict[str, object]:
        parsed_id = AssetId.parse(asset_id)
        existing = await assets.get(parsed_id)
        if existing is None:
            raise _not_found()
        ensure_business_access(str(existing.business_id), caller)
        try:
            verdict = await run_policy_check.execute(parsed_id, body.platform, body.placement)
        except (
            CreativeAssetNotFoundError,
            CreativeBriefNotFoundError,
            CreativeJobNotFoundError,
        ) as exc:
            raise _not_found() from exc
        except CreativeAssetMissingCopyError as exc:
            raise _validation_error(
                "VALIDATION_ERROR", "El activo no tiene copy compuesto todavia."
            ) from exc
        return {
            "verdict": policy_verdict_to_zod(verdict),
            "findings": policy_findings_to_zod(verdict),
        }

    @router.post("/creatives/{asset_id}/reject")
    async def reject_creative(
        asset_id: Annotated[str, Field(pattern=ULID_PATTERN)],
        body: RejectCreativeRequest,
        caller: CallerDep,
    ) -> dict[str, object]:
        parsed_id = AssetId.parse(asset_id)
        asset = await assets.get(parsed_id)
        if asset is None:
            raise _not_found()
        ensure_business_access(str(asset.business_id), caller)
        try:
            asset.reject()
        except InvalidCreativeAssetTransitionError as exc:
            raise _conflict("INVALID_CREATIVE_STATE", str(exc)) from exc
        await assets.update(asset)
        logger.info("creative_rejected", asset_id=asset_id, reason=body.reason)
        return {"asset_id": asset_id, "review_state": "rejected"}

    @router.post("/creatives/{asset_id}/regenerate", status_code=status.HTTP_202_ACCEPTED)
    async def regenerate_creative(
        asset_id: Annotated[str, Field(pattern=ULID_PATTERN)],
        body: RegenerateCreativeRequest,
        caller: CallerDep,
    ) -> CreateCreativeJobResponse:
        parsed_id = AssetId.parse(asset_id)
        asset = await assets.get(parsed_id)
        if asset is None:
            raise _not_found()
        ensure_business_access(str(asset.business_id), caller)
        logger.info("creative_regenerate_requested", asset_id=asset_id, reason=body.reason)
        try:
            job_ids = await generate_creative_assets.execute(asset.provenance.brief_id)
        except CreativeBriefNotFoundError as exc:
            raise _not_found() from exc
        except ImageGenerationUnavailableError as exc:
            raise _renderer_unavailable(exc) from exc
        return CreateCreativeJobResponse(job_id=str(job_ids[0]))

    @router.post("/creatives/{asset_id}/propose-publication", status_code=status.HTTP_201_CREATED)
    async def propose_publication(
        asset_id: Annotated[str, Field(pattern=ULID_PATTERN)],
        body: ProposePublicationRequest,
        caller: CallerDep,
    ) -> dict[str, object]:
        parsed_id = AssetId.parse(asset_id)
        asset = await assets.get(parsed_id)
        if asset is None:
            raise _not_found()
        ensure_business_access(str(asset.business_id), caller)
        if body.typed_confirmation.strip().upper() != _TYPED_CONFIRMATION_PUBLISH:
            raise _validation_error(
                "TYPED_CONFIRMATION_REQUIRED",
                f'Escribe "{_TYPED_CONFIRMATION_PUBLISH}" para confirmar.',
            )
        try:
            result = await propose_creative.execute(
                ProposeCreativeCommand(
                    asset_id=parsed_id,
                    ad_set_ref=body.ad_set_ref,
                    ad_copy=proposed_ad_copy_from_payload(body.ad_copy),
                    extra_asset_ids=tuple(AssetId.parse(a) for a in body.extra_asset_ids),
                )
            )
        except CreativeAssetPolicyCheckRequiredError as exc:
            raise _validation_error("POLICY_CHECK_REQUIRED", str(exc)) from exc
        except (CreativeAssetNotFoundError, AdSetReferenceNotFoundError) as exc:
            raise _not_found() from exc
        except InvalidCreativeAssetTransitionError as exc:
            raise _conflict("INVALID_CREATIVE_STATE", str(exc)) from exc
        return {
            "proposal_id": result.proposal_id,
            "diff_hash": result.diff_hash,
            "expires_at": result.expires_at.isoformat(),
        }

    return router


async def _job_response(
    job: CreativeJob,
    assets: CreativeAssetRepo,
    briefs: CreativeBriefRepository,
    asset_store: AssetStorePort,
    clock: Clock,
) -> dict[str, object]:
    resolved = [await assets.get(asset_id) for asset_id in job.asset_ids]
    found_assets = [asset for asset in resolved if asset is not None]
    asset_rows = [await _asset_row(asset, briefs, asset_store) for asset in found_assets]
    return job_json(job.job_id, job, asset_rows, now=clock.now())


def _preview_response(
    resolved: PreviewableCreativeAsset, *, if_none_match: str | None, max_age_s: int
) -> Response:
    """Modelado sobre `brand.presentation.router._preview_response`
    (mismo ETag = sha256 del cuerpo servido, mismo criterio de `304`) con
    dos diferencias: `Cache-Control` lleva el TTL REAL que le queda al
    enlace firmado (nunca uno fijo, el enlace caduca de verdad a los 10
    minutos) y no hay rama CSP de SVG (`creative` nunca almacena SVG).
    `X-Content-Type-Options: nosniff` no se repite aqui -- ya lo pone
    `SecurityHeadersMiddleware` en toda respuesta (`composition/api.py`)."""
    etag = f'"{hashlib.sha256(resolved.body).hexdigest()}"'
    headers = {
        "cache-control": f"private, max-age={max_age_s}",
        "etag": etag,
        "content-disposition": "inline",
    }
    if if_none_match == etag:
        return Response(status_code=status.HTTP_304_NOT_MODIFIED, headers=headers)
    return Response(content=resolved.body, media_type=resolved.content_type, headers=headers)


__all__ = [
    "AuthenticatedCaller",
    "build_creative_router",
    "require_business_access",
]

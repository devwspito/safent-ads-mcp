"""Borde HTTP de escritura de `packages` (tasks.md T031; contracts/api.md
§4/§5/§6): `approve`, `reject`, `resume`, `undo`, `owner-context`,
`creative-candidates`, `PATCH .../creative`. Todas las rutas resuelven
`business_id` por `require_business_access` y devuelven el mismo `404` para
un paquete inexistente o de otro negocio (contracts/api.md §2).

`resume`/`undo` necesitan `PauseEntity` (execution.application, dependencia
permitida) ya cableado con freno/guardarrailes/chokepoint reales -- eso
exige mas que un adaptador SQL suelto (`Container.build_execution_use_cases`,
`composition/container.py`). Para no importar `composition` desde
`packages.presentation` (romperia la direccion de dependencias), el borde
recibe una FABRICA (`pause_entity_factory`) inyectada por
`composition/app.py`, que es quien conoce el `Container`."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from typing import Annotated, Any, Protocol

from fastapi import APIRouter, Body, Depends
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from safent_ads.audit.application.record_decision import RecordDecision
from safent_ads.audit.domain.entry import ActorKind, DecisionKind, PendingDecision
from safent_ads.audit.infrastructure.sql_repository import SqlDecisionLogRepository
from safent_ads.creative.application.ports import AssetRetrievalPort, AssetStorePort
from safent_ads.creative.domain.enums import CreativeAssetState, MediaKind, PolicyVerdictResult
from safent_ads.creative.infrastructure.sql_repositories import SqlCreativeAssetRepository
from safent_ads.execution.application.entity_lifecycle_actions import PauseEntity
from safent_ads.execution.domain.guardrails import GuardrailEvaluator
from safent_ads.execution.infrastructure.sql_brake_state import (
    DEFAULT_BRAKE_ACTOR,
    SqlBrakeStatePort,
)
from safent_ads.execution.infrastructure.sql_execution_queue import SqlExecutionQueue
from safent_ads.execution.infrastructure.sql_guardrail_sets import SqlGuardrailSetRepository
from safent_ads.execution.infrastructure.sql_spend_ledger import SqlSpendLedger
from safent_ads.iam.presentation.dependencies import CURRENT_OWNER, AuthenticatedOwner
from safent_ads.iam.presentation.errors import ApiError
from safent_ads.packages.application.approve_campaign_package import (
    ApproveCampaignPackage,
    ApproveCampaignPackageCommand,
)
from safent_ads.packages.application.errors import PackageApplicationError
from safent_ads.packages.application.reject_campaign_package import (
    RejectCampaignPackage,
    RejectCampaignPackageCommand,
)
from safent_ads.packages.application.replace_ad_creative import (
    ReplaceAdCreative,
    ReplaceAdCreativeCommand,
)
from safent_ads.packages.application.resume_package_publication import (
    ResumePackagePublication,
    ResumePackagePublicationCommand,
)
from safent_ads.packages.application.undo_package_publication import (
    UndoPackagePublication,
    UndoPackagePublicationCommand,
)
from safent_ads.packages.domain.campaign_package import MAX_OWNER_CONTEXT_LENGTH, CampaignPackage
from safent_ads.packages.domain.errors import CampaignPackageInvariantError
from safent_ads.packages.domain.identifiers import PackageId, PackageIdFormatError
from safent_ads.packages.infrastructure.creative_asset_lookup import PackageCreativeAssetLookup
from safent_ads.packages.infrastructure.sql_package_authorization_repository import (
    SqlPackageAuthorizationRepository,
)
from safent_ads.packages.infrastructure.sql_package_repository import SqlCampaignPackageRepository
from safent_ads.packages.infrastructure.sql_package_step_repository import (
    SqlPackageStepRepository,
)
from safent_ads.packages.infrastructure.sql_publication_repository import (
    SqlPackagePublicationRepository,
)
from safent_ads.panel.presentation.deps import require_business_access
from safent_ads.proposals.domain.authorization import SignerPort
from safent_ads.proposals.domain.google_channel_spec import GoogleAdvertisingChannelType
from safent_ads.shared.clock import Clock
from safent_ads.shared.ids import BusinessId

__all__ = ["build_package_admin_router"]

_OwnerDep = Annotated[AuthenticatedOwner, CURRENT_OWNER]
_BusinessDep = Annotated[str, Depends(require_business_access)]
_NOT_FOUND = ApiError(status_code=404, code="NOT_FOUND", message="No encontrado.")


def _parse_package_id(raw: str) -> PackageId:
    """Mismo criterio que `panel_read._parse_package_id`: un `package_id`
    sin forma de ULID es tan inexistente como uno que no esta en la base."""
    try:
        return PackageId.parse(raw)
    except PackageIdFormatError as exc:
        raise _NOT_FOUND from exc


_PREVIEW_TTL_SECONDS = 600

_ERROR_STATUS_BY_CODE: dict[str, int] = {
    "PACKAGE_CHANGED": 409,
    "PACKAGE_NOT_PROPOSED": 409,
    "PACKAGE_EXPIRED": 409,
    "BRAKE_ENGAGED": 409,
    "GUARDRAIL_BLOCKED": 422,
    "CREATIVE_NOT_USABLE": 422,
    "PACKAGE_NOT_EDITABLE": 409,
    "PACKAGE_NOT_RESUMABLE": 409,
    "PACKAGE_APPROVAL_EXPIRED": 409,
    "UNDO_WINDOW_CLOSED": 409,
    "UNDO_NO_CONFIRMED_CAMPAIGN": 409,
    "UNDO_PAUSE_DENIED": 409,
    "PUBLICATION_NOT_FOUND": 404,
}


class AssetAccessPort(AssetStorePort, AssetRetrievalPort, Protocol):
    """`LocalAssetStorage` implementa las dos: preview firmada
    (`creative-candidates`) y lectura de bytes (`PackageCreativeAssetLookup`
    al reapuntar una creatividad, T026). Un solo parametro en vez de dos
    para el mismo adaptador real."""


@dataclass(frozen=True, slots=True)
class _RouterDeps:
    """Agrupa lo que cada ruta necesita para construir sus adaptadores por
    peticion -- evita que los handlers de mas de 5 argumentos disparen
    PLR0917 sin esconder ninguna dependencia real."""

    session_factory: async_sessionmaker[AsyncSession]
    clock: Clock
    asset_store: AssetAccessPort
    signer: SignerPort
    pause_entity_factory: Callable[[AsyncSession], PauseEntity]
    enabled_google_channels: frozenset[GoogleAdvertisingChannelType]


def build_package_admin_router(
    session_factory: async_sessionmaker[AsyncSession],
    clock: Clock,
    asset_store: AssetAccessPort,
    signer: SignerPort,
    pause_entity_factory: Callable[[AsyncSession], PauseEntity],
    *,
    enabled_google_channels: frozenset[GoogleAdvertisingChannelType] = frozenset(
        {GoogleAdvertisingChannelType.SEARCH}
    ),
) -> APIRouter:
    deps = _RouterDeps(
        session_factory=session_factory,
        clock=clock,
        asset_store=asset_store,
        signer=signer,
        pause_entity_factory=pause_entity_factory,
        enabled_google_channels=enabled_google_channels,
    )
    router = APIRouter(prefix="/api/v1/packages", tags=["packages"])

    @router.post("/{package_id}/approve")
    async def approve(
        package_id: str,
        business_id: _BusinessDep,
        owner: _OwnerDep,
        body: Annotated[dict[str, Any], Body(...)],
    ) -> dict[str, Any]:
        return await _approve(deps, package_id, business_id, owner, body)

    @router.post("/{package_id}/reject")
    async def reject(
        package_id: str,
        business_id: _BusinessDep,
        owner: _OwnerDep,
        body: Annotated[dict[str, Any], Body(...)],
    ) -> dict[str, Any]:
        return await _reject(deps, package_id, business_id, owner, body)

    @router.post("/{package_id}/resume")
    async def resume(
        package_id: str,
        business_id: _BusinessDep,
        owner: _OwnerDep,
        body: Annotated[dict[str, Any], Body(...)],
    ) -> dict[str, Any]:
        return await _resume(deps, package_id, business_id, owner, body)

    @router.post("/{package_id}/undo")
    async def undo(
        package_id: str,
        business_id: _BusinessDep,
        owner: _OwnerDep,
        body: Annotated[dict[str, Any], Body(...)],
    ) -> dict[str, Any]:
        return await _undo(deps, package_id, business_id, owner, body)

    @router.put("/{package_id}/owner-context")
    async def put_owner_context(
        package_id: str,
        business_id: _BusinessDep,
        _owner: _OwnerDep,
        body: Annotated[dict[str, Any], Body(...)],
    ) -> dict[str, Any]:
        return await _put_owner_context(deps, package_id, business_id, body)

    @router.get("/{package_id}/creative-candidates")
    async def creative_candidates(
        package_id: str,
        business_id: _BusinessDep,
        ad_local_ref: str,  # noqa: ARG001 - reservado para el filtro de formato (T054)
    ) -> dict[str, Any]:
        return await _creative_candidates(deps, package_id, business_id)

    # `ad_local_ref` es "as#1/ad#1" -- lleva una barra de verdad
    # (data-model.md `AdRef`), asi que el segmento necesita el
    # convertidor `:path` para no confundirse con dos segmentos.
    @router.patch("/{package_id}/ads/{ad_local_ref:path}/creative")
    async def patch_ad_creative(
        package_id: str,
        ad_local_ref: str,
        business_id: _BusinessDep,
        _owner: _OwnerDep,
        body: Annotated[dict[str, Any], Body(...)],
    ) -> dict[str, Any]:
        return await _patch_ad_creative(deps, package_id, ad_local_ref, business_id, body)

    return router


def _api_error(exc: PackageApplicationError) -> ApiError:
    status = _ERROR_STATUS_BY_CODE.get(exc.code, 422)
    return ApiError(status_code=status, code=exc.code, message=str(exc))


async def _require_package(
    repo: SqlCampaignPackageRepository, package_id: str, business_id: str
) -> CampaignPackage:
    package = await repo.get(
        _parse_package_id(package_id), business_id=BusinessId.parse(business_id)
    )
    if package is None:
        raise _NOT_FOUND
    return package


async def _approve(
    deps: _RouterDeps,
    package_id: str,
    business_id: str,
    owner: AuthenticatedOwner,
    body: dict[str, Any],
) -> dict[str, Any]:
    package_hash = _require_package_hash(body)
    async with deps.session_factory() as session:
        use_case = _build_approve_use_case(
            session, deps.clock, deps.signer, deps.enabled_google_channels
        )
        try:
            result = await use_case.execute(
                ApproveCampaignPackageCommand(
                    business_id=BusinessId.parse(business_id),
                    package_id=_parse_package_id(package_id),
                    package_hash=package_hash,
                    approved_by=owner.email,
                    comment=body.get("comment"),
                )
            )
        except PackageApplicationError as exc:
            raise _api_error(exc) from exc
        await session.commit()
    deadline = result.approval_expires_at.isoformat()
    return {
        "publication_id": result.publication_id,
        "authorization_id": result.authorization_id,
        "grace_seconds": result.grace_seconds,
        "execution_starts_at": deps.clock.now().isoformat(),
        "approval_expires_at": deadline,
        "undo": {"kind": "cancel_publication", "deadline": deadline},
    }


async def _reject(
    deps: _RouterDeps,
    package_id: str,
    business_id: str,
    owner: AuthenticatedOwner,
    body: dict[str, Any],
) -> dict[str, Any]:
    package_hash = _require_package_hash(body)
    parsed_business_id = BusinessId.parse(business_id)
    async with deps.session_factory() as session:
        use_case = RejectCampaignPackage(
            packages=SqlCampaignPackageRepository(session), clock=deps.clock
        )
        try:
            await use_case.execute(
                RejectCampaignPackageCommand(
                    business_id=parsed_business_id,
                    package_id=_parse_package_id(package_id),
                    package_hash=package_hash,
                    comment=body.get("comment"),
                )
            )
        except PackageApplicationError as exc:
            raise _api_error(exc) from exc
        # M3 (repaso de seguridad 0.2.23): `reject` no dejaba ningun rastro
        # queryable de quien rechazo un paquete ni por que.
        await _record_package_decision(
            session,
            business_id=parsed_business_id,
            actor_email=owner.email,
            kind=DecisionKind.PACKAGE_REJECTED,
            package_id=package_id,
            payload={"comment": body.get("comment")},
        )
        await session.commit()
    return {}


async def _record_package_decision(
    session: AsyncSession,
    *,
    business_id: BusinessId,
    actor_email: str,
    kind: DecisionKind,
    package_id: str,
    payload: dict[str, Any],
) -> None:
    """M3 (repaso de seguridad 0.2.23): `reject`/`resume`/`undo` anexan al
    MISMO `decision_log` que `approve` ya deja firmado via su propia
    `Authorization` -- persona (`actor_id`) + razon/comentario (`payload`)
    + `package_id`, nunca solo un log de structlog ni silencio."""
    recorder = RecordDecision(SqlDecisionLogRepository(session))
    await recorder.execute(
        PendingDecision(
            business_id=business_id,
            kind=kind,
            actor_kind=ActorKind.OWNER,
            actor_id=actor_email,
            payload={"package_id": package_id, **payload},
        )
    )


async def _resume(
    deps: _RouterDeps,
    package_id: str,
    business_id: str,
    owner: AuthenticatedOwner,
    body: dict[str, Any],
) -> dict[str, Any]:
    package_hash = _require_package_hash(body)
    parsed_business_id = BusinessId.parse(business_id)
    async with deps.session_factory() as session:
        use_case = ResumePackagePublication(
            packages=SqlCampaignPackageRepository(session),
            publications=SqlPackagePublicationRepository(session),
            clock=deps.clock,
        )
        try:
            result = await use_case.execute(
                ResumePackagePublicationCommand(
                    business_id=parsed_business_id,
                    package_id=_parse_package_id(package_id),
                    package_hash=package_hash,
                    resumed_by=owner.email,
                )
            )
        except PackageApplicationError as exc:
            raise _api_error(exc) from exc
        # M3 (repaso de seguridad 0.2.23): `resume` solo dejaba un log de
        # structlog (ephemero, no queryable) -- misma persona, ahora
        # tambien en el `decision_log`.
        await _record_package_decision(
            session,
            business_id=parsed_business_id,
            actor_email=owner.email,
            kind=DecisionKind.PACKAGE_RESUMED,
            package_id=package_id,
            payload={"publication_id": result.publication_id},
        )
        await session.commit()
    return {
        "publication_id": result.publication_id,
        "approval_expires_at": result.approval_expires_at.isoformat(),
    }


async def _undo(
    deps: _RouterDeps,
    package_id: str,
    business_id: str,
    owner: AuthenticatedOwner,
    body: dict[str, Any],
) -> dict[str, Any]:
    package_hash = _require_package_hash(body)
    reason = _require_undo_reason(body)
    parsed_business_id = BusinessId.parse(business_id)
    async with deps.session_factory() as session:
        use_case = UndoPackagePublication(
            packages=SqlCampaignPackageRepository(session),
            publications=SqlPackagePublicationRepository(session),
            steps=SqlPackageStepRepository(session),
            pause_entity=deps.pause_entity_factory(session),
            clock=deps.clock,
        )
        try:
            result = await use_case.execute(
                UndoPackagePublicationCommand(
                    business_id=parsed_business_id,
                    package_id=_parse_package_id(package_id),
                    package_hash=package_hash,
                    owner_email=owner.email,
                    reason=reason,
                )
            )
        except PackageApplicationError as exc:
            raise _api_error(exc) from exc
        # M3 (repaso de seguridad 0.2.23): `UndoPackagePublication` nunca
        # persistia `reason` en ningun sitio (`_cancel` archiva un motivo
        # fijo, "cancelled_by_owner"; `_pause` ni siquiera se lo pasa a
        # `PauseEntity`) -- se archiva aqui, junto a la persona, sin tocar
        # el contrato del caso de uso.
        await _record_package_decision(
            session,
            business_id=parsed_business_id,
            actor_email=owner.email,
            kind=DecisionKind.UNDO,
            package_id=package_id,
            payload={"reason": reason, "undo_kind": result.undo_kind},
        )
        await session.commit()
    return _undo_response(result)


def _undo_response(result: Any) -> dict[str, Any]:  # noqa: ANN401 - UndoPackagePublicationResult
    if result.undo_kind == "cancelled_publication":
        return {
            "undo_kind": "cancelled_publication",
            "sentence": "Publicación cancelada. No se ha creado nada.",
        }
    return {
        "undo_kind": "campaign_paused",
        "campaign_entity_ref": result.campaign_entity_ref,
        "execution_id": result.execution_id,
        "sentence": (
            "Campaña pausada. Lo creado sigue ahí, sin entregar. "
            "Puedes eliminarla en Campañas."
        ),
    }


async def _put_owner_context(
    deps: _RouterDeps, package_id: str, business_id: str, body: dict[str, Any]
) -> dict[str, Any]:
    text_value = _require_owner_context_text(body)
    async with deps.session_factory() as session:
        repo = SqlCampaignPackageRepository(session)
        package = await _require_package(repo, package_id, business_id)
        try:
            package.set_owner_context(text_value)
        except CampaignPackageInvariantError as exc:
            raise ApiError(status_code=422, code="VALIDATION_ERROR", message=str(exc)) from exc
        await repo.save(package)
        await session.commit()
    return {}


async def _creative_candidates(
    deps: _RouterDeps, package_id: str, business_id: str
) -> dict[str, Any]:
    async with deps.session_factory() as session:
        repo = SqlCampaignPackageRepository(session)
        package = await _require_package(repo, package_id, business_id)
        candidates = await SqlCreativeAssetRepository(session).list_for_business(
            package.business_id, media_kind=MediaKind.IMAGE
        )
        items = [
            await _candidate_item(asset, deps.asset_store)
            for asset in candidates
            if _is_usable(asset)
        ]
    return {"items": items}


def _is_usable(asset: Any) -> bool:  # noqa: ANN401 - `CreativeAsset`, sin exponer el tipo aqui
    # `READY` solo se alcanza con veredicto PASS/WARN
    # (`CreativeAsset.mark_ready`: FAIL -> REJECTED).
    return asset.state is CreativeAssetState.READY and asset.policy_verdict is not None


async def _candidate_item(asset: Any, asset_store: AssetAccessPort) -> dict[str, Any]:  # noqa: ANN401
    policy = "ok" if asset.policy_verdict.verdict is PolicyVerdictResult.PASS_ else "revisar"
    preview_url = await asset_store.signed_preview_url(asset.storage_uri, _PREVIEW_TTL_SECONDS)
    return {
        "asset_id": str(asset.asset_id),
        "preview_url": preview_url,
        "format": asset.format.value if asset.format is not None else None,
        "policy": policy,
        "created_at": asset.provenance.generated_at.isoformat(),
    }


async def _patch_ad_creative(
    deps: _RouterDeps, package_id: str, ad_local_ref: str, business_id: str, body: dict[str, Any]
) -> dict[str, Any]:
    package_hash = _require_package_hash(body)
    creative_asset_id = _require_creative_asset_id(body)
    async with deps.session_factory() as session:
        use_case = ReplaceAdCreative(
            packages=SqlCampaignPackageRepository(session),
            creative_lookup=PackageCreativeAssetLookup(
                SqlCreativeAssetRepository(session), deps.asset_store
            ),
            clock=deps.clock,
        )
        try:
            new_hash = await use_case.execute(
                ReplaceAdCreativeCommand(
                    business_id=BusinessId.parse(business_id),
                    package_id=_parse_package_id(package_id),
                    package_hash=package_hash,
                    ad_local_ref=ad_local_ref,
                    creative_asset_id=creative_asset_id,
                )
            )
        except PackageApplicationError as exc:
            raise _api_error(exc) from exc
        await session.commit()
    return {"package_hash": new_hash}


def _build_approve_use_case(
    session: AsyncSession,
    clock: Clock,
    signer: SignerPort,
    enabled_google_channels: frozenset[GoogleAdvertisingChannelType],
) -> ApproveCampaignPackage:
    execution_queue = SqlExecutionQueue(session, clock)
    return ApproveCampaignPackage(
        packages=SqlCampaignPackageRepository(session),
        publications=SqlPackagePublicationRepository(session),
        authorizations=SqlPackageAuthorizationRepository(session),
        brakes=SqlBrakeStatePort(session, actor=DEFAULT_BRAKE_ACTOR),
        guardrail_evaluator=GuardrailEvaluator(),
        guardrail_sets=SqlGuardrailSetRepository(session),
        spend_ledger=SqlSpendLedger(session, clock, execution_queue),
        signer=signer,
        clock=clock,
        enabled_google_channels=enabled_google_channels,
    )


def _require_package_hash(body: dict[str, Any]) -> str:
    value = body.get("package_hash")
    if not isinstance(value, str) or not value:
        raise ApiError(status_code=422, code="VALIDATION_ERROR", message="package_hash requerido")
    return value


def _require_undo_reason(body: dict[str, Any]) -> str:
    value = body.get("reason")
    if not isinstance(value, str) or not value:
        raise ApiError(status_code=422, code="VALIDATION_ERROR", message="reason requerido")
    return value


def _require_creative_asset_id(body: dict[str, Any]) -> str:
    value = body.get("creative_asset_id")
    if not isinstance(value, str) or not value:
        raise ApiError(
            status_code=422, code="VALIDATION_ERROR", message="creative_asset_id requerido"
        )
    return value


def _require_owner_context_text(body: dict[str, Any]) -> str:
    value = body.get("text")
    if not isinstance(value, str):
        raise ApiError(status_code=422, code="VALIDATION_ERROR", message="text requerido")
    if len(value) > MAX_OWNER_CONTEXT_LENGTH:
        raise ApiError(
            status_code=422,
            code="VALIDATION_ERROR",
            message=f"text supera el maximo de {MAX_OWNER_CONTEXT_LENGTH} caracteres",
        )
    return value

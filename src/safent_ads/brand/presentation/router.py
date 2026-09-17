"""Router REST de `brand`: el kit confirmado (`GET /brand`,
`GET /brand/assets`) mas el descubrimiento de identidad de marca desde un
sitio web opcional (owner request: "El MCP debe pedir el sitio web del
cliente (OPCIONAL) ... El usuario puede subir manual o poner el enlace y
que se rastree desde la web"): `POST /brand/discover`, `GET /brand/draft`,
`POST /brand/confirm`, `POST /brand/assets` (subida manual, multipart),
`GET /brand/assets/{asset_id}/preview` (bytes servidos, mismo campo
`preview_url` que expone `creative.presentation.serializers`),
`PUT /brand/claims` (politica de reclamos y avisos legales, el unico
bloque que `POST /brand/confirm` deliberadamente no gestiona).
Sin logica de negocio: valida, llama el caso de uso, mapea a JSON --
mismo reparto que `mcp_tools.py` (misma pareja de payloads compartidos en
`presentation/payloads.py`).

`require_business_access` cablea la sesion real de `iam` (integracion):
cookie `ads_session` -> `Owner` (401 sin sesion valida) -> existencia del
negocio contra `SqlBusinessDirectory` (404, nunca 403, para no filtrar
existencia — rest-api.md §"Seguridad transversal"), mismo patron que
`creative.presentation.router`/`panel.presentation.deps`. Se mantiene el
propio `_not_found()` del modulo (`ENTITY_NOT_FOUND`) en vez del de `iam`
para no romper el codigo de error que ya cubren los tests de este router."""

from __future__ import annotations

import hashlib
from typing import Annotated

from fastapi import (
    APIRouter,
    Depends,
    HTTPException,
    Query,
    Request,
    Response,
    UploadFile,
    status,
)
from pydantic import Field

from safent_ads.brand.application.confirm_brand_draft import ConfirmBrandDraft
from safent_ads.brand.application.errors import (
    BrandAssetNotFoundError,
    BrandDraftAssetNotFoundError,
    BrandDraftNotFoundError,
    BrandKitNotFoundError,
    BrandWebsiteUnreachableError,
)
from safent_ads.brand.application.get_brand_asset_preview import (
    BrandAssetPreviewRequest,
    GetBrandAssetPreview,
)
from safent_ads.brand.application.get_brand_draft import GetBrandDraft
from safent_ads.brand.application.get_brand_kit import GetBrandKit
from safent_ads.brand.application.ingest_brand_from_website import (
    IngestBrandFromWebsite,
    IngestBrandFromWebsiteRequest,
)
from safent_ads.brand.application.list_brand_assets import ListBrandAssets
from safent_ads.brand.application.update_brand_claims import UpdateBrandClaims
from safent_ads.brand.application.upload_brand_asset import (
    UploadBrandAsset,
    UploadBrandAssetBytesRequest,
)
from safent_ads.brand.domain.brand_asset import AssetKind
from safent_ads.brand.domain.discovery import LogoCandidate
from safent_ads.brand.domain.errors import (
    AllowedClaimConflictsWithForbiddenError,
    InvalidDiscoveryUrlError,
)
from safent_ads.brand.infrastructure.asset_preview_content_type import (
    PreviewableAsset,
    UnpreviewableBrandAssetError,
    resolve_preview,
)
from safent_ads.brand.infrastructure.local_brand_asset_storage import MAX_BRAND_ASSET_BYTES
from safent_ads.brand.presentation.payloads import (
    OPAQUE_ASSET_ID_PATTERN,
    UUID_PATTERN,
    ConfirmBrandDraftPayload,
    DiscoverBrandPayload,
    UpdateBrandClaimsPayload,
)
from safent_ads.brand.presentation.serializers import (
    asset_preview_url,
    brand_asset_summary,
    brand_kit_detail,
    draft_detail,
)
from safent_ads.composition.container import Container
from safent_ads.iam.infrastructure.sql_business_directory import SqlBusinessDirectory
from safent_ads.iam.presentation.dependencies import CURRENT_OWNER, AuthenticatedOwner
from safent_ads.shared.ids import BusinessId

_UPLOAD_CHUNK_BYTES = 64 * 1024
_NOT_FOUND_MESSAGE = "No encontrado."
_NO_BRAND_KIT_MESSAGE = "No hay kit de marca para este negocio."
_NO_DRAFT_MESSAGE = "No hay borrador de marca para este negocio."
_STALE_ASSET_MESSAGE = "Uno de los activos seleccionados ya no esta en el borrador actual."
_UNPREVIEWABLE_ASSET_MESSAGE = "Este activo no se puede previsualizar."
_PREVIEW_CACHE_CONTROL = "private, max-age=300"
_SVG_SANDBOX_CSP = "sandbox"


def _error_body(code: str, message: str) -> dict[str, object]:
    return {"error": {"code": code, "message": message, "details": {}}}


def _not_found(message: str = _NOT_FOUND_MESSAGE) -> HTTPException:
    return HTTPException(
        status_code=status.HTTP_404_NOT_FOUND, detail=_error_body("ENTITY_NOT_FOUND", message)
    )


def _validation_error(code: str, message: str) -> HTTPException:
    return HTTPException(
        status_code=status.HTTP_422_UNPROCESSABLE_CONTENT, detail=_error_body(code, message)
    )


async def require_business_access(
    business_id: Annotated[str, Query(pattern=UUID_PATTERN)],
    request: Request,
    _owner: AuthenticatedOwner = CURRENT_OWNER,
) -> BusinessId:
    try:
        parsed = BusinessId.parse(business_id)
    except ValueError as exc:
        raise _not_found() from exc

    container: Container = request.app.state.container
    async with container.session_factory() as db_session:
        exists = await SqlBusinessDirectory(db_session).exists(parsed.value)
    if not exists:
        raise _not_found()
    return parsed


BusinessIdDep = Annotated[BusinessId, Depends(require_business_access)]
_OwnerDep = Annotated[AuthenticatedOwner, CURRENT_OWNER]


def build_brand_router(  # noqa: PLR0915 - raiz de router, cada handler es de 5-10 lineas
    *,
    get_brand_kit: GetBrandKit,
    list_brand_assets: ListBrandAssets,
    ingest_brand_from_website: IngestBrandFromWebsite,
    get_brand_draft: GetBrandDraft,
    confirm_brand_draft: ConfirmBrandDraft,
    upload_brand_asset: UploadBrandAsset,
    get_brand_asset_preview: GetBrandAssetPreview,
    update_brand_claims: UpdateBrandClaims,
) -> APIRouter:
    """Cablea las dependencias en closures: `composition` inyecta los
    adaptadores reales sin que este modulo conozca su implementacion
    (DIP). Crea un `APIRouter` nuevo en cada llamada -- nunca comparte uno
    a nivel de modulo -- para que llamarla mas de una vez (p.ej. en tests)
    no registre las rutas por duplicado."""
    router = APIRouter(prefix="/api/v1", tags=["brand"])

    @router.get("/brand")
    async def get_brand_route(business_id: BusinessIdDep) -> dict[str, object]:
        try:
            brand_kit = await get_brand_kit.execute(business_id)
        except BrandKitNotFoundError as exc:
            raise _not_found(_NO_BRAND_KIT_MESSAGE) from exc
        return brand_kit_detail(brand_kit)

    @router.get("/brand/assets")
    async def list_brand_assets_route(
        business_id: BusinessIdDep, kind: AssetKind | None = None
    ) -> dict[str, object]:
        try:
            assets = await list_brand_assets.execute(business_id, kind=kind)
        except BrandKitNotFoundError as exc:
            raise _not_found(_NO_BRAND_KIT_MESSAGE) from exc
        return {"items": [brand_asset_summary(asset) for asset in assets]}

    @router.post("/brand/assets", status_code=status.HTTP_201_CREATED)
    async def upload_brand_asset_route(
        business_id: BusinessIdDep, kind: Annotated[AssetKind, Query()], file: UploadFile
    ) -> dict[str, object]:
        payload = await _read_upload_within_limit(file)
        candidate = await upload_brand_asset.from_bytes(
            UploadBrandAssetBytesRequest(business_id=business_id, kind=kind, payload=payload)
        )
        return _logo_candidate_response(candidate)

    @router.get("/brand/assets/{asset_id}/preview")
    async def get_brand_asset_preview_route(
        business_id: BusinessIdDep,
        asset_id: Annotated[str, Field(pattern=OPAQUE_ASSET_ID_PATTERN)],
        request: Request,
    ) -> Response:
        try:
            preview = await get_brand_asset_preview.execute(
                BrandAssetPreviewRequest(business_id=business_id, asset_id=asset_id)
            )
        except BrandAssetNotFoundError as exc:
            raise _not_found() from exc
        try:
            resolved = resolve_preview(preview.payload)
        except UnpreviewableBrandAssetError as exc:
            raise HTTPException(
                status_code=status.HTTP_415_UNSUPPORTED_MEDIA_TYPE,
                detail=_error_body("UNSUPPORTED_ASSET_TYPE", _UNPREVIEWABLE_ASSET_MESSAGE),
            ) from exc
        return _preview_response(resolved, if_none_match=request.headers.get("if-none-match"))

    @router.post("/brand/discover")
    async def discover_brand_route(
        business_id: BusinessIdDep, body: DiscoverBrandPayload
    ) -> dict[str, object]:
        try:
            draft = await ingest_brand_from_website.execute(
                IngestBrandFromWebsiteRequest(business_id=business_id, url=body.url)
            )
        except InvalidDiscoveryUrlError as exc:
            raise _validation_error("VALIDATION_ERROR", "URL no valida para rastreo.") from exc
        except BrandWebsiteUnreachableError as exc:
            raise _validation_error(
                "DISCOVERY_UNREACHABLE", "No se pudo rastrear el sitio indicado."
            ) from exc
        return draft_detail(draft)

    @router.get("/brand/draft")
    async def get_brand_draft_route(business_id: BusinessIdDep) -> dict[str, object]:
        try:
            draft = await get_brand_draft.execute(business_id)
        except BrandDraftNotFoundError as exc:
            raise _not_found(_NO_DRAFT_MESSAGE) from exc
        return draft_detail(draft)

    @router.post("/brand/confirm")
    async def confirm_brand_draft_route(
        business_id: BusinessIdDep, body: ConfirmBrandDraftPayload
    ) -> dict[str, object]:
        try:
            kit = await confirm_brand_draft.execute(body.to_request(business_id))
        except BrandDraftNotFoundError as exc:
            raise _not_found(_NO_DRAFT_MESSAGE) from exc
        except BrandDraftAssetNotFoundError as exc:
            raise _validation_error("VALIDATION_ERROR", _STALE_ASSET_MESSAGE) from exc
        return brand_kit_detail(kit)

    @router.put("/brand/claims")
    async def update_brand_claims_route(
        business_id: BusinessIdDep, body: UpdateBrandClaimsPayload, owner: _OwnerDep
    ) -> dict[str, object]:
        try:
            kit = await update_brand_claims.execute(
                body.to_request(business_id, actor_email=owner.email)
            )
        except BrandKitNotFoundError as exc:
            raise _not_found(_NO_BRAND_KIT_MESSAGE) from exc
        except AllowedClaimConflictsWithForbiddenError as exc:
            raise _validation_error("VALIDATION_ERROR", str(exc)) from exc
        return brand_kit_detail(kit)

    return router


async def _read_upload_within_limit(file: UploadFile) -> bytes:
    """Corta la lectura del `UploadFile` en streaming al mismo tope que
    `LocalBrandAssetStorage` aplicaria de todas formas -- sin esto, un
    fichero enorme se bufferizaria entero en memoria antes de que el
    puerto tuviera ocasion de rechazarlo (trust boundary, nunca I/O sin
    limite)."""
    chunks: list[bytes] = []
    total = 0
    while chunk := await file.read(_UPLOAD_CHUNK_BYTES):
        total += len(chunk)
        if total > MAX_BRAND_ASSET_BYTES:
            raise _validation_error(
                "VALIDATION_ERROR", f"El fichero supera el tope de {MAX_BRAND_ASSET_BYTES} bytes."
            )
        chunks.append(chunk)
    return b"".join(chunks)


def _logo_candidate_response(candidate: LogoCandidate) -> dict[str, object]:
    return {
        "asset_id": candidate.asset_id,
        "kind": candidate.kind.value,
        "storage_uri": candidate.storage_uri,
        "sha256": candidate.sha256,
        "source": candidate.source.value,
        "confidence": candidate.confidence,
        "preview_url": asset_preview_url(candidate.asset_id),
    }


def _preview_response(resolved: PreviewableAsset, *, if_none_match: str | None) -> Response:
    """`ETag` es el sha256 del cuerpo REALMENTE servido (el SVG saneado,
    no el original) entre comillas (RFC 7232), y decide el `304` -- nunca
    el `sha256` que guarda `LogoCandidate`, que es del payload en crudo
    antes de sanear. `Content-Security-Policy: sandbox` solo en SVG
    (`X-Content-Type-Options: nosniff` ya lo pone `SecurityHeadersMiddleware`
    en toda respuesta, `composition/api.py`, asi que no se repite aqui)."""
    etag = f'"{hashlib.sha256(resolved.body).hexdigest()}"'
    headers = {"cache-control": _PREVIEW_CACHE_CONTROL, "etag": etag}
    if resolved.content_type == "image/svg+xml":
        headers["content-security-policy"] = _SVG_SANDBOX_CSP
    if if_none_match == etag:
        return Response(status_code=status.HTTP_304_NOT_MODIFIED, headers=headers)
    return Response(content=resolved.body, media_type=resolved.content_type, headers=headers)

"""`list_kit_files`/`get_kit_text`/`get_kit_file` (encargo del dueno,
14-sep): el kit de marketing del negocio -- marca, mascotas, campanas
pasadas, plantillas, etiquetas, guias, encargos, archivo -- ya montado de
solo lectura en el servidor via `ADS_KIT_DIR`. Principio del encargo: "el
MCP da acceso, el arnes piensa" -- las tres solo leen del disco (via
`KitStorePort`), nunca interpretan ni resumen nada.

Un unico kit por instalacion, sin variar por negocio: aun asi cada `Args`
exige `business_id` (regla de diseno documentada en `presentation/
args.py`: "todo modelo salvo ListBusinessesArgs exige business_id") solo
para que el `ToolDispatcher` autorice contra la `CallerScope` del llamante
por el mismo unico camino que el resto del catalogo -- el kit en si no se
filtra por negocio.

`build_kit_preview_router` es una ruta REST propia, no la de `creative`
(`creative/infrastructure/local_asset_storage.py`/`creative/presentation/
router.py`, otra lane): raiz de disco distinta y, sobre todo, sin sesion de
panel -- quien pide `get_kit_file` es el arnes MCP (Claude Code/Codex), no
el navegador del dueno, asi que no hay cookie de `CallerDep` que exigir.
Mismo principio que ya documenta `LocalAssetStorage.signed_preview_url`:
"la firma HMAC + TTL... es la puerta real de ESE recurso" -- aqui es la
UNICA puerta, a proposito."""

from __future__ import annotations

import re
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Annotated, Any

from fastapi import APIRouter, Response
from pydantic import AfterValidator, Field

from safent_ads.iam.presentation.errors import ApiError
from safent_ads.mcp.application.caller_scope import CallerScope
from safent_ads.mcp.application.kit_port import (
    KitFileEntry,
    KitNotConfiguredError,
    KitPathRejectedError,
    KitStorePort,
    KitTextFile,
)
from safent_ads.mcp.presentation.args import BusinessId, ToolArgs
from safent_ads.mcp.presentation.registry import ToolClass, ToolDefinition

__all__ = ["KitToolServices", "build_kit_preview_router", "build_kit_tool_definitions"]

type Handler[ArgsT, ReturnT] = Callable[[ArgsT, CallerScope], Awaitable[ReturnT]]

_MAX_LIST_RESULTS = 500
_DEFAULT_LIST_RESULTS = 100
_MAX_TEXT_BYTES = 262_144
_DEFAULT_TEXT_BYTES = 65_536
_MAX_QUERY_LENGTH = 200
_MAX_PATH_LENGTH = 512
_PREVIEW_TTL_S = 600  # 10 minutos, mismo TTL que `creative` (rest-api.md)

_CONTROL_CHAR_PATTERN = re.compile(r"[\x00-\x1f]")

_CONTENT_TYPE_BY_EXTENSION: dict[str, str] = {
    ".png": "image/png",
    ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg",
    ".webp": "image/webp",
    ".pdf": "application/pdf",
    ".zip": "application/zip",
    ".woff2": "font/woff2",
    ".ttf": "font/ttf",
}

_KIT_GUIDE_NOTE = (
    "Antes de nada, lee AGENTS.md, README.md y .agents/product-marketing.md en la raiz del "
    "kit (list_kit_files con path=\"\" y get_kit_text sobre esos ficheros): son la guia del "
    "propio kit, con las convenciones que estas tres herramientas asumen."
)


def _reject_control_chars(value: str) -> str:
    if _CONTROL_CHAR_PATTERN.search(value):
        raise ValueError("ruta con caracteres de control no permitida")
    return value


_KitRelativePath = Annotated[
    str, Field(max_length=_MAX_PATH_LENGTH), AfterValidator(_reject_control_chars)
]
_KitQuery = Annotated[str, Field(min_length=1, max_length=_MAX_QUERY_LENGTH)]


@dataclass(frozen=True, slots=True)
class KitToolServices:
    """`store=None` (encargo del dueno: "si `ADS_KIT_DIR` unset -> tools
    report «kit no configurado» cleanly"): las tres herramientas siguen
    registradas, cada handler devuelve `KIT_NOT_CONFIGURED` en vez de
    fallar en crudo contra un puerto ausente.

    `brand_name` (lane 006-cloudflare-ui, imagen generica): rellena la
    descripcion de `list_kit_files`/`get_kit_text`, nunca el nombre de un
    cliente a pie de letra -- `ApiSettings.brand_name` (`ADS_BRAND_NAME`)
    en produccion, "tu negocio" por defecto."""

    store: KitStorePort | None
    public_base_url: str
    brand_name: str = "tu negocio"


class _KitScopedArgs(ToolArgs):
    business_id: BusinessId


class ListKitFilesArgs(_KitScopedArgs):
    path: _KitRelativePath = ""
    query: _KitQuery | None = None
    max_results: Annotated[int, Field(ge=1, le=_MAX_LIST_RESULTS)] = _DEFAULT_LIST_RESULTS


class GetKitTextArgs(_KitScopedArgs):
    path: Annotated[_KitRelativePath, Field(min_length=1)]
    max_bytes: Annotated[int, Field(ge=1, le=_MAX_TEXT_BYTES)] = _DEFAULT_TEXT_BYTES


class GetKitFileArgs(_KitScopedArgs):
    path: Annotated[_KitRelativePath, Field(min_length=1)]


def build_kit_tool_definitions(services: KitToolServices) -> list[ToolDefinition[Any]]:
    return [
        ToolDefinition(
            name="list_kit_files",
            description=(
                f"Explora el Kit de Marketing de {services.brand_name} (marca, mascotas, "
                "campanas pasadas, plantillas, etiquetas, guias, encargos, archivo): ruta "
                "relativa, tamano, extension y fecha de modificacion, recursivo desde `path` "
                "(raiz si se omite). `query` filtra por subcadena en la ruta, sin distinguir "
                "mayusculas. " + _KIT_GUIDE_NOTE
            ),
            args_model=ListKitFilesArgs,
            tool_class=ToolClass.READ,
            handler=_list_kit_files(services),
            business_id_of=_by_business_id,
        ),
        ToolDefinition(
            name="get_kit_text",
            description=(
                "Lee el contenido de texto (.md .txt .json .csv .yaml .yml .html .svg .py "
                ".mjs .ts) de un fichero del kit por su ruta relativa, hasta `max_bytes`. "
                "Devuelve el texto tal cual, en UTF-8; nunca lo ejecuta ni lo interpreta. "
                + _KIT_GUIDE_NOTE
            ),
            args_model=GetKitTextArgs,
            tool_class=ToolClass.READ,
            handler=_get_kit_text(services),
            business_id_of=_by_business_id,
        ),
        ToolDefinition(
            name="get_kit_file",
            description=(
                "Da una URL de previsualizacion firmada y caducable (10 minutos) para un "
                "fichero binario del kit (.png .jpg .jpeg .webp .pdf .zip .woff2 .ttf) por "
                "su ruta relativa. Para texto, usa get_kit_text."
            ),
            args_model=GetKitFileArgs,
            tool_class=ToolClass.READ,
            handler=_get_kit_file(services),
            business_id_of=_by_business_id,
        ),
    ]


def _require_store(services: KitToolServices) -> KitStorePort:
    if services.store is None:
        raise KitNotConfiguredError("el kit de marketing no esta configurado en esta instalacion")
    return services.store


def _list_kit_files(services: KitToolServices) -> Handler[ListKitFilesArgs, list[KitFileEntry]]:
    async def handler(args: ListKitFilesArgs, _caller_scope: CallerScope) -> list[KitFileEntry]:
        store = _require_store(services)
        return await store.list_files(args.path, query=args.query, max_results=args.max_results)

    return handler


def _get_kit_text(services: KitToolServices) -> Handler[GetKitTextArgs, KitTextFile]:
    async def handler(args: GetKitTextArgs, _caller_scope: CallerScope) -> KitTextFile:
        store = _require_store(services)
        content = await store.read_text(args.path, max_bytes=args.max_bytes)
        return KitTextFile(
            path=args.path, content=content, size_bytes=len(content.encode("utf-8"))
        )

    return handler


def _get_kit_file(services: KitToolServices) -> Handler[GetKitFileArgs, dict[str, object]]:
    async def handler(args: GetKitFileArgs, _caller_scope: CallerScope) -> dict[str, object]:
        store = _require_store(services)
        relative_url = await store.signed_preview_url(args.path, ttl_s=_PREVIEW_TTL_S)
        return {
            "path": args.path,
            "preview_url": f"{services.public_base_url.rstrip('/')}{relative_url}",
            "expires_in_seconds": _PREVIEW_TTL_S,
        }

    return handler


def _by_business_id(args: Any) -> str:  # noqa: ANN401 - extractor generico por posicion
    return str(args.business_id)


def build_kit_preview_router(store: KitStorePort) -> APIRouter:
    """Crea un `APIRouter` nuevo en cada llamada (mismo criterio que
    `creative.presentation.router.build_creative_router`): llamarla mas de
    una vez (p.ej. en tests) no registra la ruta por duplicado."""
    router = APIRouter(prefix="/api/v1", tags=["kit"])

    @router.get("/kit-previews/{key:path}")
    async def get_kit_preview(key: str, exp: int, sig: str) -> Response:
        try:
            payload = await store.open_preview(key, exp, sig)
        except KitPathRejectedError as exc:
            raise ApiError(status_code=404, code="NOT_FOUND", message="No encontrado.") from exc
        content_type = _CONTENT_TYPE_BY_EXTENSION.get(
            _extension_of(key), "application/octet-stream"
        )
        return Response(
            content=payload,
            media_type=content_type,
            headers={"Cache-Control": "private, max-age=60"},
        )

    return router


def _extension_of(key: str) -> str:
    dot_index = key.rfind(".")
    return key[dot_index:].lower() if dot_index != -1 else ""

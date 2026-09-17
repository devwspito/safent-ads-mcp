"""`upload_creative_asset` (004 tasks-2.md W4, historia 13): la unica forma
que el arnes tiene de dejar dentro de Safent una imagen que genero por su
cuenta o que la persona le dio -- sin esto, esa pieza nunca entra en una
propuesta ni la ve el dueno. Clase `CREATIVE_WRITE` (nueva, `I1` la anade a
`registry.py` -- este modulo no la hardcodea: `tool_class` viaja inyectado
para no depender de un enum que otro carril todavia no crea, D-3).

Base64 unicamente (D-2, threat-model.md C-11 "sin URLs libres"): el arnes
que puede darnos una URL puede darnos los bytes. `presentation/args.py::
UploadCreativeAssetArgs` ya decodifica y acota a 8 MiB antes de que este
handler vea nada; `LocalAssetStorage.put` (via `ImportCreativeAsset.
from_bytes`) aplica magic bytes contra el `media_kind` declarado. El
activo queda sin `PolicyVerdict` (pendiente de politica): publicar sigue
exigiendo `run_creative_policy_check` -> `PASS` primero
(`ProposeCreative._require_policy_check_passed`, sin tocar)."""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Any

from safent_ads.creative.application.ports import CreativeBriefRepository
from safent_ads.creative.domain.identifiers import BriefId
from safent_ads.mcp.application.caller_scope import CallerScope
from safent_ads.mcp.application.creative_upload_port import CreativeUploadPort, CreativeUploadResult
from safent_ads.mcp.application.errors import EntityNotFoundError
from safent_ads.mcp.presentation.args import UploadCreativeAssetArgs
from safent_ads.mcp.presentation.registry import ToolClass, ToolDefinition
from safent_ads.shared.ids import BusinessId

__all__ = [
    "GENERATE_CREATIVE_ASSETS_FALLBACK_DESCRIPTION",
    "CreativeUploadToolServices",
    "build_creative_upload_tool_definitions",
]

type Handler[ArgsT, ReturnT] = Callable[[ArgsT, CallerScope], Awaitable[ReturnT]]

_DESCRIPTION = (
    "Sube una imagen o video que tu arnes genero o que la persona te dio (base64, <= 8 MiB). "
    "Queda como activo pendiente de politica: llama a run_creative_policy_check antes de "
    "usarlo en propose_creative_publication. No es el camino principal de generacion -- "
    "solo deja dentro de Safent lo que ya tienes."
)

# W5 (004 tasks-2.md): frase unica que declara `generate_creative_assets`
# como respaldo -- I1 la aplica en `creative_generation_tools.py`
# (`ToolDefinition.description` de esa herramienta), el fichero que de
# verdad la declara hoy (no `catalog.py`, que no la referencia).
GENERATE_CREATIVE_ASSETS_FALLBACK_DESCRIPTION = (
    "Respaldo. Genera la imagen con la cola del sistema solo si tu arnes no puede generarla. "
    "Si puedes generarla tu, subela con upload_creative_asset."
)


@dataclass(frozen=True, slots=True)
class CreativeUploadToolServices:
    briefs: CreativeBriefRepository
    upload_port: CreativeUploadPort
    public_base_url: str


def build_creative_upload_tool_definitions(
    services: CreativeUploadToolServices, *, tool_class: ToolClass
) -> list[ToolDefinition[Any]]:
    return [
        ToolDefinition(
            name="upload_creative_asset",
            description=_DESCRIPTION,
            args_model=UploadCreativeAssetArgs,
            tool_class=tool_class,
            handler=_upload_creative_asset(services),
            business_id_of=_by_business_id,
        )
    ]


def _upload_creative_asset(
    services: CreativeUploadToolServices,
) -> Handler[UploadCreativeAssetArgs, dict[str, object]]:
    async def handler(
        args: UploadCreativeAssetArgs, _caller_scope: CallerScope
    ) -> dict[str, object]:
        await _require_brief_in_business(services.briefs, args.brief_id, args.business_id)
        # M-4: ya decodificado (y validado) una vez en
        # `UploadCreativeAssetArgs._content_decodes_within_limit` -- nunca
        # se repite el mismo base64.
        content = args.decoded_content()
        result = await services.upload_port.upload_creative_asset(
            business_id=args.business_id,
            brief_id=args.brief_id,
            media_kind=args.media_kind.value,
            content=content,
            native_tool_used=args.native_tool_used,
        )
        return _response(result, services.public_base_url)

    return handler


async def _require_brief_in_business(
    briefs: CreativeBriefRepository, brief_id: str, business_id: str
) -> None:
    brief = await briefs.get(BriefId.parse(brief_id))
    if brief is None or brief.business_id != BusinessId.parse(business_id):
        raise EntityNotFoundError(brief_id)


def _response(result: CreativeUploadResult, public_base_url: str) -> dict[str, object]:
    preview_url = (
        f"{public_base_url.rstrip('/')}{result.preview_url}" if result.preview_url else None
    )
    return {
        "asset_id": result.asset_id,
        "media_kind": result.media_kind,
        "preview_url": preview_url,
        "policy_status": "pending",
        "message": "Activo guardado. Corre run_creative_policy_check antes de proponerlo.",
    }


def _by_business_id(args: Any) -> str:  # noqa: ANN401 - extractor generico por posicion
    return str(args.business_id)

"""`creative_upload_tools.py` (004 tasks-2.md W4): `upload_creative_asset`
sobre el almacen real (`LocalAssetStorage`, tmp_path) -- magic bytes
mentidos se rechazan, y un activo valido queda sin `PolicyVerdict`
(pendiente de politica: `run_creative_policy_check` sigue siendo
obligatorio antes de publicar)."""

from __future__ import annotations

import base64
import io
from datetime import UTC, datetime
from pathlib import Path

import pytest
from PIL import Image
from pydantic import ValidationError

from safent_ads.composition.creative_upload_adapter import ContainerCreativeUploadAdapter
from safent_ads.creative.application.import_creative_asset import ImportCreativeAsset
from safent_ads.creative.domain.identifiers import BriefId
from safent_ads.creative.infrastructure.in_memory_repositories import (
    InMemoryCreativeAssetRepository,
    InMemoryCreativeBriefRepository,
)
from safent_ads.creative.infrastructure.local_asset_storage import LocalAssetStorage
from safent_ads.mcp.application.caller_scope import CallerScope, Permission
from safent_ads.mcp.application.errors import EntityNotFoundError, ToolValidationError
from safent_ads.mcp.presentation.creative_upload_tools import (
    CreativeUploadToolServices,
    build_creative_upload_tool_definitions,
)
from safent_ads.mcp.presentation.registry import ToolClass
from safent_ads.shared.clock import FixedClock
from safent_ads.shared.ids import BusinessId
from tests.unit.creative.domain.factories import make_brief

_NOW = datetime(2026, 9, 15, tzinfo=UTC)
_PUBLIC_BASE_URL = "https://ads.example.com"


def _real_png(width: int = 2, height: int = 2) -> bytes:
    buffer = io.BytesIO()
    Image.new("RGB", (width, height), color=(10, 20, 30)).save(buffer, format="PNG")
    return buffer.getvalue()


_PNG_BYTES = _real_png()
_NOT_AN_IMAGE = b"just some plain text, not an image at all"


def _services(tmp_path: Path) -> tuple[CreativeUploadToolServices, InMemoryCreativeBriefRepository]:
    briefs = InMemoryCreativeBriefRepository()
    assets = InMemoryCreativeAssetRepository()
    storage = LocalAssetStorage(tmp_path, signing_key=b"k" * 32, clock=FixedClock(_NOW))
    import_creative_asset = ImportCreativeAsset(
        briefs=briefs,
        assets=assets,
        asset_fetch=_UnusedAssetFetch(),
        asset_store=storage,
        allowed_hosts=frozenset(),
        clock=FixedClock(_NOW),
    )
    adapter = ContainerCreativeUploadAdapter(import_creative_asset, storage)
    services = CreativeUploadToolServices(
        briefs=briefs, upload_port=adapter, public_base_url=_PUBLIC_BASE_URL
    )
    return services, briefs


class _UnusedAssetFetch:
    async def fetch(self, url: str) -> bytes:
        del url
        raise AssertionError("upload_creative_asset nunca descarga de una URL")


def _caller_scope() -> CallerScope:
    return CallerScope(
        caller_id="person:00000000-0000-0000-0000-000000000001",
        allowed_business_ids=frozenset(),
        permission=Permission.PROPOSE,
        person_label="Agente de prueba",
    )


def _handler(services: CreativeUploadToolServices):
    definitions = build_creative_upload_tool_definitions(services, tool_class=ToolClass.PROPOSAL)
    return {d.name: d for d in definitions}["upload_creative_asset"]


async def test_png_mentido_como_jpeg_se_rechaza_y_el_activo_queda_pendiente_de_politica(
    tmp_path: Path,
) -> None:
    services, briefs = _services(tmp_path)
    business_id = BusinessId.new()
    brief_id = await _add_brief(briefs, business_id)
    definition = _handler(services)

    bad_args = definition.args_model(
        business_id=str(business_id),
        brief_id=str(brief_id),
        media_kind="image",
        content_base64=base64.b64encode(_NOT_AN_IMAGE).decode(),
        native_tool_used="image_generate",
    )
    with pytest.raises(ToolValidationError):
        await definition.handler(bad_args, _caller_scope())

    good_args = definition.args_model(
        business_id=str(business_id),
        brief_id=str(brief_id),
        media_kind="image",
        content_base64=base64.b64encode(_PNG_BYTES).decode(),
        native_tool_used="image_generate",
    )
    result = await definition.handler(good_args, _caller_scope())

    assert result["policy_status"] == "pending"
    assert result["media_kind"] == "image"
    assert isinstance(result["preview_url"], str)
    assert result["preview_url"].startswith(_PUBLIC_BASE_URL)


async def test_brief_de_otro_negocio_da_entity_not_found(tmp_path: Path) -> None:
    services, briefs = _services(tmp_path)
    brief_id = await _add_brief(briefs, BusinessId.new())
    definition = _handler(services)
    args = definition.args_model(
        business_id=str(BusinessId.new()),
        brief_id=str(brief_id),
        media_kind="image",
        content_base64=base64.b64encode(_PNG_BYTES).decode(),
        native_tool_used="image_generate",
    )

    with pytest.raises(EntityNotFoundError):
        await definition.handler(args, _caller_scope())


def test_content_base64_invalido_se_rechaza_en_los_argumentos(tmp_path: Path) -> None:
    services, _ = _services(tmp_path)
    definition = _handler(services)

    with pytest.raises(ValidationError):
        definition.args_model(
            business_id=str(BusinessId.new()),
            brief_id="01ARZ3NDEKTSV4RRFFQ69G5FAV",
            media_kind="image",
            content_base64="not-base64!!",
            native_tool_used="image_generate",
        )


def test_contenido_mayor_de_8_mib_se_rechaza_en_los_argumentos(tmp_path: Path) -> None:
    services, _ = _services(tmp_path)
    definition = _handler(services)
    oversized = base64.b64encode(b"\x89PNG\r\n\x1a\n" + b"0" * (8 * 1024 * 1024 + 1)).decode()

    with pytest.raises(ValidationError):
        definition.args_model(
            business_id=str(BusinessId.new()),
            brief_id="01ARZ3NDEKTSV4RRFFQ69G5FAV",
            media_kind="image",
            content_base64=oversized,
            native_tool_used="image_generate",
        )


def test_decoded_content_matches_the_original_bytes(tmp_path: Path) -> None:
    """M-4: `decoded_content()` devuelve el mismo contenido que ya
    decodifico y valido el validador -- el handler lo reutiliza en vez de
    decodificar el mismo `content_base64` una segunda vez."""
    services, _ = _services(tmp_path)
    definition = _handler(services)

    args = definition.args_model(
        business_id=str(BusinessId.new()),
        brief_id="01ARZ3NDEKTSV4RRFFQ69G5FAV",
        media_kind="image",
        content_base64=base64.b64encode(_PNG_BYTES).decode(),
        native_tool_used="image_generate",
    )

    assert args.decoded_content() == _PNG_BYTES


def test_content_base64_over_the_string_length_cap_is_rejected(tmp_path: Path) -> None:
    """M-4: tope barato de longitud de string, sin decodificar nada."""
    services, _ = _services(tmp_path)
    definition = _handler(services)

    with pytest.raises(ValidationError):
        definition.args_model(
            business_id=str(BusinessId.new()),
            brief_id="01ARZ3NDEKTSV4RRFFQ69G5FAV",
            media_kind="image",
            content_base64="A" * 12_000_001,
            native_tool_used="image_generate",
        )


def test_ver_no_puede_construirse_como_read(tmp_path: Path) -> None:
    """`upload_creative_asset` nunca debe registrarse como `READ`: I1 debe
    inyectar `ToolClass.CREATIVE_WRITE`, no reutilizar una clase visible
    con el permiso `ver`."""
    services, _ = _services(tmp_path)
    definitions = build_creative_upload_tool_definitions(services, tool_class=ToolClass.PROPOSAL)

    assert definitions[0].tool_class is not ToolClass.READ


async def _add_brief(briefs: InMemoryCreativeBriefRepository, business_id: BusinessId) -> BriefId:
    brief_id = BriefId.new()
    await briefs.add(brief_id, make_brief(business_id=business_id, variant_count=1))
    return brief_id

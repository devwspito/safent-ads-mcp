"""`kit_tools.py` (encargo del dueno, 14-sep): los tres handlers delegan en
`KitStorePort` sin logica propia, `store=None` reporta `KIT_NOT_CONFIGURED`
limpiamente, y `build_kit_preview_router` traduce `KitPathRejectedError` al
mismo 404 uniforme que el resto de rutas de previsualizacion."""

from __future__ import annotations

from datetime import UTC, datetime
from unittest.mock import AsyncMock

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from pydantic import ValidationError

from safent_ads.mcp.application.caller_scope import CallerScope, Permission
from safent_ads.mcp.application.kit_port import (
    KitFileEntry,
    KitNotConfiguredError,
    KitPathRejectedError,
    KitTextFile,
)
from safent_ads.mcp.presentation.kit_tools import (
    KitToolServices,
    build_kit_preview_router,
    build_kit_tool_definitions,
)
from safent_ads.mcp.presentation.registry import ToolClass
from safent_ads.shared.ids import BusinessId
from tests.unit.test_no_client_strings import has_client_string

_PUBLIC_BASE_URL = "https://ads.example.com"
_NOW = datetime(2026, 9, 15, tzinfo=UTC)


def _caller_scope() -> CallerScope:
    return CallerScope(
        caller_id="person:00000000-0000-0000-0000-000000000001",
        allowed_business_ids=frozenset(),
        permission=Permission.VIEW,
        person_label="Agente de prueba",
    )


def _services(store: AsyncMock | None) -> KitToolServices:
    return KitToolServices(store=store, public_base_url=_PUBLIC_BASE_URL)


def _definitions(services: KitToolServices) -> dict[str, object]:
    return {d.name: d for d in build_kit_tool_definitions(services)}


def test_builds_exactly_three_read_tools() -> None:
    definitions = build_kit_tool_definitions(_services(AsyncMock()))

    assert {d.name for d in definitions} == {"list_kit_files", "get_kit_text", "get_kit_file"}
    assert all(d.tool_class is ToolClass.READ for d in definitions)


def test_default_brand_name_is_generic_never_a_fixed_client_name() -> None:
    """Lane 006-cloudflare-ui (imagen publica generica): sin `brand_name`
    explicito, la descripcion nunca lleva el nombre de un cliente concreto
    a pie de letra (contracts/ci-guard.md §1)."""
    definitions = _definitions(_services(AsyncMock()))

    description = definitions["list_kit_files"].description
    assert not has_client_string(description)
    assert "Kit de Marketing de tu negocio" in description


def test_brand_name_flows_into_the_list_kit_files_description() -> None:
    services = KitToolServices(
        store=AsyncMock(), public_base_url=_PUBLIC_BASE_URL, brand_name="Acme"
    )

    description = _definitions(services)["list_kit_files"].description

    assert "Kit de Marketing de Acme" in description


async def test_list_kit_files_delegates_to_the_store() -> None:
    store = AsyncMock()
    store.list_files.return_value = [
        KitFileEntry(path="AGENTS.md", size_bytes=5, extension=".md", modified_at=_NOW)
    ]
    definitions = _definitions(_services(store))
    args = definitions["list_kit_files"].args_model(business_id=str(BusinessId.new()))

    result = await definitions["list_kit_files"].handler(args, _caller_scope())

    store.list_files.assert_awaited_once_with("", query=None, max_results=100)
    assert result == store.list_files.return_value


async def test_get_kit_text_wraps_content_with_its_real_size() -> None:
    store = AsyncMock()
    store.read_text.return_value = "guia del kit"
    definitions = _definitions(_services(store))
    args = definitions["get_kit_text"].args_model(
        business_id=str(BusinessId.new()), path="AGENTS.md"
    )

    result = await definitions["get_kit_text"].handler(args, _caller_scope())

    store.read_text.assert_awaited_once_with("AGENTS.md", max_bytes=65_536)
    assert result == KitTextFile(
        path="AGENTS.md", content="guia del kit", size_bytes=len(b"guia del kit")
    )


async def test_get_kit_file_prefixes_the_signed_url_with_the_public_base_url() -> None:
    store = AsyncMock()
    store.signed_preview_url.return_value = "/api/v1/kit-previews/01_MARCA/logo.png?exp=1&sig=a"
    definitions = _definitions(_services(store))
    args = definitions["get_kit_file"].args_model(
        business_id=str(BusinessId.new()), path="01_MARCA/logo.png"
    )

    result = await definitions["get_kit_file"].handler(args, _caller_scope())

    store.signed_preview_url.assert_awaited_once_with("01_MARCA/logo.png", ttl_s=600)
    assert result["preview_url"] == f"{_PUBLIC_BASE_URL}{store.signed_preview_url.return_value}"
    assert result["expires_in_seconds"] == 600


@pytest.mark.parametrize("tool_name", ["list_kit_files", "get_kit_text", "get_kit_file"])
async def test_every_handler_reports_kit_not_configured_when_store_is_none(tool_name: str) -> None:
    definitions = _definitions(_services(None))
    args_kwargs = {"business_id": str(BusinessId.new())}
    if tool_name != "list_kit_files":
        args_kwargs["path"] = "AGENTS.md"
    args = definitions[tool_name].args_model(**args_kwargs)

    with pytest.raises(KitNotConfiguredError):
        await definitions[tool_name].handler(args, _caller_scope())


def test_path_with_control_characters_is_rejected_at_the_args_layer() -> None:
    definitions = _definitions(_services(AsyncMock()))

    with pytest.raises(ValidationError):
        definitions["get_kit_text"].args_model(
            business_id=str(BusinessId.new()), path="AGENTS.md\x00"
        )


def test_max_results_over_the_hard_cap_is_rejected() -> None:
    definitions = _definitions(_services(AsyncMock()))

    with pytest.raises(ValidationError):
        definitions["list_kit_files"].args_model(business_id=str(BusinessId.new()), max_results=501)


def test_max_bytes_over_the_hard_cap_is_rejected() -> None:
    definitions = _definitions(_services(AsyncMock()))

    with pytest.raises(ValidationError):
        definitions["get_kit_text"].args_model(
            business_id=str(BusinessId.new()), path="AGENTS.md", max_bytes=262_145
        )


# --- build_kit_preview_router ---------------------------------------------


def _client(store: AsyncMock) -> TestClient:
    app = FastAPI()
    app.include_router(build_kit_preview_router(store))
    return TestClient(app, raise_server_exceptions=False)


def test_preview_router_serves_bytes_with_the_right_content_type() -> None:
    store = AsyncMock()
    store.open_preview.return_value = b"\x89PNG\r\n\x1a\n"
    client = _client(store)

    response = client.get("/api/v1/kit-previews/01_MARCA/logo.png?exp=123&sig=abc")

    assert response.status_code == 200
    assert response.content == b"\x89PNG\r\n\x1a\n"
    assert response.headers["content-type"] == "image/png"
    store.open_preview.assert_awaited_once_with("01_MARCA/logo.png", 123, "abc")


def test_preview_router_returns_404_on_a_rejected_key() -> None:
    """La ruta traduce CUALQUIER `KitPathRejectedError` (firma invalida,
    caducada, fuera de la raiz...) al mismo 404 uniforme -- el guard de
    traversal de verdad vive en `LocalKitStore`, probado en
    `tests/unit/mcp/infrastructure/test_kit_local_store.py`."""
    store = AsyncMock()
    store.open_preview.side_effect = KitPathRejectedError("firma invalida")
    client = _client(store)

    response = client.get("/api/v1/kit-previews/AGENTS.md?exp=123&sig=abc")

    assert response.status_code == 404

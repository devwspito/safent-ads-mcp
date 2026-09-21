"""`registries_by_permission` (004 tasks.md A5): fija el reparto de hoy y
falla al anadir una herramienta nueva sin clasificarla. Reutiliza el
constructor real de `tests/unit/bundle/test_mcp_registry_matches_overlay_
and_contract.py::_build_real_registry` (mismo catalogo completo que
`composition/app.py` cablea), mas `connection_services` (Anadido del
dueno, 15-sep), los cinco modulos de 004 tasks-2.md (I1): datos de
referencia (R3/R4), paso a traves de Meta (R5), competencia (R7), empresa
(R2/R8) y subida de creatividad (W4), `kit_services` (kit de marketing del
negocio, encargo del dueno 14-sep, +3 READ) y `cloudflare_services`
(conector Cloudflare, Anadido del dueno 14-sep, +2 READ/+2 CATALOG_WRITE)
-- el registro que este test construye es, por fin, el COMPLETO que
`composition/app.py` cablea de verdad, no un subconjunto."""

from __future__ import annotations

from typing import Final
from unittest.mock import AsyncMock

import pytest

from safent_ads.mcp.application.caller_scope import Permission
from safent_ads.mcp.presentation.catalog import build_default_registry, registries_by_permission
from safent_ads.mcp.presentation.cloudflare_tools import CloudflareToolServices
from safent_ads.mcp.presentation.company_tools import CompanyToolServices
from safent_ads.mcp.presentation.competitor_tools import CompetitorToolServices
from safent_ads.mcp.presentation.connection_tools import ConnectionToolServices
from safent_ads.mcp.presentation.creative_upload_tools import CreativeUploadToolServices
from safent_ads.mcp.presentation.kit_tools import KitToolServices
from safent_ads.mcp.presentation.native_ads_tools import NativeAdsToolServices
from safent_ads.mcp.presentation.package_tools import PackageToolServices
from safent_ads.mcp.presentation.passthrough_tools import PassthroughToolServices
from safent_ads.mcp.presentation.reference_data_tools import ReferenceDataToolServices
from safent_ads.mcp.presentation.registry import ToolClass, ToolRegistry
from safent_ads.shared.clock import SystemClock
from tests.unit.bundle.test_mcp_registry_matches_overlay_and_contract import (
    _creative_generation_services,
    _economics_service,
    _experiment_services,
    _FakeProposalWritePort,
    _opportunity_services,
    _optimization_service,
    _read_model_ports,
    _search_terms_services,
)
from tests.unit.test_no_client_strings import has_client_string

# El reparto final (004 tasks-2.md §5, contracts/mcp.md §3, ampliado por
# 003-paquete-de-campana T040 -- +1 PROPOSAL, `propose_campaign_package` --,
# por el Kit de Marketing (encargo del dueno, 14-sep) -- +3 READ,
# `list_kit_files`/`get_kit_text`/`get_kit_file` --, por el conector
# Cloudflare (Anadido del dueno, 14-sep) -- +2 READ (`list_dns_zones`/
# `list_dns_records`) y +2 CATALOG_WRITE (`upsert_dns_record`/
# `delete_dns_record`) -- y por la conexion Cloudflare gestionada desde el
# panel (lane 006-cloudflare-ui, 15-sep) -- +1 READ, `get_cloudflare_
# connection_status`):
# 75 READ / 20 PROPOSAL / 3 CATALOG_WRITE / 1 CREATIVE_WRITE visibles a
# `proponer`/`aprobar`; `aprobar` ademas ve las 2 CONNECTION_WRITE.
_EXPECTED_READ = 77  # Workspace list/detail use the same tenant scope.
_EXPECTED_PROPOSAL = 23  # Workspace context, draft and paused-review commands.
_EXPECTED_CATALOG_WRITE = 3
_EXPECTED_CREATIVE_WRITE = 1
_EXPECTED_CONNECTION_WRITE = 2


def _connection_services() -> ConnectionToolServices:
    return ConnectionToolServices(
        session_factory=AsyncMock(),
        oauth_broker=AsyncMock(),
        id_generator=AsyncMock(),
        public_base_url="https://ads.example.com",
    )


def _reference_data_services() -> ReferenceDataToolServices:
    return ReferenceDataToolServices(meta=AsyncMock(), google=AsyncMock())


def _passthrough_services() -> PassthroughToolServices:
    return PassthroughToolServices(graph=AsyncMock())


def _competitor_services() -> CompetitorToolServices:
    return CompetitorToolServices(competitor_research=AsyncMock())


def _company_services() -> CompanyToolServices:
    return CompanyToolServices(crm_summary=AsyncMock(), top_performing_ads=AsyncMock())


def _creative_upload_services() -> CreativeUploadToolServices:
    return CreativeUploadToolServices(
        briefs=AsyncMock(), upload_port=AsyncMock(), public_base_url="https://ads.example.com"
    )


def _package_services() -> PackageToolServices:
    return PackageToolServices(propose_campaign_package=AsyncMock(), packages=AsyncMock())


def _kit_services() -> KitToolServices:
    return KitToolServices(store=AsyncMock(), public_base_url="https://ads.example.com")


def _cloudflare_services() -> CloudflareToolServices:
    return CloudflareToolServices(cloudflare=AsyncMock(), connection_status=AsyncMock())


_DEFAULT_PACKAGE_SERVICES: Final = object()


def _full_registry(
    *,
    package_services: PackageToolServices | None | object = _DEFAULT_PACKAGE_SERVICES,
    brand_name: str = "tu negocio",
) -> ToolRegistry:
    """El centinela por defecto (resuelto a `_package_services()`) es el
    caso de siempre, del que dependen otros ficheros que reusan esta
    fabrica (`test_mcp_instructions_and_descriptions.py`,
    `test_tool_descriptions_and_schemas.py`) sin conocer el flag
    `ADS_CAMPAIGN_PACKAGES_ENABLED` -- `package_services=None` explicito es
    solo para los tests de este fichero que fijan el estado "apagado"."""
    if package_services is _DEFAULT_PACKAGE_SERVICES:
        package_services = _package_services()
    return build_default_registry(
        _read_model_ports(),
        SystemClock(),
        _FakeProposalWritePort(),
        experiment_services=_experiment_services(),
        search_terms_services=_search_terms_services(),
        opportunity_services=_opportunity_services(),
        economics_service=_economics_service(),
        optimization_service=_optimization_service(),
        creative_generation_services=_creative_generation_services(),
        native_ads_services=NativeAdsToolServices(AsyncMock()),
        offering_creation=AsyncMock(),
        campaign_drafts=AsyncMock(),
        connection_services=_connection_services(),
        reference_data_services=_reference_data_services(),
        passthrough_services=_passthrough_services(),
        competitor_services=_competitor_services(),
        company_services=_company_services(),
        creative_upload_services=_creative_upload_services(),
        package_services=package_services,
        kit_services=_kit_services(),
        cloudflare_services=_cloudflare_services(),
        brand_name=brand_name,
    )


@pytest.fixture(scope="module")
def full_registry() -> ToolRegistry:
    return _full_registry(package_services=_package_services())


def test_todays_split_is_pinned(full_registry: ToolRegistry) -> None:
    """Falla si alguien anade una herramienta sin darse cuenta: el total
    por clase cambia aunque el nombre sea valido segun `tool_naming.py`."""
    counts = {tool_class: 0 for tool_class in ToolClass}
    for definition in full_registry:
        counts[definition.tool_class] += 1

    assert counts[ToolClass.READ] == _EXPECTED_READ
    assert counts[ToolClass.PROPOSAL] == _EXPECTED_PROPOSAL
    assert counts[ToolClass.CATALOG_WRITE] == _EXPECTED_CATALOG_WRITE
    assert counts[ToolClass.CREATIVE_WRITE] == _EXPECTED_CREATIVE_WRITE
    assert counts[ToolClass.CONNECTION_WRITE] == _EXPECTED_CONNECTION_WRITE


# 004 tasks-2.md I1: las 6 propuestas nuevas (W2/W3) mas `upload_creative_asset`
# (W4, CREATIVE_WRITE) -- ninguna la debe ver `ver`.
_NEW_WRITE_TOOL_NAMES = (
    "propose_resume",
    "propose_bid_target",
    "propose_negative_keywords",
    "propose_creative_rotation",
    "propose_delete",
    "propose_native_write",
    "upload_creative_asset",
)


def test_ver_has_only_read_tools(full_registry: ToolRegistry) -> None:
    by_permission = registries_by_permission(full_registry)
    view_names = {d.name for d in by_permission[Permission.VIEW]}

    assert len(view_names) == _EXPECTED_READ
    for prefix in ("propose_", "withdraw_", "generate_", "apply_"):
        assert not any(name.startswith(prefix) for name in view_names)
    assert "create_offering" not in view_names
    assert "connect_platform_account" not in view_names
    assert "get_connection_status" not in view_names
    assert "list_dns_zones" in view_names
    assert "list_dns_records" in view_names
    assert "upsert_dns_record" not in view_names
    assert "delete_dns_record" not in view_names
    for name in _NEW_WRITE_TOOL_NAMES:
        assert name not in view_names


def test_proponer_adds_proposal_and_catalog_write_but_not_connection_write(
    full_registry: ToolRegistry,
) -> None:
    by_permission = registries_by_permission(full_registry)
    propose_names = {d.name for d in by_permission[Permission.PROPOSE]}

    assert len(propose_names) == (
        _EXPECTED_READ + _EXPECTED_PROPOSAL + _EXPECTED_CATALOG_WRITE + _EXPECTED_CREATIVE_WRITE
    )
    assert "create_offering" in propose_names
    assert "connect_platform_account" not in propose_names
    assert "get_connection_status" not in propose_names
    assert "upsert_dns_record" in propose_names
    assert "delete_dns_record" in propose_names
    for name in _NEW_WRITE_TOOL_NAMES:
        assert name in propose_names


def test_aprobar_adds_connection_write_on_top_of_proponer(full_registry: ToolRegistry) -> None:
    by_permission = registries_by_permission(full_registry)
    propose_names = {d.name for d in by_permission[Permission.PROPOSE]}
    approve_names = {d.name for d in by_permission[Permission.APPROVE]}

    assert approve_names - propose_names == {"connect_platform_account", "get_connection_status"}
    assert len(approve_names) == len(propose_names) + _EXPECTED_CONNECTION_WRITE
    # `aprobar` nunca registra un verbo de decision: no existe ni puede existir.
    assert not any(name.startswith(("approve_", "execute_")) for name in approve_names)


# `ADS_CAMPAIGN_PACKAGES_ENABLED` (composition/settings.py, default `False`):
# `composition/app.py::_build_mcp_registry_and_dispatcher` pasa
# `package_services=None` cuando esta apagado -- la misma senal opcional
# que ya usa el resto de modulos de `_extend_with_optional_tool_modules`,
# nunca un `if` propio dentro de `catalog.py`. `propose_campaign_package`
# es la UNICA herramienta de `package_services` (T040): apagar el flag
# quita exactamente 1 PROPOSAL, `ver` no cambia (no registra ninguna
# lectura) y `CATALOG_WRITE`/`CREATIVE_WRITE`/`CONNECTION_WRITE` tampoco.
def test_campaign_packages_flag_off_removes_propose_campaign_package_everywhere() -> None:
    registry = _full_registry(package_services=None)
    by_permission = registries_by_permission(registry)
    view_names = {d.name for d in by_permission[Permission.VIEW]}
    propose_names = {d.name for d in by_permission[Permission.PROPOSE]}
    approve_names = {d.name for d in by_permission[Permission.APPROVE]}

    assert "propose_campaign_package" not in view_names
    assert "propose_campaign_package" not in propose_names
    assert "propose_campaign_package" not in approve_names
    assert len(view_names) == _EXPECTED_READ
    assert len(propose_names) == (
        _EXPECTED_READ
        + (_EXPECTED_PROPOSAL - 1)
        + _EXPECTED_CATALOG_WRITE
        + _EXPECTED_CREATIVE_WRITE
    )
    assert len(approve_names) == len(propose_names) + _EXPECTED_CONNECTION_WRITE


def test_campaign_packages_flag_on_keeps_propose_campaign_package(
    full_registry: ToolRegistry,
) -> None:
    by_permission = registries_by_permission(full_registry)

    assert "propose_campaign_package" not in {d.name for d in by_permission[Permission.VIEW]}
    assert "propose_campaign_package" in {d.name for d in by_permission[Permission.PROPOSE]}
    assert "propose_campaign_package" in {d.name for d in by_permission[Permission.APPROVE]}


def test_list_businesses_description_is_generic_by_default_never_a_fixed_client_name() -> None:
    """Lane 006-cloudflare-ui (imagen publica generica): sin `brand_name`
    explicito, `list_businesses` nunca dice el nombre de un cliente
    concreto a pie de letra (contracts/ci-guard.md §1)."""
    registry = _full_registry()
    description = next(d.description for d in registry if d.name == "list_businesses")

    assert not has_client_string(description)
    assert "Negocios de tu negocio" in description


def test_list_businesses_description_takes_the_configured_brand_name() -> None:
    registry = _full_registry(brand_name="Acme")
    description = next(d.description for d in registry if d.name == "list_businesses")

    assert "Negocios de Acme" in description

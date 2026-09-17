"""`creative_generation_tools.py` (B-1, checklists/final-review.md):
`generate_creative_assets`/`run_creative_policy_check`, las 2 herramientas
de `creative` que la revision final marco como declaradas en el overlay
pero nunca servidas. A diferencia de `economics_tools.py`/
`optimization_tools.py` (adaptadores puros sobre un `ToolSpec` ya probado),
este modulo tiene logica propia -- el validador de `business_id` contra
`brief.business_id` y la comprobacion de propiedad de `asset_id` antes de
correr el veredicto -- asi que se prueba directamente, sin depender de un
`ToolSpec` de otra lane."""

from __future__ import annotations

from datetime import UTC, datetime

import pytest
from pydantic import ValidationError

from safent_ads.creative.application.errors import (
    RenderBudgetExceededError,
    RenderQuotaExceededError,
)
from safent_ads.creative.application.generate_creative_assets import GenerateCreativeAssets
from safent_ads.creative.application.run_policy_check import RunPolicyCheck
from safent_ads.creative.domain.enums import Placement
from safent_ads.creative.domain.identifiers import AssetId
from safent_ads.creative.domain.renderer_selector import RendererSelector
from safent_ads.creative.infrastructure.in_memory_repositories import (
    InMemoryCreativeAssetRepository,
    InMemoryCreativeBriefRepository,
    InMemoryCreativeJobRepository,
)
from safent_ads.creative.infrastructure.in_process_gpu_queue import InProcessGpuQueue
from safent_ads.creative.infrastructure.policy_checker import LocalPolicyChecker
from safent_ads.mcp.application.errors import ToolDispatchError
from safent_ads.mcp.presentation.creative_generation_tools import (
    CreativeGenerationToolServices,
    GenerateCreativeAssetsArgs,
    RunCreativePolicyCheckArgs,
    build_creative_generation_tool_definitions,
)
from safent_ads.mcp.presentation.registry import ToolClass
from safent_ads.shared.clock import FixedClock
from safent_ads.shared.ids import BusinessId, PlatformCode
from tests.unit.creative.domain.factories import make_ad_copy
from tests.unit.creative.infrastructure.fakes import make_creative_asset

_NOW = datetime(2026, 9, 10, tzinfo=UTC)
_BUSINESS_ID = "11111111-1111-1111-1111-111111111111"
_OTHER_BUSINESS_ID = "22222222-2222-2222-2222-222222222222"


def _valid_brief_payload(business_id: str) -> dict[str, object]:
    return {
        "business_id": business_id,
        "calendar_event_id": None,
        "objective": "lead",
        "audience_summary": "Adultos 25-45",
        "hook": "Tu plaza empieza aqui",
        "shots": [
            {"order": 1, "description": "Aula"},
            {"order": 2, "description": "Presentador"},
            {"order": 3, "description": "Estudiante feliz"},
        ],
        "on_screen_text": ["Plazas limitadas"],
        "cta": "Apúntate ya",
        "voiceover_lines": ["Prepárate con Negocio Ejemplo."],
        "brand_kit": {
            "primary_font": "Inter",
            "secondary_font": "Inter",
            "primary_color_hex": "#112233",
            "secondary_color_hex": "#FFFFFF",
            "logo_asset_id": str(AssetId.new()),
        },
        "source_signal_id": None,
        "variant_count": 1,
    }


def _services() -> CreativeGenerationToolServices:
    briefs = InMemoryCreativeBriefRepository()
    assets = InMemoryCreativeAssetRepository()
    jobs = InMemoryCreativeJobRepository()
    return CreativeGenerationToolServices(
        briefs=briefs,
        assets=assets,
        generate_creative_assets=GenerateCreativeAssets(
            briefs=briefs,
            jobs=jobs,
            assets=assets,
            image_renderers={},
            renderer_selector=RendererSelector(),
            gpu_lease=InProcessGpuQueue(FixedClock(_NOW)),
        ),
        run_policy_check=RunPolicyCheck(LocalPolicyChecker(assets), assets),
    )


def test_build_creative_generation_tool_definitions_registers_the_two_tools() -> None:
    definitions = {d.name: d for d in build_creative_generation_tool_definitions(_services())}

    assert set(definitions) == {"generate_creative_assets", "run_creative_policy_check"}
    assert definitions["generate_creative_assets"].tool_class is ToolClass.PROPOSAL
    assert definitions["run_creative_policy_check"].tool_class is ToolClass.READ


def test_generate_creative_assets_args_rejects_business_id_mismatch() -> None:
    with pytest.raises(ValidationError):
        GenerateCreativeAssetsArgs(
            business_id=_BUSINESS_ID, brief=_valid_brief_payload(_OTHER_BUSINESS_ID)
        )


def test_generate_creative_assets_args_accepts_matching_business_id() -> None:
    args = GenerateCreativeAssetsArgs(
        business_id=_BUSINESS_ID, brief=_valid_brief_payload(_BUSINESS_ID)
    )

    assert args.business_id == _BUSINESS_ID


async def test_generate_creative_assets_reports_capability_never_fakes_it() -> None:
    """`image_renderers={}` (mismo estado que produccion cuando el broker
    no tiene ninguna clave de proveedor configurada, `composition/app.py::
    _build_broker_image_renderers`): la cascada se agota siempre --
    `CREATIVE_RENDERER_UNAVAILABLE`, nunca un `job_id` inventado (correccion
    del propietario 2026-09-09)."""
    definitions = {d.name: d for d in build_creative_generation_tool_definitions(_services())}
    args = GenerateCreativeAssetsArgs(
        business_id=_BUSINESS_ID, brief=_valid_brief_payload(_BUSINESS_ID)
    )

    with pytest.raises(Exception) as exc_info:  # noqa: PT011 - tipo exacto abajo
        await definitions["generate_creative_assets"].handler(args, None)

    assert type(exc_info.value).__name__ == "CreativeRendererUnavailableError"


async def test_run_creative_policy_check_rejects_asset_from_another_business() -> None:
    services = _services()
    asset = make_creative_asset(business_id=BusinessId.parse(_OTHER_BUSINESS_ID))
    await services.assets.add(asset)
    definitions = {d.name: d for d in build_creative_generation_tool_definitions(services)}
    args = RunCreativePolicyCheckArgs(
        business_id=_BUSINESS_ID,
        asset_id=str(asset.asset_id),
        platform=PlatformCode.GOOGLE,
        placement=Placement.FEED,
    )

    with pytest.raises(Exception) as exc_info:  # noqa: PT011 - tipo exacto abajo
        await definitions["run_creative_policy_check"].handler(args, None)

    assert type(exc_info.value).__name__ == "EntityNotFoundError"


async def test_run_creative_policy_check_rejects_asset_without_ad_copy() -> None:
    services = _services()
    asset = make_creative_asset(business_id=BusinessId.parse(_BUSINESS_ID), ad_copy=None)
    await services.assets.add(asset)
    definitions = {d.name: d for d in build_creative_generation_tool_definitions(services)}
    args = RunCreativePolicyCheckArgs(
        business_id=_BUSINESS_ID,
        asset_id=str(asset.asset_id),
        platform=PlatformCode.GOOGLE,
        placement=Placement.FEED,
    )

    with pytest.raises(Exception) as exc_info:  # noqa: PT011 - tipo exacto abajo
        await definitions["run_creative_policy_check"].handler(args, None)

    assert type(exc_info.value).__name__ == "ToolValidationError"


async def test_run_creative_policy_check_returns_verdict_for_own_asset() -> None:
    services = _services()
    asset = make_creative_asset(business_id=BusinessId.parse(_BUSINESS_ID), ad_copy=make_ad_copy())
    await services.assets.add(asset)
    definitions = {d.name: d for d in build_creative_generation_tool_definitions(services)}
    args = RunCreativePolicyCheckArgs(
        business_id=_BUSINESS_ID,
        asset_id=str(asset.asset_id),
        platform=PlatformCode.GOOGLE,
        placement=Placement.FEED,
    )

    result = await definitions["run_creative_policy_check"].handler(args, None)

    assert "verdict" in result
    assert "findings" in result


class _RaisingGenerate:
    def __init__(self, exc: Exception) -> None:
        self._exc = exc

    async def execute(self, _brief_id: object) -> list[object]:
        raise self._exc


@pytest.mark.parametrize(
    ("raised", "expected"),
    [
        (RenderQuotaExceededError("cuota"), "RENDER_QUOTA_EXCEEDED"),
        (RenderBudgetExceededError("tope"), "RENDER_COST_CAP"),
    ],
)
async def test_generate_creative_assets_translates_broker_quota_and_cost_cap(
    raised: Exception, expected: str
) -> None:
    """M-2 (revision 0.2.22): la cuota por negocio y el tope de coste que
    aplica `ads-broker` llegan al arnés como error tipado del sobre MCP,
    nunca como `TOOL_FAILED` opaco."""
    base = _services()
    services = CreativeGenerationToolServices(
        briefs=base.briefs,
        assets=base.assets,
        generate_creative_assets=_RaisingGenerate(raised),  # type: ignore[arg-type]
        run_policy_check=base.run_policy_check,
    )
    definitions = {d.name: d for d in build_creative_generation_tool_definitions(services)}
    args = GenerateCreativeAssetsArgs(
        business_id=_BUSINESS_ID, brief=_valid_brief_payload(_BUSINESS_ID)
    )

    with pytest.raises(ToolDispatchError) as exc_info:
        await definitions["generate_creative_assets"].handler(args, None)

    assert exc_info.value.code == expected

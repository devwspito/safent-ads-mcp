"""`propose_campaign_package` vs `ADS_GOOGLE_CHANNELS_ENABLED` (tasks.md
T076, POLISH "canales por configuracion"): el gate de seguridad T035 solo
permite publicar 0.2.24 con DISPLAY/DEMAND_GEN/PERFORMANCE_MAX apagados.
`_decode_campaign_native` rechaza el canal ANTES de `execute()` -- nunca
toca un puerto -- con el codigo tipado `CHANNEL_TYPE_NOT_ENABLED`, distinto
de `CHANNEL_TYPE_NOT_SUPPORTED` (ese, retirado, cubria una forma fuera de
la union discriminada; este cubre una fila valida que la instalacion no
tiene encendida)."""

from __future__ import annotations

from typing import Any
from unittest.mock import AsyncMock

import pytest

from safent_ads.mcp.application.caller_scope import CallerScope, Permission
from safent_ads.mcp.presentation.package_tools import (
    ChannelTypeNotEnabledToolError,
    PackageToolServices,
    ProposeCampaignPackageArgs,
    build_package_tool_definitions,
)
from safent_ads.proposals.domain.google_channel_spec import GoogleAdvertisingChannelType

from .test_propose_campaign_package import google_1x3_payload, performance_max_1_asset_group_payload

_CALLER = CallerScope(
    caller_id="test-agent",
    allowed_business_ids=frozenset({"11111111-1111-1111-1111-111111111111"}),
    permission=Permission.PROPOSE,
    person_label="Agente de prueba",
)


def _handler(
    *,
    enabled_google_channels: frozenset[GoogleAdvertisingChannelType],
    execute_side_effect: BaseException,
) -> Any:
    """`execute()` nunca resuelve un paquete de verdad -- un `side_effect`
    que la union tipada del handler no atrapa demuestra, si se propaga tal
    cual, que la llamada SI llego a `execute()` (canal aceptado)."""
    use_case = AsyncMock()
    use_case.execute.side_effect = execute_side_effect
    services = PackageToolServices(
        propose_campaign_package=use_case,
        packages=AsyncMock(),
        enabled_google_channels=enabled_google_channels,
    )
    return build_package_tool_definitions(services)[0].handler, use_case


class _ReachedExecute(RuntimeError):
    pass


async def test_performance_max_denied_by_default_before_touching_execute() -> None:
    handler, use_case = _handler(
        enabled_google_channels=frozenset({GoogleAdvertisingChannelType.SEARCH}),
        execute_side_effect=_ReachedExecute(),
    )
    args = ProposeCampaignPackageArgs.model_validate(performance_max_1_asset_group_payload())

    with pytest.raises(ChannelTypeNotEnabledToolError) as excinfo:
        await handler(args, _CALLER)

    assert excinfo.value.code == "CHANNEL_TYPE_NOT_ENABLED"
    use_case.execute.assert_not_awaited()


async def test_search_stays_allowed_by_default() -> None:
    handler, use_case = _handler(
        enabled_google_channels=frozenset({GoogleAdvertisingChannelType.SEARCH}),
        execute_side_effect=_ReachedExecute(),
    )
    args = ProposeCampaignPackageArgs.model_validate(google_1x3_payload())

    with pytest.raises(_ReachedExecute):
        await handler(args, _CALLER)

    use_case.execute.assert_awaited_once()


async def test_performance_max_accepted_when_enabled_via_env() -> None:
    handler, use_case = _handler(
        enabled_google_channels=frozenset(
            {GoogleAdvertisingChannelType.SEARCH, GoogleAdvertisingChannelType.PERFORMANCE_MAX}
        ),
        execute_side_effect=_ReachedExecute(),
    )
    args = ProposeCampaignPackageArgs.model_validate(performance_max_1_asset_group_payload())

    with pytest.raises(_ReachedExecute):
        await handler(args, _CALLER)

    use_case.execute.assert_awaited_once()

"""Mapeo de los codigos §5 (contracts/mcp-tools.md) al limite MCP en
`_propose_campaign_package` (revision 0.2.24, hallazgo de revision de
005 US1): `DAILY_BUDGET_BELOW_CHANNEL_
MINIMUM`/`DURATION_BELOW_CHANNEL_MINIMUM` -- los dos unicos vivos antes de
esta revision -- nunca llegaban tipados al modelo porque el handler no los
atrapaba; `BIDDING_NOT_ALLOWED_FOR_CHANNEL`, `CONVERSION_ACTION_REQUIRED`,
`ASSET_GROUP_INCOMPLETE` y `CREATIVE_ASPECT_RATIO_INVALID` eran codigo
muerto por completo. `CHANNEL_TYPE_NOT_SUPPORTED` no aparece aqui: la union
discriminada de canal hace que ningun camino real pueda levantarlo (ver
`packages/application/errors.py`); `CONVERSION_ACTION_NOT_USABLE` tampoco:
lo verifica el bróker por GAQL (T032), fuera de este caso de uso.

Los minimos por canal (T026) tambien se movieron de `ProposeCampaignPackage.
execute()` al dominio (`PlannedCampaign.__post_init__`) -- se prueban aqui
con la llamada REAL a `_decode_campaign`/`_decode_campaign_native` (no un
`execute()` de mentira), igual que bidding/metas de conversion."""

from __future__ import annotations

from typing import Any
from unittest.mock import AsyncMock

import pytest

from safent_ads.mcp.application.caller_scope import CallerScope, Permission
from safent_ads.mcp.presentation.package_tools import (
    AssetGroupIncompleteToolError,
    BiddingNotAllowedForChannelToolError,
    ConversionActionRequiredToolError,
    CreativeAspectRatioInvalidToolError,
    DailyBudgetBelowChannelMinimumToolError,
    DurationBelowChannelMinimumToolError,
    PackageToolServices,
    ProposeCampaignPackageArgs,
    build_package_tool_definitions,
)
from safent_ads.packages.application.errors import (
    AssetGroupIncompleteError,
    CreativeAspectRatioInvalidError,
    DailyBudgetBelowChannelMinimumError,
    DurationBelowChannelMinimumError,
)
from safent_ads.proposals.domain.google_channel_spec import GoogleAdvertisingChannelType

from .test_google_channel_union import _campaign_for, _display_native
from .test_propose_campaign_package import google_1x3_payload, performance_max_1_asset_group_payload

_CALLER = CallerScope(
    caller_id="test-agent",
    allowed_business_ids=frozenset({"11111111-1111-1111-1111-111111111111"}),
    permission=Permission.PROPOSE,
    person_label="Agente de prueba",
)


def _handler(execute_side_effect: BaseException | None = None) -> Any:
    """`execute()` nunca corre de verdad -- los casos que se resuelven en
    el decodificador (bidding/metas de conversion) lo demuestran pasando
    `None` y dejando que un `AsyncMock` sin `side_effect` reviente si
    alguna vez se llega a invocar."""
    use_case = AsyncMock()
    if execute_side_effect is not None:
        use_case.execute.side_effect = execute_side_effect
    # T076 (POLISH): this file exercises the decode-time channel/bidding/
    # conversion-goal errors, not the ADS_GOOGLE_CHANNELS_ENABLED gate --
    # every row stays on so the DISPLAY/PERFORMANCE_MAX payloads below keep
    # exercising what they always tested.
    services = PackageToolServices(
        propose_campaign_package=use_case,
        packages=AsyncMock(),
        enabled_google_channels=frozenset(GoogleAdvertisingChannelType),
    )
    return build_package_tool_definitions(services)[0].handler


class TestLosDosErroresVivosLlegaronSiempreTipados:
    async def test_presupuesto_por_debajo_del_minimo_llega_como_tool_error(self) -> None:
        handler = _handler(
            DailyBudgetBelowChannelMinimumError(minimum="20.00", channel="PERFORMANCE_MAX")
        )
        args = ProposeCampaignPackageArgs.model_validate(google_1x3_payload())

        with pytest.raises(DailyBudgetBelowChannelMinimumToolError) as excinfo:
            await handler(args, _CALLER)
        assert excinfo.value.code == "DAILY_BUDGET_BELOW_CHANNEL_MINIMUM"

    async def test_duracion_por_debajo_del_minimo_llega_como_tool_error(self) -> None:
        handler = _handler(DurationBelowChannelMinimumError(minimum=14))
        args = ProposeCampaignPackageArgs.model_validate(google_1x3_payload())

        with pytest.raises(DurationBelowChannelMinimumToolError) as excinfo:
            await handler(args, _CALLER)
        assert excinfo.value.code == "DURATION_BELOW_CHANNEL_MINIMUM"

    async def test_presupuesto_insuficiente_falla_antes_de_llegar_al_caso_de_uso(self) -> None:
        """Revision 0.2.24: el minimo por canal ahora lo exige `Planned
        Campaign.__post_init__` (dominio) al decodificar, no `execute()`
        -- `execute()` nunca deberia correr para esta llamada."""
        handler = _handler()
        payload = performance_max_1_asset_group_payload()
        payload["campaign"]["daily_budget"] = {"amount": "15.00", "currency": "EUR"}
        args = ProposeCampaignPackageArgs.model_validate(payload)

        with pytest.raises(DailyBudgetBelowChannelMinimumToolError) as excinfo:
            await handler(args, _CALLER)
        assert excinfo.value.code == "DAILY_BUDGET_BELOW_CHANNEL_MINIMUM"

    async def test_duracion_insuficiente_falla_antes_de_llegar_al_caso_de_uso(self) -> None:
        handler = _handler()
        payload = performance_max_1_asset_group_payload()
        payload["campaign"]["duration_days"] = 7
        args = ProposeCampaignPackageArgs.model_validate(payload)

        with pytest.raises(DurationBelowChannelMinimumToolError) as excinfo:
            await handler(args, _CALLER)
        assert excinfo.value.code == "DURATION_BELOW_CHANNEL_MINIMUM"


class TestLosCuatroReciencableadosLlegaTipados:
    async def test_grupo_de_recursos_incompleto_llega_como_tool_error(self) -> None:
        handler = _handler(AssetGroupIncompleteError(missing=("asset_group_headline",)))
        args = ProposeCampaignPackageArgs.model_validate(google_1x3_payload())

        with pytest.raises(AssetGroupIncompleteToolError) as excinfo:
            await handler(args, _CALLER)
        assert excinfo.value.code == "ASSET_GROUP_INCOMPLETE"

    async def test_relacion_de_aspecto_invalida_llega_como_tool_error(self) -> None:
        handler = _handler(CreativeAspectRatioInvalidError(expected="1:1", ad_local_ref="as#1"))
        args = ProposeCampaignPackageArgs.model_validate(google_1x3_payload())

        with pytest.raises(CreativeAspectRatioInvalidToolError) as excinfo:
            await handler(args, _CALLER)
        assert excinfo.value.code == "CREATIVE_ASPECT_RATIO_INVALID"

    async def test_bidding_no_permitida_falla_antes_de_llegar_al_caso_de_uso(self) -> None:
        handler = _handler()
        payload = _campaign_for(_display_native())
        payload["campaign"]["native"]["bidding_strategy"] = {
            "kind": "MAXIMIZE_CONVERSION_VALUE",
            "target_roas": "3.5",
        }
        args = ProposeCampaignPackageArgs.model_validate(payload)

        with pytest.raises(BiddingNotAllowedForChannelToolError) as excinfo:
            await handler(args, _CALLER)
        assert excinfo.value.code == "BIDDING_NOT_ALLOWED_FOR_CHANNEL"

    async def test_meta_de_conversion_requerida_falla_antes_de_llegar_al_caso_de_uso(
        self,
    ) -> None:
        handler = _handler()
        payload = _campaign_for(_display_native())
        payload["campaign"]["native"]["bidding_strategy"] = {"kind": "MAXIMIZE_CONVERSIONS"}
        args = ProposeCampaignPackageArgs.model_validate(payload)

        with pytest.raises(ConversionActionRequiredToolError) as excinfo:
            await handler(args, _CALLER)
        assert excinfo.value.code == "CONVERSION_ACTION_REQUIRED"

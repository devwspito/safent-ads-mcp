"""Decodificador `Args -> dominio` de `propose_campaign_package` (tasks.md
T028; contracts/mcp-tools.md §2, §4, §5). Cubre `_decode_campaign_native`
para DISPLAY/DEMAND_GEN/PERFORMANCE_MAX -- SEARCH ya esta congelado por
`test_search_native_args_wire_shape_unchanged.py` -- y `_decode_ad_set` para
el grupo de recursos de Maximo Rendimiento: el `TODO(T021, lane/005-us1-
dominio)` que este modulo cierra."""

from __future__ import annotations

import copy
from decimal import Decimal

import pytest

from safent_ads.mcp.presentation.package_tools import (
    PlannedAdSetInput,
    ProposeCampaignPackageArgs,
    _decode_ad_set,
    _decode_campaign_native,
)
from safent_ads.packages.application.errors import (
    BiddingNotAllowedForChannelError,
    ConversionActionRequiredError,
)
from safent_ads.packages.application.propose_campaign_package import PlannedAssetGroupInput
from safent_ads.packages.domain.planned_tree import (
    GoogleDemandGenCampaignNative,
    GoogleDisplayCampaignNative,
    GooglePerformanceMaxCampaignNative,
)
from safent_ads.proposals.domain.google_bidding import (
    GoogleBiddingError,
    ManualCpc,
    MaximizeConversions,
    MaximizeConversionValue,
)
from safent_ads.proposals.domain.google_channel_spec import GoogleAdvertisingChannelType

from .test_google_channel_union import (
    _RESOURCE_NAME,
    _campaign_for,
    _demand_gen_native,
    _display_native,
    _performance_max_native,
)
from .test_package_tools_urls import _ASSET_GROUP_AD_SET

# T076 (POLISH): this module exercises the decoder's own logic (bidding/
# conversion-goal/forced-literal translation), not the ADS_GOOGLE_CHANNELS_
# ENABLED gate -- every row stays on.
_ALL_CHANNELS = frozenset(GoogleAdvertisingChannelType)


def _campaign_native(native_payload: dict) -> object:
    payload = _campaign_for(native_payload)
    args = ProposeCampaignPackageArgs.model_validate(payload)
    return args.campaign.native


class TestDecodeCampaignNativeHappyPath:
    def test_display_decodifica_a_su_variante_de_dominio(self) -> None:
        decoded = _decode_campaign_native(_campaign_native(_display_native()), _ALL_CHANNELS)

        assert isinstance(decoded, GoogleDisplayCampaignNative)
        assert isinstance(decoded.bidding_strategy, ManualCpc)
        assert decoded.geographic_targeting is None
        assert decoded.conversion_goals == ()

    def test_demand_gen_decodifica_a_su_variante_de_dominio(self) -> None:
        decoded = _decode_campaign_native(_campaign_native(_demand_gen_native()), _ALL_CHANNELS)

        assert isinstance(decoded, GoogleDemandGenCampaignNative)
        assert isinstance(decoded.bidding_strategy, MaximizeConversions)
        assert [goal.resource_name for goal in decoded.conversion_goals] == [_RESOURCE_NAME]

    def test_performance_max_decodifica_a_su_variante_de_dominio(self) -> None:
        decoded = _decode_campaign_native(
            _campaign_native(_performance_max_native()), _ALL_CHANNELS
        )

        assert isinstance(decoded, GooglePerformanceMaxCampaignNative)
        assert isinstance(decoded.bidding_strategy, MaximizeConversionValue)
        assert decoded.bidding_strategy.target_roas == Decimal("3.5")

    def test_los_literales_forzados_no_los_manda_el_cliente_los_inyecta_el_dominio(self) -> None:
        """BL-1 capa 3->1 (contracts/mcp-tools.md §4): el decodificador nunca
        lee `url_expansion_opt_out` -- no es un campo, `extra="forbid"` lo
        rechazaria si el cliente lo intentase. Es `GoogleChannelSpec.
        forced_literals`, aplicado por el propio `to_canonical()` del
        dominio (`planned_tree._forced_literal_campaign_native`)."""
        decoded = _decode_campaign_native(
            _campaign_native(_performance_max_native()), _ALL_CHANNELS
        )

        canonical = decoded.to_canonical()

        assert canonical["url_expansion_opt_out"] is True
        assert canonical["text_asset_automation_enabled"] is False


class TestDecodeCampaignNativeTypedErrors:
    def test_bidding_no_permitida_en_el_canal_falla_en_el_decodificador(self) -> None:
        """`BIDDING_NOT_ALLOWED_FOR_CHANNEL` (contracts/mcp-tools.md §5):
        DISPLAY admite el objeto de puja completo a nivel de esquema, pero
        `MAXIMIZE_CONVERSION_VALUE` no esta en la fila de DISPLAY -- el
        rechazo lo hace el dominio al decodificar (revision 0.2.24: ya no
        escapa como `PlannedTreeError` crudo, se traduce al codigo exacto
        del contrato)."""
        native = _display_native()
        native["bidding_strategy"] = {"kind": "MAXIMIZE_CONVERSION_VALUE", "target_roas": "3.5"}

        with pytest.raises(BiddingNotAllowedForChannelError) as excinfo:
            _decode_campaign_native(_campaign_native(native), _ALL_CHANNELS)
        assert excinfo.value.code == "BIDDING_NOT_ALLOWED_FOR_CHANNEL"
        assert excinfo.value.channel == "DISPLAY"
        assert sorted(excinfo.value.allowed) == [
            "MANUAL_CPC",
            "MAXIMIZE_CLICKS",
            "MAXIMIZE_CONVERSIONS",
        ]

    def test_puja_por_conversiones_sin_metas_falla_en_el_decodificador(self) -> None:
        """`CONVERSION_ACTION_REQUIRED` (contracts/mcp-tools.md §5): DISPLAY
        no obliga `conversion_goals` a nivel de esquema, pero elegir una
        puja por conversiones si lo exige a nivel de dominio (revision
        0.2.24: traducido al codigo exacto, ya no un `PlannedTreeError`
        crudo)."""
        native = _display_native()
        native["bidding_strategy"] = {"kind": "MAXIMIZE_CONVERSIONS"}

        with pytest.raises(ConversionActionRequiredError) as excinfo:
            _decode_campaign_native(_campaign_native(native), _ALL_CHANNELS)
        assert excinfo.value.code == "CONVERSION_ACTION_REQUIRED"

    def test_target_roas_fuera_de_rango_falla_en_el_decodificador(self) -> None:
        """La puja se decodifica como objeto etiquetado (`google_bidding.
        py`), no como texto suelto: `target_roas` fuera de rango lo rechaza
        el propio value object, no el esquema (que solo valida el patron)."""
        native = _performance_max_native()
        native["bidding_strategy"] = {"kind": "MAXIMIZE_CONVERSION_VALUE", "target_roas": "0.00"}

        with pytest.raises(GoogleBiddingError, match="google_bidding_target_roas_out_of_range"):
            _decode_campaign_native(_campaign_native(native), _ALL_CHANNELS)


class TestDecodeAdSetAssetGroup:
    def test_grupo_de_recursos_decodifica_a_un_dto_sin_resolver(self) -> None:
        """El decodificador se detiene en `PlannedAssetGroupInput` (mismo
        motivo que `PlannedAdInput.creative_asset_id: str | None`):
        resolver un `asset_id` en un `ImageCreativeRef` verificado exige
        `CreativeAssetLookupPort` (E/S), fuera de esta capa."""
        payload = _campaign_for(_performance_max_native())
        payload["ad_sets"] = [copy.deepcopy(_ASSET_GROUP_AD_SET)]
        args = ProposeCampaignPackageArgs.model_validate(payload)

        decoded = _decode_ad_set(args.ad_sets[0])

        assert isinstance(decoded, PlannedAdSetInput)
        assert isinstance(decoded.native, PlannedAssetGroupInput)
        assert decoded.native.final_url == "https://clinicax.example/reservar"
        assert decoded.native.logo_asset_id == "asset-logo"
        assert decoded.native.business_name == "Clinica X"
        assert decoded.ads == ()
        assert decoded.keywords is None
        assert decoded.cpc_bid is None

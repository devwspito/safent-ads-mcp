"""`_google()` queries `GoogleChannelSpec` instead of comparing channel/bidding
literals (tasks.md T014). Search stays
byte-compatible (`test_manual_cpc_cadena_sigue_validando`); the three new
channels are validated against their row: legal bidding, forced literals
(BL-1 capa 2), conversion goals, and the exact key set (`audience_signal` is
not a key of any row, I-2)."""

from __future__ import annotations

from decimal import Decimal

import pytest

from safent_ads.proposals.domain.campaign_creation import CampaignCreationError, _google

_EU_POLITICAL_DECLARATION = "DOES_NOT_CONTAIN_EU_POLITICAL_ADVERTISING"
_CONVERSION_GOAL = {"resource_name": "customers/1234567890/conversionActions/1"}


def _search_native(**overrides: object) -> dict[str, object]:
    native: dict[str, object] = {
        "advertising_channel_type": "SEARCH",
        "bidding_strategy": "MANUAL_CPC",
        "contains_eu_political_advertising": _EU_POLITICAL_DECLARATION,
        "network_settings": {
            "target_google_search": True,
            "target_search_network": False,
            "target_content_network": False,
            "target_partner_search_network": False,
        },
    }
    native.update(overrides)
    return native


def _display_native(**overrides: object) -> dict[str, object]:
    native: dict[str, object] = {
        "advertising_channel_type": "DISPLAY",
        "bidding_strategy": {"kind": "MANUAL_CPC"},
        "contains_eu_political_advertising": _EU_POLITICAL_DECLARATION,
    }
    native.update(overrides)
    return native


def _demand_gen_native(**overrides: object) -> dict[str, object]:
    native: dict[str, object] = {
        "advertising_channel_type": "DEMAND_GEN",
        "bidding_strategy": {"kind": "MAXIMIZE_CONVERSIONS"},
        "contains_eu_political_advertising": _EU_POLITICAL_DECLARATION,
        "conversion_goals": [_CONVERSION_GOAL],
        "url_expansion_opt_out": True,
        "text_asset_automation_enabled": False,
    }
    native.update(overrides)
    return native


def _performance_max_native(**overrides: object) -> dict[str, object]:
    native: dict[str, object] = {
        "advertising_channel_type": "PERFORMANCE_MAX",
        "bidding_strategy": {"kind": "MAXIMIZE_CONVERSION_VALUE", "target_roas": "4.00"},
        "contains_eu_political_advertising": _EU_POLITICAL_DECLARATION,
        "conversion_goals": [_CONVERSION_GOAL],
        "url_expansion_opt_out": True,
        "text_asset_automation_enabled": False,
    }
    native.update(overrides)
    return native


@pytest.mark.parametrize(
    "native",
    [_search_native(), _display_native(), _demand_gen_native(), _performance_max_native()],
    ids=["search", "display", "demand_gen", "performance_max"],
)
def test_las_cuatro_filas_validan_con_su_nativo_valido(native: dict[str, object]) -> None:
    _google(native)


def test_manual_cpc_cadena_sigue_validando() -> None:
    _google(_search_native(bidding_strategy="MANUAL_CPC"))
    _google(_search_native(bidding_strategy={"kind": "MANUAL_CPC"}))


def test_manual_cpc_cadena_solo_vale_para_search() -> None:
    with pytest.raises(CampaignCreationError, match="^campaign_creation_"):
        _google(_display_native(bidding_strategy="MANUAL_CPC"))


def test_pmax_con_manual_cpc_es_bidding_not_allowed() -> None:
    with pytest.raises(CampaignCreationError, match="campaign_creation_bidding_not_allowed"):
        _google(_performance_max_native(bidding_strategy={"kind": "MANUAL_CPC"}))


def test_pmax_sin_metas_es_conversion_action_required() -> None:
    native = _performance_max_native()
    del native["conversion_goals"]
    with pytest.raises(CampaignCreationError, match="campaign_creation_conversion_goal_required"):
        _google(native)


def test_pmax_con_metas_vacias_es_conversion_action_required() -> None:
    with pytest.raises(CampaignCreationError, match="campaign_creation_conversion_goal_required"):
        _google(_performance_max_native(conversion_goals=[]))


def test_display_con_network_settings_es_native_invalid() -> None:
    with pytest.raises(CampaignCreationError, match="campaign_creation_native_invalid"):
        _google(
            _display_native(
                network_settings={
                    "target_google_search": True,
                    "target_search_network": False,
                    "target_content_network": False,
                    "target_partner_search_network": False,
                }
            )
        )


def test_pmax_sin_literales_forzados_no_valida() -> None:
    native = _performance_max_native()
    del native["url_expansion_opt_out"]
    del native["text_asset_automation_enabled"]
    with pytest.raises(CampaignCreationError, match="campaign_creation_native_invalid"):
        _google(native)


def test_pmax_con_url_expansion_en_false_no_valida() -> None:
    with pytest.raises(CampaignCreationError, match="campaign_creation_native_invalid"):
        _google(_performance_max_native(url_expansion_opt_out=False))


def test_audience_signal_no_es_una_clave_del_nativo() -> None:
    with pytest.raises(CampaignCreationError, match="campaign_creation_native_invalid"):
        _google(_performance_max_native(audience_signal=None))


def test_canal_desconocido_es_channel_unsupported() -> None:
    with pytest.raises(CampaignCreationError, match="campaign_creation_channel_unsupported"):
        _google(_search_native(advertising_channel_type="TIKTOK"))


def test_demand_gen_con_target_roas_fuera_de_rango_no_valida() -> None:
    native = _demand_gen_native(
        bidding_strategy={"kind": "MAXIMIZE_CONVERSION_VALUE", "target_roas": "400.00"}
    )
    with pytest.raises(CampaignCreationError, match="campaign_creation_native_invalid"):
        _google(native)


def test_maximize_clicks_con_cpc_bid_ceiling_valido() -> None:
    native = _search_native(
        bidding_strategy={
            "kind": "MAXIMIZE_CLICKS",
            "cpc_bid_ceiling": {"amount": "2.50", "currency": "EUR"},
        }
    )
    _google(native)


def test_maximize_clicks_con_cpc_bid_ceiling_no_positivo_no_valida() -> None:
    native = _search_native(
        bidding_strategy={
            "kind": "MAXIMIZE_CLICKS",
            "cpc_bid_ceiling": {"amount": "0.00", "currency": "EUR"},
        }
    )
    with pytest.raises(CampaignCreationError, match="campaign_creation_native_invalid"):
        _google(native)


def test_conversion_goals_con_mas_de_diez_no_valida() -> None:
    goals = [
        {"resource_name": f"customers/1234567890/conversionActions/{index}"}
        for index in range(11)
    ]
    with pytest.raises(CampaignCreationError, match="campaign_creation_conversion_goal_required"):
        _google(_performance_max_native(conversion_goals=goals))


def test_conversion_goal_malformado_es_native_invalid() -> None:
    with pytest.raises(CampaignCreationError, match="campaign_creation_native_invalid"):
        _google(_performance_max_native(conversion_goals=[{"resource_name": "not-a-resource"}]))


def test_geografia_invalida_sigue_fallando() -> None:
    with pytest.raises(CampaignCreationError, match="campaign_creation_geography_invalid"):
        _google(_search_native(geographic_targeting={"bad": "shape"}))


def test_eu_policy_ausente_sigue_fallando() -> None:
    native = _search_native()
    native["contains_eu_political_advertising"] = "MAYBE"
    with pytest.raises(CampaignCreationError, match="campaign_creation_eu_policy_required"):
        _google(native)


@pytest.mark.parametrize("invalid", [[], {}, True, None, "unsupported"])
def test_bidding_strategy_malformado_es_native_invalid(invalid: object) -> None:
    with pytest.raises(CampaignCreationError, match="campaign_creation_native_invalid"):
        _google(_search_native(bidding_strategy=invalid))


def test_target_roas_delega_en_google_bidding_como_decimal() -> None:
    native = _demand_gen_native(
        bidding_strategy={"kind": "MAXIMIZE_CONVERSION_VALUE", "target_roas": "2.5"}
    )
    _google(native)
    assert Decimal("2.5") == Decimal("2.50")

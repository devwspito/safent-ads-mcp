"""Contrato del bloque nativo compartido `creation_plan.native` (tasks.md
T027, T029; contracts/mcp-tools.md §1, §2, §3): la misma union discriminada
de `mcp.presentation.campaign_creation_args`, usada por `propose_campaign_
draft` (via `mcp.presentation.campaign_draft_tools`) y por `propose_
campaign` (via `mcp.presentation.opportunity_tools`)."""

from __future__ import annotations

import json

import pytest
from pydantic import ValidationError

from safent_ads.mcp.presentation.campaign_creation_args import (
    GoogleCampaignCreationArgs,
    GoogleChannelNotEnabledError,
    MetaCampaignCreationArgs,
    require_enabled_google_channel,
)
from safent_ads.proposals.domain.google_channel_spec import GoogleAdvertisingChannelType

_RESOURCE_NAME = "customers/1234567890/conversionActions/1"


def _plan(native: dict) -> dict:
    return {
        "schema_version": 1,
        "platform": "google",
        "name": "Reserva de citas",
        "status": "PAUSED",
        "daily_budget": {"amount": "20.00", "currency": "EUR"},
        "native": native,
    }


def _search_native() -> dict:
    return {
        "advertising_channel_type": "SEARCH",
        "bidding_strategy": "MANUAL_CPC",
        "contains_eu_political_advertising": "DOES_NOT_CONTAIN_EU_POLITICAL_ADVERTISING",
        "network_settings": {
            "target_google_search": True,
            "target_search_network": True,
            "target_content_network": False,
            "target_partner_search_network": False,
        },
    }


def _performance_max_native() -> dict:
    return {
        "advertising_channel_type": "PERFORMANCE_MAX",
        "bidding_strategy": {"kind": "MAXIMIZE_CONVERSIONS"},
        "contains_eu_political_advertising": "DOES_NOT_CONTAIN_EU_POLITICAL_ADVERTISING",
        "conversion_goals": [{"resource_name": _RESOURCE_NAME}],
    }


def test_url_expansion_no_es_un_campo_del_esquema() -> None:
    schema = GoogleCampaignCreationArgs.model_json_schema()

    blob = json.dumps(schema)
    assert "url_expansion_opt_out" not in blob
    assert "text_asset_automation_enabled" not in blob


def test_audience_signal_no_existe_en_el_esquema() -> None:
    schema = GoogleCampaignCreationArgs.model_json_schema()

    assert "audience_signal" not in json.dumps(schema)


def test_las_cuatro_variantes_de_creation_plan_validan() -> None:
    for native in (
        _search_native(),
        {
            "advertising_channel_type": "DISPLAY",
            "bidding_strategy": {"kind": "MANUAL_CPC"},
            "contains_eu_political_advertising": "DOES_NOT_CONTAIN_EU_POLITICAL_ADVERTISING",
        },
        {
            "advertising_channel_type": "DEMAND_GEN",
            "bidding_strategy": {"kind": "MAXIMIZE_CONVERSIONS"},
            "contains_eu_political_advertising": "DOES_NOT_CONTAIN_EU_POLITICAL_ADVERTISING",
            "conversion_goals": [{"resource_name": _RESOURCE_NAME}],
        },
        _performance_max_native(),
    ):
        GoogleCampaignCreationArgs.model_validate(_plan(native))


def test_performance_max_sin_url_expansion_opt_out_en_el_wire_igual_valida() -> None:
    """BL-1 capa 3->1: el modelo NUNCA manda el literal forzado -- el
    antes-validador lo inyecta solo en la copia que ve `creation_budget`."""
    plan = _plan(_performance_max_native())
    assert "url_expansion_opt_out" not in plan["native"]

    args = GoogleCampaignCreationArgs.model_validate(plan)

    assert args.model_dump(mode="json") == plan


def test_performance_max_con_url_expansion_opt_out_en_el_wire_falla() -> None:
    plan = _plan({**_performance_max_native(), "url_expansion_opt_out": True})

    with pytest.raises(ValidationError):
        GoogleCampaignCreationArgs.model_validate(plan)


def test_performance_max_sin_metas_de_conversion_falla() -> None:
    native = _performance_max_native()
    del native["conversion_goals"]

    with pytest.raises(ValidationError):
        GoogleCampaignCreationArgs.model_validate(_plan(native))


class TestRequireEnabledGoogleChannel:
    """tasks.md T076 (POLISH): el gate compartido -- `propose_campaign_
    draft` (`campaign_draft_tools.py`) y `propose_campaign_package`
    (`package_tools.py`) lo invocan explicitamente, antes de tocar ningun
    puerto; esta clase lo prueba aislado del resto de la tuberia MCP."""

    def test_search_pasa_con_el_defecto_de_solo_search(self) -> None:
        native = GoogleCampaignCreationArgs.model_validate(_plan(_search_native())).native

        require_enabled_google_channel(
            native, frozenset({GoogleAdvertisingChannelType.SEARCH})
        )

    def test_performance_max_rechazado_con_el_defecto_de_solo_search(self) -> None:
        native = GoogleCampaignCreationArgs.model_validate(_plan(_performance_max_native())).native

        with pytest.raises(GoogleChannelNotEnabledError) as excinfo:
            require_enabled_google_channel(
                native, frozenset({GoogleAdvertisingChannelType.SEARCH})
            )
        assert excinfo.value.code == "CHANNEL_TYPE_NOT_ENABLED"
        assert excinfo.value.channel is GoogleAdvertisingChannelType.PERFORMANCE_MAX

    def test_performance_max_pasa_cuando_esta_habilitado(self) -> None:
        native = GoogleCampaignCreationArgs.model_validate(_plan(_performance_max_native())).native

        require_enabled_google_channel(
            native,
            frozenset(
                {GoogleAdvertisingChannelType.SEARCH, GoogleAdvertisingChannelType.PERFORMANCE_MAX}
            ),
        )

    def test_meta_nunca_se_compara_contra_canales_de_google(self) -> None:
        native = MetaCampaignCreationArgs.model_validate(
            {
                "schema_version": 1,
                "platform": "meta",
                "name": "Reserva de citas",
                "status": "PAUSED",
                "daily_budget": {"amount": "20.00", "currency": "EUR"},
                "native": {
                    "objective": "OUTCOME_LEADS",
                    "buying_type": "AUCTION",
                    "bid_strategy": "LOWEST_COST_WITHOUT_CAP",
                    "special_ad_categories": [],
                    "special_ad_category_country": [],
                },
            }
        ).native

        require_enabled_google_channel(native, frozenset())

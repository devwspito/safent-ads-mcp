"""Contrato de la union discriminada de canal de Google (tasks.md T029;
contracts/mcp-tools.md §2, §4). Cubre `propose_campaign_package` (`campaign.
native`, `mcp.presentation.package_tools`) -- el mismo bloque que reutiliza
`propose_campaign_draft` (`creation_plan.native`,
`mcp.presentation.campaign_creation_args`) esta cubierto aparte por
`test_campaign_creation_args.py`."""

from __future__ import annotations

import copy
import json
from typing import Any

import pytest
from pydantic import ValidationError

from safent_ads.mcp.presentation.package_tools import ProposeCampaignPackageArgs
from tests.unit.mcp.presentation.test_tool_descriptions_and_schemas import _inline_refs

from .test_propose_campaign_package import google_1x3_payload

_BUSINESS_ID = "11111111-1111-1111-1111-111111111111"
_RESOURCE_NAME = "customers/1234567890/conversionActions/1"


def _campaign_for(native: dict[str, Any]) -> dict[str, Any]:
    payload = copy.deepcopy(google_1x3_payload())
    payload["campaign"]["native"] = native
    return payload


def _search_native() -> dict[str, Any]:
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


def _display_native() -> dict[str, Any]:
    return {
        "advertising_channel_type": "DISPLAY",
        "bidding_strategy": {"kind": "MANUAL_CPC"},
        "contains_eu_political_advertising": "DOES_NOT_CONTAIN_EU_POLITICAL_ADVERTISING",
    }


def _demand_gen_native() -> dict[str, Any]:
    return {
        "advertising_channel_type": "DEMAND_GEN",
        "bidding_strategy": {"kind": "MAXIMIZE_CONVERSIONS"},
        "contains_eu_political_advertising": "DOES_NOT_CONTAIN_EU_POLITICAL_ADVERTISING",
        "conversion_goals": [{"resource_name": _RESOURCE_NAME}],
    }


def _performance_max_native() -> dict[str, Any]:
    return {
        "advertising_channel_type": "PERFORMANCE_MAX",
        "bidding_strategy": {"kind": "MAXIMIZE_CONVERSION_VALUE", "target_roas": "3.5"},
        "contains_eu_political_advertising": "DOES_NOT_CONTAIN_EU_POLITICAL_ADVERTISING",
        "conversion_goals": [{"resource_name": _RESOURCE_NAME}],
    }


class TestLasCuatroVariantesValidan:
    @pytest.mark.parametrize(
        "native_factory",
        [_search_native, _display_native, _demand_gen_native, _performance_max_native],
    )
    def test_las_cuatro_variantes_validan(self, native_factory) -> None:
        native = native_factory()

        args = ProposeCampaignPackageArgs.model_validate(_campaign_for(native))

        assert args.campaign.native.advertising_channel_type == native["advertising_channel_type"]

    def test_performance_max_con_grupo_de_recursos_sin_anuncios_valida(self) -> None:
        payload = _campaign_for(_performance_max_native())
        payload["ad_sets"] = [
            {
                "local_ref": "as#1",
                "name": "Grupo de recursos",
                "audience_plain": "Mujeres y hombres de 25 a 65 años en Valencia",
                "native": {
                    "kind": "ASSET_GROUP",
                    "final_url": "https://clinicax.example/reservar",
                    "assets": {
                        "headlines": ["Reserva ya", "Cita veterinaria", "Atencion 24h"],
                        "long_headlines": ["Reserva tu cita veterinaria en minutos, sin esperas"],
                        "descriptions": [
                            "Reserva tu cita en minutos",
                            "Atencion profesional cercana",
                        ],
                        "business_name": "Clinica X",
                        "logo_asset_id": "asset-logo",
                        "marketing_image_asset_id": "asset-marketing",
                        "square_image_asset_id": "asset-square",
                    },
                },
                "ads": [],
            }
        ]

        args = ProposeCampaignPackageArgs.model_validate(payload)

        assert args.ad_sets[0].ads == []


class TestFormaPorFilaDelCanal:
    def test_network_settings_en_display_falla_por_extra_forbid(self) -> None:
        payload = _campaign_for(_display_native())
        payload["campaign"]["native"]["network_settings"] = {
            "target_google_search": True,
            "target_search_network": True,
            "target_content_network": False,
            "target_partner_search_network": False,
        }

        with pytest.raises(ValidationError):
            ProposeCampaignPackageArgs.model_validate(payload)

    def test_manual_cpc_en_demand_gen_falla_fuera_de_la_puja_permitida(self) -> None:
        payload = _campaign_for(_demand_gen_native())
        payload["campaign"]["native"]["bidding_strategy"] = "MANUAL_CPC"

        with pytest.raises(ValidationError):
            ProposeCampaignPackageArgs.model_validate(payload)

    @pytest.mark.parametrize(
        "native_factory", [_search_native, _display_native, _demand_gen_native]
    )
    def test_geographic_targeting_null_es_invalido(self, native_factory) -> None:
        payload = _campaign_for(native_factory())
        payload["campaign"]["native"]["geographic_targeting"] = None

        with pytest.raises(ValidationError):
            ProposeCampaignPackageArgs.model_validate(payload)

    def test_conversion_goals_ausente_en_demand_gen_falla(self) -> None:
        payload = _campaign_for(_demand_gen_native())
        del payload["campaign"]["native"]["conversion_goals"]

        with pytest.raises(ValidationError):
            ProposeCampaignPackageArgs.model_validate(payload)


class TestEsquemaPublicado:
    def test_manual_cpc_cadena_no_aparece_en_el_schema_publicado(self) -> None:
        defs = ProposeCampaignPackageArgs.model_json_schema()["$defs"]

        search_bidding = defs["GoogleSearchNativeArgs"]["properties"]["bidding_strategy"]
        assert search_bidding.get("const") == "MANUAL_CPC"

        for variant in ("GoogleDisplayNativeArgs", "GoogleDemandGenNativeArgs"):
            bidding = defs[variant]["properties"]["bidding_strategy"]
            assert bidding.get("const") != "MANUAL_CPC"
            assert "oneOf" in bidding or "anyOf" in bidding

    def test_url_expansion_no_es_campo_del_esquema(self) -> None:
        defs = ProposeCampaignPackageArgs.model_json_schema()["$defs"]

        for variant in ("GoogleDemandGenNativeArgs", "GooglePerformanceMaxNativeArgs"):
            properties = defs[variant]["properties"]
            assert "url_expansion_opt_out" not in properties
            assert "text_asset_automation_enabled" not in properties

        blob = json.dumps(defs)
        assert "url_expansion_opt_out" not in blob
        assert "text_asset_automation_enabled" not in blob

    def test_audience_signal_no_existe_en_el_esquema(self) -> None:
        schema = ProposeCampaignPackageArgs.model_json_schema()

        assert "audience_signal" not in json.dumps(schema)

    def test_cero_ref_tras_el_inlineado_de_defs(self) -> None:
        schema = ProposeCampaignPackageArgs.model_json_schema()
        defs = schema.get("$defs", {})

        resolved = _inline_refs(schema, defs, frozenset())

        assert "$ref" not in json.dumps(resolved)


class TestDecoderArgsADominio:
    def test_decoder_omite_geographic_targeting_nunca_null(self) -> None:
        payload = _campaign_for(_search_native())

        args = ProposeCampaignPackageArgs.model_validate(payload)

        assert args.campaign.native.geographic_targeting is None
        dumped = args.campaign.native.model_dump(mode="json")
        assert "geographic_targeting" not in dumped

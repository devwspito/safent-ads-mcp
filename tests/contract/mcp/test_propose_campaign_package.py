"""Contrato de `propose_campaign_package` (tasks.md T041;
contracts/mcp-tools.md §2). Dos ejemplos completos validos (Meta 2 conjuntos
x 2 anuncios, Google 1 grupo x 3 RSA) y variantes con campos inventados que
deben fallar por `extra="forbid"`."""

from __future__ import annotations

import copy

import pytest
from pydantic import ValidationError

from safent_ads.mcp.presentation.package_tools import ProposeCampaignPackageArgs

_BUSINESS_ID = "11111111-1111-1111-1111-111111111111"


def _meta_ad(local_ref: str, checksum_seed: str) -> dict:
    opaque_seed = checksum_seed.replace("#", "").replace("/", "-")
    return {
        "local_ref": local_ref,
        "name": f"Anuncio {local_ref}",
        "creative_asset_id": f"asset-{opaque_seed}",
        "landing_url": "https://clinicax.example/reservar",
        "copy": {
            "primary_text": "Reserva tu cita hoy mismo, sin esperas.",
            "headline": "Reserva ya",
            "description": "Citas veterinarias fuera de horario",
        },
        "cta": "LEARN_MORE",
    }


def _meta_ad_set(local_ref: str, ad_refs: list[str]) -> dict:
    return {
        "local_ref": local_ref,
        "name": f"Conjunto {local_ref}",
        "audience_plain": "Mujeres y hombres de 25 a 65 años, a 15 km de Valencia",
        "native": {
            "budget_mode": "CAMPAIGN",
            "billing_event": "IMPRESSIONS",
            "optimization_goal": "LINK_CLICKS",
            "destination_type": "WEBSITE",
            "targeting": {
                "geo_locations": {"countries": ["ES"]},
                "age_min": 25,
                "age_max": 65,
                "targeting_automation": {"advantage_audience": 0},
            },
            "dsa_beneficiary": "Clinica X",
            "dsa_payor": "Clinica X",
        },
        "ads": [_meta_ad(ref, f"{local_ref}-{ref}") for ref in ad_refs],
    }


def meta_2x2_payload() -> dict:
    return {
        "business_id": _BUSINESS_ID,
        "platform": "meta",
        "account_ref": None,
        "offering_id": "offering-1",
        "campaign": {
            "name": "Reserva de citas",
            "objective": "reservas",
            "daily_budget": {"amount": "20.00", "currency": "EUR"},
            "duration_days": 14,
            "success_criterion": "CPL bajo 15 EUR en 7 dias",
            "kill_criterion": "CPL sobre 40 EUR durante 3 dias seguidos",
            "native": {
                "objective": "OUTCOME_LEADS",
                "buying_type": "AUCTION",
                "bid_strategy": "LOWEST_COST_WITHOUT_CAP",
                "special_ad_categories": [],
                "special_ad_category_country": [],
            },
        },
        "ad_sets": [
            _meta_ad_set("as#1", ["as#1/ad#1", "as#1/ad#2"]),
            _meta_ad_set("as#2", ["as#2/ad#1", "as#2/ad#2"]),
        ],
        "rationale": {
            "owner_request": "Hazme una campaña de reserva de citas veterinarias",
            "why": "Tus clientes buscan cita fuera de horario y no hay campaña que los recoja",
        },
        "research": None,
        "package_group_id": None,
    }


def google_1x3_payload() -> dict:
    return {
        "business_id": _BUSINESS_ID,
        "platform": "google",
        "account_ref": None,
        "offering_id": "offering-1",
        "campaign": {
            "name": "Reserva de citas Google",
            "objective": "reservas",
            "daily_budget": {"amount": "20.00", "currency": "EUR"},
            "duration_days": 14,
            "success_criterion": "CPL bajo 15 EUR en 7 dias",
            "kill_criterion": "CPL sobre 40 EUR durante 3 dias seguidos",
            "native": {
                "advertising_channel_type": "SEARCH",
                "bidding_strategy": "MANUAL_CPC",
                "contains_eu_political_advertising": "DOES_NOT_CONTAIN_EU_POLITICAL_ADVERTISING",
                "network_settings": {
                    "target_google_search": True,
                    "target_search_network": True,
                    "target_content_network": False,
                    "target_partner_search_network": False,
                },
            },
        },
        "ad_sets": [
            {
                "local_ref": "as#1",
                "name": "Grupo 1",
                "audience_plain": "Mujeres y hombres de 25 a 65 años, a 15 km de Valencia",
                "native": {
                    "type": "SEARCH_STANDARD",
                    "bidding_strategy": "MANUAL_CPC",
                    "targeting_mode": "INHERIT_CAMPAIGN",
                },
                "keywords": [
                    {"text": "reserva cita veterinario", "match_type": "PHRASE"},
                    {"text": "veterinario urgencias valencia", "match_type": "BROAD"},
                ],
                "cpc_bid": {"amount": "0.80", "currency": "EUR"},
                "ads": [
                    {
                        "local_ref": "as#1/ad#1",
                        "name": "RSA 1",
                        "creative_asset_id": None,
                        "landing_url": "https://clinicax.example/reservar",
                        "copy": {
                            "headlines": ["Reserva ya", "Cita veterinaria", "Atencion 24h"],
                            "descriptions": [
                                "Reserva tu cita en minutos",
                                "Atencion profesional cercana",
                            ],
                        },
                        "cta": None,
                    },
                    {
                        "local_ref": "as#1/ad#2",
                        "name": "RSA 2",
                        "creative_asset_id": None,
                        "landing_url": "https://clinicax.example/reservar",
                        "copy": {
                            "headlines": ["Cita hoy mismo", "Veterinario 24h", "Sin esperas"],
                            "descriptions": [
                                "Pide tu cita en menos de un minuto",
                                "Equipo veterinario disponible siempre",
                            ],
                        },
                        "cta": None,
                    },
                    {
                        "local_ref": "as#1/ad#3",
                        "name": "RSA 3",
                        "creative_asset_id": None,
                        "landing_url": "https://clinicax.example/reservar",
                        "copy": {
                            "headlines": ["Urgencias 24h", "Reserva online", "Cita en minutos"],
                            "descriptions": [
                                "Servicio veterinario fuera de horario",
                                "Reserva desde el movil ahora mismo",
                            ],
                        },
                        "cta": None,
                    },
                ],
            }
        ],
        "rationale": {
            "owner_request": "Hazme una campaña de reserva de citas veterinarias en Google",
            "why": "La gente busca veterinario de urgencia en Google antes que en redes",
        },
        "research": {
            "internal": [{"kind": "crm", "summary": "El 40% de las reservas llegan de noche"}],
            "external": [
                {
                    "kind": "web",
                    "summary": "La competencia no ofrece reserva online 24h",
                    "url": "https://example.com/blog/veterinarios",
                    "observed_at": "2026-09-10",
                }
            ],
        },
        "package_group_id": None,
    }


class TestValidExamples:
    def test_meta_2x2_is_valid(self) -> None:
        args = ProposeCampaignPackageArgs.model_validate(meta_2x2_payload())

        assert len(args.ad_sets) == 2
        assert all(len(ad_set.ads) == 2 for ad_set in args.ad_sets)
        assert args.platform.value == "meta"

    def test_google_1x3_is_valid(self) -> None:
        args = ProposeCampaignPackageArgs.model_validate(google_1x3_payload())

        assert len(args.ad_sets) == 1
        assert len(args.ad_sets[0].ads) == 3
        assert args.platform.value == "google"
        assert args.research is not None
        assert len(args.research.external) == 1


class TestInventedFieldsFail:
    @pytest.mark.parametrize("payload_factory", [meta_2x2_payload, google_1x3_payload])
    def test_invented_top_level_field_fails(self, payload_factory) -> None:
        payload = payload_factory()
        payload["status"] = "PAUSED"

        with pytest.raises(ValidationError):
            ProposeCampaignPackageArgs.model_validate(payload)

    def test_invented_field_on_meta_native_fails(self) -> None:
        payload = meta_2x2_payload()
        payload["campaign"]["native"]["page_id"] = "1234567890"

        with pytest.raises(ValidationError):
            ProposeCampaignPackageArgs.model_validate(payload)

    def test_invented_field_on_ad_fails(self) -> None:
        payload = copy.deepcopy(meta_2x2_payload())
        payload["ad_sets"][0]["ads"][0]["image_hash"] = "deadbeef"

        with pytest.raises(ValidationError):
            ProposeCampaignPackageArgs.model_validate(payload)

    def test_picture_url_is_not_a_schema_field(self) -> None:
        """Revision 2 §R2.1 (BL-6): la imagen nunca es una URL -- el modelo
        solo puede enviar `creative_asset_id`."""
        payload = copy.deepcopy(meta_2x2_payload())
        payload["ad_sets"][0]["ads"][0]["picture"] = "https://cdn.example.com/x.png"

        with pytest.raises(ValidationError):
            ProposeCampaignPackageArgs.model_validate(payload)

    def test_page_id_is_not_a_schema_field(self) -> None:
        payload = copy.deepcopy(meta_2x2_payload())
        payload["page_id"] = "1234567890"

        with pytest.raises(ValidationError):
            ProposeCampaignPackageArgs.model_validate(payload)


def performance_max_1_asset_group_payload() -> dict:
    """Maximo Rendimiento (tasks.md T027): un grupo de recursos SIN
    anuncios -- `ads_per_node = (0, 0)`, el grupo de recursos ES el
    anuncio (data-model.md)."""
    payload = google_1x3_payload()
    payload["campaign"]["native"] = {
        "advertising_channel_type": "PERFORMANCE_MAX",
        "bidding_strategy": {"kind": "MAXIMIZE_CONVERSIONS"},
        "contains_eu_political_advertising": "DOES_NOT_CONTAIN_EU_POLITICAL_ADVERTISING",
        "conversion_goals": [{"resource_name": "customers/1234567890/conversionActions/1"}],
    }
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
                    "long_headlines": ["Reserva tu cita en minutos, sin esperas"],
                    "descriptions": ["Reserva tu cita en minutos", "Atencion cercana"],
                    "business_name": "Clinica X",
                    "logo_asset_id": "asset-logo",
                    "marketing_image_asset_id": "asset-marketing",
                    "square_image_asset_id": "asset-square",
                },
            },
            "ads": [],
        }
    ]
    return payload


class TestPerformanceMaxAssetGroup:
    def test_grupo_de_recursos_sin_anuncios_es_valido(self) -> None:
        args = ProposeCampaignPackageArgs.model_validate(performance_max_1_asset_group_payload())

        assert args.ad_sets[0].ads == []
        assert args.ad_sets[0].native.kind == "ASSET_GROUP"

    def test_grupo_de_recursos_con_un_anuncio_falla(self) -> None:
        payload = performance_max_1_asset_group_payload()
        payload["ad_sets"][0]["ads"] = [
            {
                "local_ref": "as#1/ad#1",
                "name": "RSA 1",
                "creative_asset_id": None,
                "landing_url": "https://clinicax.example/reservar",
                "copy": {
                    "headlines": ["Reserva ya", "Cita veterinaria", "Atencion 24h"],
                    "descriptions": ["Reserva tu cita en minutos", "Atencion cercana"],
                },
                "cta": None,
            }
        ]

        with pytest.raises(ValidationError):
            ProposeCampaignPackageArgs.model_validate(payload)

    def test_menos_de_tres_titulares_en_el_grupo_de_recursos_falla(self) -> None:
        payload = performance_max_1_asset_group_payload()
        payload["ad_sets"][0]["native"]["assets"]["headlines"] = ["Reserva ya"]

        with pytest.raises(ValidationError):
            ProposeCampaignPackageArgs.model_validate(payload)

    def test_primera_descripcion_del_grupo_de_recursos_por_encima_de_60_falla(self) -> None:
        payload = performance_max_1_asset_group_payload()
        payload["ad_sets"][0]["native"]["assets"]["descriptions"] = [
            "x" * 61,
            "Atencion profesional cercana",
        ]

        with pytest.raises(ValidationError):
            ProposeCampaignPackageArgs.model_validate(payload)

    def test_campo_inventado_en_el_grupo_de_recursos_falla(self) -> None:
        payload = performance_max_1_asset_group_payload()
        payload["ad_sets"][0]["native"]["audience_signal"] = {"customer_list": "x"}

        with pytest.raises(ValidationError):
            ProposeCampaignPackageArgs.model_validate(payload)

    def test_grupo_normal_de_google_sin_anuncios_falla(self) -> None:
        """Regla no cambiada por Maximo Rendimiento: Busqueda/Display/Demand
        Gen siguen exigiendo 1..4 anuncios por conjunto."""
        payload = google_1x3_payload()
        payload["ad_sets"][0]["ads"] = []

        with pytest.raises(ValidationError):
            ProposeCampaignPackageArgs.model_validate(payload)

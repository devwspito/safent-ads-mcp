"""Barrera anti-URL del borde ampliada para el grupo de recursos (tasks.md
T028; threat-model.md ME-1/T-4, residual R-2 de 004). `final_url` entra en
`_URL_BEARING_FIELD_NAMES` -- la MISMA forma que `landing_url`, la forma no
es la autoridad, la allow-list de destino de T042 lo es -- y superar
`_MAX_URL_SCAN_DEPTH` lanza en vez de devolver en silencio: el grupo de
recursos anade dos niveles de anidamiento sobre el resto del arbol."""

from __future__ import annotations

import copy

import pytest
from pydantic import ValidationError

from safent_ads.mcp.presentation.package_tools import ProposeCampaignPackageArgs

from .test_google_channel_union import _campaign_for, _performance_max_native

_ASSET_GROUP_AD_SET = {
    "local_ref": "as#1",
    "name": "Grupo de recursos",
    "audience_plain": "Mujeres y hombres de 25 a 65 años en Valencia",
    "native": {
        "kind": "ASSET_GROUP",
        "final_url": "https://clinicax.example/reservar",
        "assets": {
            "headlines": ["Reserva ya", "Cita veterinaria", "Atencion 24h"],
            "long_headlines": ["Reserva tu cita veterinaria en minutos, sin esperas"],
            "descriptions": ["Reserva tu cita en minutos", "Atencion profesional cercana"],
            "business_name": "Clinica X",
            "logo_asset_id": "asset-logo",
            "marketing_image_asset_id": "asset-marketing",
            "square_image_asset_id": "asset-square",
        },
    },
    "ads": [],
}


def _performance_max_payload() -> dict:
    payload = _campaign_for(_performance_max_native())
    payload["ad_sets"] = [copy.deepcopy(_ASSET_GROUP_AD_SET)]
    return payload


def _nested_url(depth: int, url: str) -> object:
    value: object = url
    for _ in range(depth):
        value = {"x": value}
    return value


class TestFinalUrl:
    def test_final_url_se_valida_como_url_no_se_prohibe(self) -> None:
        args = ProposeCampaignPackageArgs.model_validate(_performance_max_payload())

        assert args.ad_sets[0].native.final_url == "https://clinicax.example/reservar"

    def test_final_url_no_https_falla(self) -> None:
        payload = _performance_max_payload()
        payload["ad_sets"][0]["native"]["final_url"] = "http://clinicax.example/reservar"

        with pytest.raises(ValidationError):
            ProposeCampaignPackageArgs.model_validate(payload)

    def test_final_url_con_credenciales_falla(self) -> None:
        payload = _performance_max_payload()
        payload["ad_sets"][0]["native"]["final_url"] = "https://u:p@clinicax.example/reservar"

        with pytest.raises(ValidationError):
            ProposeCampaignPackageArgs.model_validate(payload)


class TestProfundidadDeEscaneo:
    def test_url_a_profundidad_9_se_rechaza(self) -> None:
        payload = _performance_max_payload()
        payload["poison"] = _nested_url(depth=8, url="https://evil.example/")

        with pytest.raises(ValidationError, match="url_scan_depth_exceeded"):
            ProposeCampaignPackageArgs.model_validate(payload)

    def test_url_dentro_de_la_profundidad_se_rechaza_como_url_libre(self) -> None:
        payload = _performance_max_payload()
        payload["poison"] = _nested_url(depth=3, url="https://evil.example/")

        with pytest.raises(ValidationError) as error:
            ProposeCampaignPackageArgs.model_validate(payload)
        assert "url_scan_depth_exceeded" not in str(error.value)

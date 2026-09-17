"""Red de regresion de Busqueda (tasks.md T003, plan T021): la llamada de
`propose_campaign_package` valida para Busqueda con la unica forma que
existe hoy, antes de que los tipos de campana de Google introduzcan la
union discriminada. Congela `google_1x3_payload()` de
`test_propose_campaign_package.py` como constante de este fichero: si una
tarea posterior cambia una clave del cable de Busqueda por error, esta
prueba lo detecta sin depender de que nadie recuerde extenderla."""

from __future__ import annotations

import copy

from safent_ads.mcp.presentation.package_tools import ProposeCampaignPackageArgs

from .test_propose_campaign_package import google_1x3_payload

_TODAY_SEARCH_CALL: dict = google_1x3_payload()


class TestSearchNativeArgsWireShapeUnchanged:
    def test_llamada_de_busqueda_de_hoy_sigue_validando(self) -> None:
        payload = copy.deepcopy(_TODAY_SEARCH_CALL)

        args = ProposeCampaignPackageArgs.model_validate(payload)

        assert args.platform.value == "google"
        assert args.campaign.native.advertising_channel_type == "SEARCH"
        assert args.campaign.native.bidding_strategy == "MANUAL_CPC"
        assert len(args.ad_sets) == 1
        assert len(args.ad_sets[0].ads) == 3

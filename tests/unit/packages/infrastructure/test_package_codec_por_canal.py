"""`package_codec` por canal (tasks.md T025): `_decode_campaign_native`
despacha por `advertising_channel_type` y `_decode_ad_set_native` por
`kind` -- ida y vuelta exacta para las cuatro variantes de
`GoogleCampaignNative` y para el grupo de recursos de Máximo Rendimiento."""

from __future__ import annotations

from safent_ads.packages.domain.planned_tree import (
    EuPoliticalAdvertisingDeclaration,
    GoogleDemandGenCampaignNative,
    GoogleDisplayCampaignNative,
)
from safent_ads.packages.infrastructure.package_codec import decode_plan, encode_plan
from safent_ads.proposals.domain.conversion_goal import ConversionGoal
from safent_ads.proposals.domain.google_bidding import MaximizeClicks, MaximizeConversions

from ..domain.conftest import (
    google_ad_set,
    google_campaign,
    performance_max_ad_set,
    performance_max_campaign_native,
)

_GOAL = ConversionGoal("customers/1112223333/conversionActions/4445556666")


class TestFourVariantsRoundTrip:
    def test_ida_y_vuelta_de_las_cuatro_variantes(self) -> None:
        variants = (
            google_campaign(),
            google_campaign(
                native=GoogleDisplayCampaignNative(
                    bidding_strategy=MaximizeClicks(),
                    contains_eu_political_advertising=(
                        EuPoliticalAdvertisingDeclaration.DOES_NOT_CONTAIN
                    ),
                )
            ),
            google_campaign(
                native=GoogleDemandGenCampaignNative(
                    bidding_strategy=MaximizeConversions(),
                    contains_eu_political_advertising=(
                        EuPoliticalAdvertisingDeclaration.DOES_NOT_CONTAIN
                    ),
                    conversion_goals=(_GOAL,),
                )
            ),
            google_campaign(native=performance_max_campaign_native()),
        )

        for campaign in variants:
            raw = encode_plan(campaign, (google_ad_set(),))
            decoded_campaign, _ = decode_plan(raw)
            assert decoded_campaign == campaign

    def test_paquete_search_almacenado_hoy_se_decodifica_igual(self) -> None:
        campaign = google_campaign()
        ad_set = google_ad_set()

        raw = encode_plan(campaign, (ad_set,))
        decoded_campaign, decoded_ad_sets = decode_plan(raw)

        assert decoded_campaign == campaign
        assert decoded_ad_sets == (ad_set,)

    def test_grupo_de_recursos_ida_y_vuelta_con_preview_key(self) -> None:
        campaign = google_campaign(native=performance_max_campaign_native())
        ad_set = performance_max_ad_set()

        raw = encode_plan(campaign, (ad_set,))
        decoded_campaign, decoded_ad_sets = decode_plan(raw)

        assert decoded_campaign == campaign
        assert decoded_ad_sets == (ad_set,)
        assert decoded_ad_sets[0].native.assets.logo.preview_key == "preview-key-1"  # type: ignore[union-attr]

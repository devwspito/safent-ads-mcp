"""Union de cuatro variantes de `GoogleCampaignNative` + `GoogleAssetGroupNative`
(data-model.md; tasks.md T020-T023). Búsqueda no se mueve (T003/T021, red de
regresión aparte); estas pruebas cubren SOLO lo nuevo: Máximo Rendimiento
como grupo de recursos de segundo nivel, sus literales forzados (BL-1) y las
formas de cable que `platform_completeness.py` deriva de la fila del canal."""

from __future__ import annotations

import pytest

from safent_ads.packages.domain.errors import PlannedTreeError
from safent_ads.packages.domain.planned_tree import (
    AdSetRef,
    EuPoliticalAdvertisingDeclaration,
    GoogleAssetGroupNative,
    GoogleCampaignNative,
    GoogleDemandGenCampaignNative,
    GoogleDisplayCampaignNative,
    GoogleNetworkSettings,
    GoogleSearchCampaignNative,
    PlannedAdSet,
    PlannedCampaign,
)
from safent_ads.packages.domain.platform_completeness import ad_set_wire_plan, campaign_wire_plan
from safent_ads.packages.domain.values import AssetGroupAssetPlan
from safent_ads.proposals.domain.conversion_goal import ConversionGoal
from safent_ads.proposals.domain.google_bidding import ManualCpc, MaximizeConversions
from safent_ads.proposals.domain.money import Money
from safent_ads.shared.ids import PlatformCode

from .conftest import (
    audience,
    google_ad,
    google_campaign,
    image_creative,
    meta_campaign,
    performance_max_ad_set,
    performance_max_campaign_native,
)

_GOAL = ConversionGoal("customers/1112223333/conversionActions/4445556666")


class TestPerformanceMaxCampaignNative:
    def test_pmax_sin_network_settings_es_valida(self) -> None:
        native = performance_max_campaign_native()

        assert native.advertising_channel_type.value == "PERFORMANCE_MAX"

    def test_pmax_con_network_settings_falla(self) -> None:
        with pytest.raises(TypeError):
            performance_max_campaign_native(
                network_settings=GoogleNetworkSettings(True, True, False, False)
            )

    def test_pmax_sin_url_expansion_opt_out_falla(self) -> None:
        """BL-1 capa 1: el literal forzado no es un campo -- enviarlo
        falla en la propia frontera del constructor, nunca se puede
        apagar desde fuera."""
        with pytest.raises(TypeError):
            performance_max_campaign_native(url_expansion_opt_out=False)

    def test_pmax_con_automatizacion_de_texto_encendida_falla(self) -> None:
        with pytest.raises(TypeError):
            performance_max_campaign_native(text_asset_automation_enabled=True)

    def test_pmax_proyecta_los_literales_forzados_en_el_canonico(self) -> None:
        native = performance_max_campaign_native()

        canonical = native.to_canonical()

        assert canonical["url_expansion_opt_out"] is True
        assert canonical["text_asset_automation_enabled"] is False

    def test_pmax_sin_conversion_goals_falla(self) -> None:
        with pytest.raises(PlannedTreeError, match="google_campaign_conversion_goals_required"):
            performance_max_campaign_native(conversion_goals=())

    def test_pmax_con_manual_cpc_falla(self) -> None:
        with pytest.raises(PlannedTreeError, match="google_campaign_bidding_not_allowed"):
            performance_max_campaign_native(bidding_strategy=ManualCpc())


class TestChannelMinimums:
    """T026 (contracts/mcp-tools.md §5): `PlannedCampaign.__post_init__`
    exige el minimo de presupuesto/duracion del canal -- movido aqui desde
    `packages.application.propose_campaign_package._require_channel_
    minimums` (revision 0.2.24): el mismo invariante que ya vive en esta
    clase (presupuesto positivo, duracion en rango), no una regla de caso
    de uso aparte. `mcp.presentation.package_tools._translate_channel_
    minimum_error` traduce estos dos codigos al tipo que el limite MCP
    espera; ese cableado se prueba en `tests/contract/mcp/
    test_package_tools_typed_errors.py`."""

    def test_presupuesto_por_debajo_del_minimo_del_canal_falla(self) -> None:
        with pytest.raises(
            PlannedTreeError, match="planned_campaign_daily_budget_below_channel_minimum"
        ):
            google_campaign(
                native=performance_max_campaign_native(), daily_budget=Money.of("15.00")
            )

    def test_duracion_por_debajo_del_minimo_del_canal_falla(self) -> None:
        with pytest.raises(
            PlannedTreeError, match="planned_campaign_duration_below_channel_minimum"
        ):
            google_campaign(
                native=performance_max_campaign_native(),
                daily_budget=Money.of("20.00"),
                duration_days=7,
            )

    def test_presupuesto_por_debajo_del_target_cpa_falla(self) -> None:
        with pytest.raises(
            PlannedTreeError, match="planned_campaign_daily_budget_below_channel_minimum"
        ):
            google_campaign(
                native=performance_max_campaign_native(
                    bidding_strategy=MaximizeConversions(target_cpa=Money.of("25.00"))
                ),
                daily_budget=Money.of("20.00"),
            )

    def test_presupuesto_y_duracion_suficientes_no_fallan_por_minimos(self) -> None:
        campaign = google_campaign(
            native=performance_max_campaign_native(), daily_budget=Money.of("20.00")
        )

        assert campaign.daily_budget == Money.of("20.00")

    def test_meta_no_tiene_fila_que_consultar(self) -> None:
        # 1,00 EUR/dia esta por debajo del minimo de CUALQUIER fila de
        # Google (SEARCH, la mas barata, exige 5,00) -- si Meta consultase
        # `CHANNEL_SPECS` por error, esto fallaria.
        campaign = meta_campaign(daily_budget=Money.of("1.00"), duration_days=7)

        assert campaign.daily_budget == Money.of("1.00")


class TestGoogleCampaignNativeUnionAlias:
    def test_isinstance_contra_el_alias_de_union_sigue_funcionando(self) -> None:
        variants: tuple[GoogleCampaignNative, ...] = (
            GoogleSearchCampaignNative(
                bidding_strategy=ManualCpc(),
                contains_eu_political_advertising=(
                    EuPoliticalAdvertisingDeclaration.DOES_NOT_CONTAIN
                ),
                network_settings=GoogleNetworkSettings(True, True, False, False),
            ),
            GoogleDisplayCampaignNative(
                bidding_strategy=ManualCpc(),
                contains_eu_political_advertising=(
                    EuPoliticalAdvertisingDeclaration.DOES_NOT_CONTAIN
                ),
            ),
            GoogleDemandGenCampaignNative(
                bidding_strategy=MaximizeConversions(),
                contains_eu_political_advertising=(
                    EuPoliticalAdvertisingDeclaration.DOES_NOT_CONTAIN
                ),
                conversion_goals=(_GOAL,),
            ),
            performance_max_campaign_native(),
        )

        for native in variants:
            assert isinstance(native, GoogleCampaignNative)
            campaign = google_campaign(native=native)
            assert campaign.platform is PlatformCode.GOOGLE


class TestAssetGroupNative:
    def test_audience_signal_no_es_una_clave_del_nativo(self) -> None:
        base = performance_max_ad_set().native

        with pytest.raises(TypeError):
            GoogleAssetGroupNative(
                final_url=base.final_url, assets=base.assets, audience_signal=None
            )  # type: ignore[call-arg]

        assert "audience_signal" not in base.to_canonical()

    def test_grupo_de_recursos_exige_lista_de_anuncios_vacia(self) -> None:
        with pytest.raises(PlannedTreeError, match="planned_ad_set_ads_count_invalid"):
            performance_max_ad_set(ads=(google_ad(local_ref="as#1/ad#1"),))

    def test_ad_set_con_grupo_de_recursos_y_anuncios_falla(self) -> None:
        with pytest.raises(PlannedTreeError, match="planned_ad_set_ads_count_invalid"):
            PlannedAdSet(
                local_ref=AdSetRef("as#1"),
                name="Grupo de recursos 1",
                audience=audience(),
                native=performance_max_ad_set().native,
                ads=(google_ad(local_ref="as#1/ad#1"), google_ad(local_ref="as#1/ad#2")),
            )

    def test_relacion_de_aspecto_fuera_del_2_por_ciento_falla(self) -> None:
        with pytest.raises(
            PlannedTreeError, match="asset_group_marketing_image_aspect_ratio_invalid"
        ):
            performance_max_ad_set(
                native=GoogleAssetGroupNative(
                    final_url=performance_max_ad_set().native.final_url,
                    assets=AssetGroupAssetPlan(
                        headlines=("Reserva ya", "Cita veterinaria", "Atencion 24h"),
                        long_headlines=("Reserva tu cita veterinaria en minutos",),
                        descriptions=("Reserva ya", "Atencion profesional cercana"),
                        business_name="Clinica X",
                        logo=image_creative(checksum="a1" * 32, width=1080, height=1080),
                        marketing_image=image_creative(checksum="a2" * 32, width=1200, height=1200),
                        square_image=image_creative(checksum="a3" * 32, width=1080, height=1080),
                    ),
                )
            )


class TestAssetGroupWireShape:
    """T023: la forma de cable sale de la fila, no se reimplementa aqui."""

    def test_forma_de_cable_de_pmax_no_lleva_redes(self) -> None:
        campaign = PlannedCampaign(
            name="Reservas Maximo Rendimiento",
            objective=google_campaign().objective,
            daily_budget=google_campaign().daily_budget,
            duration_days=14,
            native=performance_max_campaign_native(),
            success_criterion="CPL bajo 15 EUR en 7 dias",
            kill_criterion="CPL sobre 40 EUR durante 3 dias seguidos",
        )

        wire = campaign_wire_plan(campaign)

        assert "network_settings" not in wire["native"]

    def test_la_forma_de_cable_de_pmax_lleva_los_literales_forzados(self) -> None:
        wire = ad_set_wire_plan(performance_max_ad_set(), PlatformCode.GOOGLE)

        assert wire["native"]["kind"] == "ASSET_GROUP"

        campaign_wire = campaign_wire_plan(
            PlannedCampaign(
                name="Reservas Maximo Rendimiento",
                objective=google_campaign().objective,
                daily_budget=google_campaign().daily_budget,
                duration_days=14,
                native=performance_max_campaign_native(),
                success_criterion="CPL bajo 15 EUR en 7 dias",
                kill_criterion="CPL sobre 40 EUR durante 3 dias seguidos",
            )
        )
        assert campaign_wire["native"]["url_expansion_opt_out"] is True
        assert campaign_wire["native"]["text_asset_automation_enabled"] is False

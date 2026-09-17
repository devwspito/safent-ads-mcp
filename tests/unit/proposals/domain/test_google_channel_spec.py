"""`GoogleChannelSpec` (data-model.md
§`GoogleChannelSpec`, tasks.md T010). The four rows are the only place a
channel or bidding literal may legally exist (INV-15); `spec_for` is
fail-closed (INV-16)."""

from __future__ import annotations

from dataclasses import FrozenInstanceError
from decimal import Decimal

import pytest

from safent_ads.proposals.domain.google_channel_spec import (
    CHANNEL_SPECS,
    ChannelSpecError,
    FieldRule,
    GoogleAdvertisingChannelType,
    GoogleBiddingStrategy,
    GoogleChannelSpec,
    GoogleChildNodeKind,
    spec_for,
)
from safent_ads.proposals.domain.money import Money


def _assert_search_row(search: GoogleChannelSpec) -> None:
    assert search.allowed_bidding == {
        GoogleBiddingStrategy.MANUAL_CPC,
        GoogleBiddingStrategy.MAXIMIZE_CLICKS,
        GoogleBiddingStrategy.MAXIMIZE_CONVERSIONS,
    }
    assert search.requires_conversion_goals is False
    assert search.network_settings is FieldRule.REQUIRED
    assert search.geographic_targeting is FieldRule.OPTIONAL
    assert search.child_node is GoogleChildNodeKind.AD_GROUP
    assert search.allowed_child_types == {"SEARCH_STANDARD"}
    assert search.allowed_ad_types == {"RESPONSIVE_SEARCH_AD"}
    assert search.keywords is FieldRule.REQUIRED
    assert search.cpc_bid is FieldRule.REQUIRED
    assert search.ads_per_node == (1, 4)
    assert search.min_daily_budget == Money.of("5.00")
    assert search.min_duration_days == 7
    assert dict(search.forced_literals) == {}
    assert search.api_version == "v25"


def _assert_display_row(display: GoogleChannelSpec) -> None:
    assert display.allowed_bidding == {
        GoogleBiddingStrategy.MANUAL_CPC,
        GoogleBiddingStrategy.MAXIMIZE_CLICKS,
        GoogleBiddingStrategy.MAXIMIZE_CONVERSIONS,
    }
    assert display.requires_conversion_goals is False
    assert display.network_settings is FieldRule.FORBIDDEN
    assert display.child_node is GoogleChildNodeKind.AD_GROUP
    assert display.allowed_child_types == {"DISPLAY_STANDARD"}
    assert display.allowed_ad_types == {"RESPONSIVE_DISPLAY_AD"}
    assert display.keywords is FieldRule.FORBIDDEN
    assert display.cpc_bid is FieldRule.OPTIONAL
    assert display.ads_per_node == (1, 4)
    assert display.min_daily_budget == Money.of("5.00")
    assert display.min_duration_days == 7
    assert dict(display.forced_literals) == {}


def _assert_demand_gen_row(demand_gen: GoogleChannelSpec) -> None:
    assert demand_gen.allowed_bidding == {
        GoogleBiddingStrategy.MAXIMIZE_CONVERSIONS,
        GoogleBiddingStrategy.MAXIMIZE_CONVERSION_VALUE,
    }
    assert demand_gen.requires_conversion_goals is True
    assert demand_gen.network_settings is FieldRule.FORBIDDEN
    assert demand_gen.child_node is GoogleChildNodeKind.AD_GROUP
    assert demand_gen.allowed_child_types == {"DEMAND_GEN_STANDARD"}
    assert demand_gen.allowed_ad_types == {"DEMAND_GEN_MULTI_ASSET_AD"}
    assert demand_gen.keywords is FieldRule.FORBIDDEN
    assert demand_gen.cpc_bid is FieldRule.FORBIDDEN
    assert demand_gen.ads_per_node == (1, 4)
    assert demand_gen.min_daily_budget == Money.of("15.00")
    assert demand_gen.min_duration_days == 14
    assert dict(demand_gen.forced_literals) == {
        "url_expansion_opt_out": True,
        "text_asset_automation_enabled": False,
    }


def _assert_performance_max_row(performance_max: GoogleChannelSpec) -> None:
    assert performance_max.allowed_bidding == {
        GoogleBiddingStrategy.MAXIMIZE_CONVERSIONS,
        GoogleBiddingStrategy.MAXIMIZE_CONVERSION_VALUE,
    }
    assert performance_max.requires_conversion_goals is True
    assert performance_max.network_settings is FieldRule.FORBIDDEN
    assert performance_max.child_node is GoogleChildNodeKind.ASSET_GROUP
    assert performance_max.allowed_child_types == frozenset()
    assert performance_max.allowed_ad_types == frozenset()
    assert performance_max.keywords is FieldRule.FORBIDDEN
    assert performance_max.cpc_bid is FieldRule.FORBIDDEN
    assert performance_max.ads_per_node == (0, 0)
    assert performance_max.min_daily_budget == Money.of("20.00")
    assert performance_max.min_duration_days == 14
    assert dict(performance_max.forced_literals) == {
        "url_expansion_opt_out": True,
        "text_asset_automation_enabled": False,
    }


def test_las_cuatro_filas_coinciden_con_data_model() -> None:
    assert set(CHANNEL_SPECS) == set(GoogleAdvertisingChannelType)
    _assert_search_row(CHANNEL_SPECS[GoogleAdvertisingChannelType.SEARCH])
    _assert_display_row(CHANNEL_SPECS[GoogleAdvertisingChannelType.DISPLAY])
    _assert_demand_gen_row(CHANNEL_SPECS[GoogleAdvertisingChannelType.DEMAND_GEN])
    _assert_performance_max_row(CHANNEL_SPECS[GoogleAdvertisingChannelType.PERFORMANCE_MAX])
    for spec in CHANNEL_SPECS.values():
        assert spec.api_version == "v25"
        assert spec.min_daily_budget.currency == "EUR"
        assert isinstance(spec.min_daily_budget.amount, Decimal)


def test_canal_sin_fila_falla_antes_de_leer_nada() -> None:
    with pytest.raises(ChannelSpecError) as excinfo:
        spec_for("TIKTOK")

    assert excinfo.value.attempted_channel == "TIKTOK"
    assert excinfo.value.supported_channels == frozenset(GoogleAdvertisingChannelType)


def test_canal_valido_devuelve_la_fila() -> None:
    assert spec_for("SEARCH") is CHANNEL_SPECS[GoogleAdvertisingChannelType.SEARCH]


def test_la_tabla_no_es_mutable() -> None:
    with pytest.raises(TypeError):
        CHANNEL_SPECS[GoogleAdvertisingChannelType.SEARCH] = CHANNEL_SPECS[  # type: ignore[index]
            GoogleAdvertisingChannelType.DISPLAY
        ]

    search = CHANNEL_SPECS[GoogleAdvertisingChannelType.SEARCH]
    with pytest.raises(TypeError):
        search.forced_literals["url_expansion_opt_out"] = True  # type: ignore[index]

    with pytest.raises(FrozenInstanceError):
        search.min_duration_days = 999  # type: ignore[misc]

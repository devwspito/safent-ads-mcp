"""`ApiSettings.google_channels_enabled` (tasks.md T076, POLISH "canales
por configuracion"): el gate de seguridad T035 solo permite publicar 0.2.24
con los tres canales nuevos de Google (DISPLAY/DEMAND_GEN/
PERFORMANCE_MAX) APAGADOS. CSV o JSON, mismo patron que `ADS_MCP_EXTRA_
ALLOWED_HOSTS`/`CLOUDFLARE_ALLOWED_ZONES`
(`tests/unit/composition/test_env_list_fields_accept_csv_and_json.py`)."""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from safent_ads.proposals.domain.google_channel_spec import GoogleAdvertisingChannelType
from tests.unit.composition.factories import build_api_settings


def test_absent_env_var_defaults_to_search_only(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("ADS_GOOGLE_CHANNELS_ENABLED", raising=False)

    settings = build_api_settings()

    assert settings.google_channels_enabled == frozenset({GoogleAdvertisingChannelType.SEARCH})


def test_comma_separated_env_var_is_accepted(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("ADS_GOOGLE_CHANNELS_ENABLED", "SEARCH,PERFORMANCE_MAX")

    settings = build_api_settings()

    assert settings.google_channels_enabled == frozenset(
        {GoogleAdvertisingChannelType.SEARCH, GoogleAdvertisingChannelType.PERFORMANCE_MAX}
    )


def test_json_list_env_var_is_accepted(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("ADS_GOOGLE_CHANNELS_ENABLED", '["SEARCH", "DISPLAY"]')

    settings = build_api_settings()

    assert settings.google_channels_enabled == frozenset(
        {GoogleAdvertisingChannelType.SEARCH, GoogleAdvertisingChannelType.DISPLAY}
    )


def test_single_channel_without_commas_is_wrapped_in_a_one_item_set(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("ADS_GOOGLE_CHANNELS_ENABLED", "DEMAND_GEN")

    settings = build_api_settings()

    assert settings.google_channels_enabled == frozenset({GoogleAdvertisingChannelType.DEMAND_GEN})


def test_invalid_value_fails_startup(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("ADS_GOOGLE_CHANNELS_ENABLED", "SEARCH,NOT_A_CHANNEL")

    with pytest.raises(ValidationError, match="ADS_GOOGLE_CHANNELS_ENABLED"):
        build_api_settings()

"""Deployment environment names must reach the optional catalogue connector."""

import pytest

from tests.unit.composition.factories import build_api_settings


def test_store_api_reads_operator_environment(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("ADS_STORE_API_BASE_URL", "https://catalog.example/api")
    monkeypatch.setenv("ADS_STORE_API_EGRESS_IP", "203.0.113.10")
    settings = build_api_settings()
    assert settings.store_api_base_url == "https://catalog.example/api"
    assert settings.store_api_egress_ip == "203.0.113.10"


def test_store_api_remains_optional_without_environment(monkeypatch: pytest.MonkeyPatch) -> None:
    for name in (
        "ADS_STORE_API_BASE_URL",
        "ADS_STORE_API_EGRESS_IP",
        "store_api_base_url",
        "store_api_egress_ip",
    ):
        monkeypatch.delenv(name, raising=False)
    settings = build_api_settings()
    assert settings.store_api_base_url == ""
    assert settings.store_api_egress_ip == ""

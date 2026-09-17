"""`ApiSettings.public_base_url` (tasks.md 002 T002, plan.md "Ajustes"):
normaliza la barra final y rechaza path/query/fragment -- RFC 8414 compara
el emisor como cadena exacta, asi que una barra de mas rompe el
descubrimiento del AS en silencio."""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from tests.unit.composition.factories import build_api_settings


def test_accepts_https_without_trailing_slash() -> None:
    settings = build_api_settings(public_base_url="https://ads.example.com")

    assert settings.public_base_url == "https://ads.example.com"


def test_strips_a_single_trailing_slash() -> None:
    settings = build_api_settings(public_base_url="https://ads.example.com/")

    assert settings.public_base_url == "https://ads.example.com"


def test_accepts_http_localhost_for_dev() -> None:
    settings = build_api_settings(public_base_url="http://localhost:8410")

    assert settings.public_base_url == "http://localhost:8410"


def test_rejects_http_non_localhost() -> None:
    with pytest.raises(ValidationError, match="https o http://localhost"):
        build_api_settings(public_base_url="http://ads.example.com")


def test_rejects_a_path() -> None:
    with pytest.raises(ValidationError, match="path/query/fragment"):
        build_api_settings(public_base_url="https://ads.example.com/mcp")


def test_rejects_a_path_hidden_behind_a_trailing_slash() -> None:
    with pytest.raises(ValidationError, match="path/query/fragment"):
        build_api_settings(public_base_url="https://ads.example.com/mcp/")


def test_rejects_a_query_string() -> None:
    with pytest.raises(ValidationError, match="path/query/fragment"):
        build_api_settings(public_base_url="https://ads.example.com?debug=1")


def test_rejects_a_fragment() -> None:
    with pytest.raises(ValidationError, match="path/query/fragment"):
        build_api_settings(public_base_url="https://ads.example.com#section")

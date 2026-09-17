"""`validate_import_source_url`: allow-list de host exacto, solo `https`,
sin literales IP (threat-model.md C-11/C-12)."""

from __future__ import annotations

import pytest

from safent_ads.creative.domain.asset_import import (
    SourceUrlNotAllowedError,
    validate_import_source_url,
)

_ALLOWED = frozenset({"v3.fal.media", "videos.openai.com"})


def test_accepts_https_url_on_allowed_host() -> None:
    host = validate_import_source_url("https://v3.fal.media/files/out.png", _ALLOWED)

    assert host == "v3.fal.media"


def test_matches_host_case_insensitively() -> None:
    host = validate_import_source_url("https://V3.FAL.MEDIA/files/out.png", _ALLOWED)

    assert host == "v3.fal.media"


def test_rejects_http_scheme() -> None:
    with pytest.raises(SourceUrlNotAllowedError, match="https"):
        validate_import_source_url("http://v3.fal.media/files/out.png", _ALLOWED)


def test_rejects_host_outside_allow_list() -> None:
    with pytest.raises(SourceUrlNotAllowedError, match="lista blanca"):
        validate_import_source_url("https://evil.example/out.png", _ALLOWED)


def test_rejects_ipv4_literal_even_if_it_would_match_no_allow_list() -> None:
    with pytest.raises(SourceUrlNotAllowedError, match="literal IP"):
        validate_import_source_url("https://169.254.169.254/latest/meta-data", _ALLOWED)


def test_rejects_ipv6_literal() -> None:
    with pytest.raises(SourceUrlNotAllowedError, match="literal IP"):
        validate_import_source_url("https://[::1]/x", _ALLOWED)


def test_rejects_url_without_host() -> None:
    with pytest.raises(SourceUrlNotAllowedError, match="sin host"):
        validate_import_source_url("https:///no-host", _ALLOWED)


def test_rejects_data_uri() -> None:
    with pytest.raises(SourceUrlNotAllowedError, match="https"):
        validate_import_source_url("data:image/png;base64,AAAA", _ALLOWED)


def test_empty_allow_list_rejects_everything() -> None:
    with pytest.raises(SourceUrlNotAllowedError):
        validate_import_source_url("https://v3.fal.media/files/out.png", frozenset())

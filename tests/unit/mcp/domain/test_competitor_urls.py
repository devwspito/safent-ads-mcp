"""`build_competitor_links` (R7): constructor puro de dos URL canonicas, sin
ninguna llamada de red. Valida el dominio (IDNA, minusculas, sin IP
literal, sin credenciales/esquema/ruta, forma de host valida)."""

from __future__ import annotations

import pytest

from safent_ads.mcp.domain.competitor_urls import (
    InvalidCountryCodeError,
    InvalidDomainError,
    MissingSearchTermError,
    build_competitor_links,
)


def test_builds_both_urls_for_a_plain_domain() -> None:
    links = build_competitor_links(domain="Example.COM", name=None, country="ES")

    assert "country=ES" in links.meta_ad_library_url
    assert "example.com" in links.meta_ad_library_url
    assert "region=ES" in links.google_transparency_center_url
    assert "domain=example.com" in links.google_transparency_center_url


def test_builds_both_urls_for_a_name() -> None:
    links = build_competitor_links(domain=None, name="Acme Center", country="ES")

    assert "q=Acme" in links.meta_ad_library_url
    assert "query=Acme" in links.google_transparency_center_url


def test_idna_encodes_non_ascii_domains() -> None:
    links = build_competitor_links(domain="café.com", name=None, country="ES")

    assert "xn--caf-dma.com" in links.meta_ad_library_url


@pytest.mark.parametrize("domain", ["127.0.0.1", "::1", "2001:db8::1"])
def test_rejects_literal_ip_as_domain(domain: str) -> None:
    with pytest.raises(InvalidDomainError):
        build_competitor_links(domain=domain, name=None, country="ES")


@pytest.mark.parametrize(
    "domain",
    [
        "user:pass@example.com",
        "https://example.com",
        "example.com/path",
        "not_a_domain",
    ],
)
def test_rejects_malformed_domains(domain: str) -> None:
    with pytest.raises(InvalidDomainError):
        build_competitor_links(domain=domain, name=None, country="ES")


def test_missing_both_domain_and_name_is_rejected() -> None:
    with pytest.raises(MissingSearchTermError):
        build_competitor_links(domain=None, name=None, country="ES")


def test_blank_name_is_rejected() -> None:
    with pytest.raises(MissingSearchTermError):
        build_competitor_links(domain=None, name="   ", country="ES")


@pytest.mark.parametrize("country", ["es", "ESP", "E1", ""])
def test_rejects_non_iso_country_codes(country: str) -> None:
    with pytest.raises(InvalidCountryCodeError):
        build_competitor_links(domain="example.com", name=None, country=country)


def test_domain_wins_when_both_are_given() -> None:
    links = build_competitor_links(domain="example.com", name="Ignorado", country="ES")

    assert "example.com" in links.meta_ad_library_url
    assert "Ignorado" not in links.meta_ad_library_url

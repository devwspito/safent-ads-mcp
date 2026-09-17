"""`ip_guard`: lista blanca de direcciones IP (F-4) mas validacion
sintactica de URL de egreso (F-11) -- puro, sin resolver DNS ni tocar
red. Cada caso bloqueado de aqui esta *medido* como aceptado por la
lista negra que este modulo reemplaza
(checklists/website-brand-extractor-review.md, F-4)."""

from __future__ import annotations

import ipaddress

import pytest

from safent_ads.shared.net.ip_guard import (
    DEFAULT_ALLOWED_PORTS,
    DEFAULT_ALLOWED_SCHEMES,
    UnsafeEgressUrlError,
    is_blocked_ip,
    validate_egress_url,
)


def _ip(value: str) -> ipaddress.IPv4Address | ipaddress.IPv6Address:
    return ipaddress.ip_address(value)


@pytest.mark.parametrize(
    "address",
    [
        "127.0.0.1",  # loopback
        "0.0.0.0",  # noqa: S104 - unspecified, Linux la conecta a localhost
        "10.0.0.5",  # RFC1918
        "172.16.0.5",  # RFC1918
        "192.168.1.1",  # RFC1918
        "100.64.0.1",  # CGNAT, RFC 6598
        "169.254.169.254",  # link-local, metadata cloud
        "198.18.0.1",  # benchmark, RFC 2544
        "192.0.0.1",  # IETF protocol assignments
        "224.0.0.1",  # multicast
        "240.0.0.1",  # reservado clase E
        "::1",  # loopback IPv6
        "::",  # unspecified IPv6
        "fe80::1",  # link-local IPv6
        "fc00::1",  # ULA IPv6
        "::ffff:127.0.0.1",  # IPv4-mapped de loopback
        "::ffff:169.254.169.254",  # IPv4-mapped de metadata cloud
        "64:ff9b::7f00:1",  # NAT64 (RFC 6052) que embebe 127.0.0.1
        "2002:7f00:1::",  # 6to4 que embebe 127.0.0.1
        "2001:0:4136:e37e::80ff:fffe",  # Teredo cuyo cliente embebido es 127.0.0.1
    ],
)
def test_blocks_every_bypass_form_from_the_security_review(address: str) -> None:
    assert is_blocked_ip(_ip(address)) is True


@pytest.mark.parametrize(
    "address",
    [
        "93.184.216.34",
        "8.8.8.8",
        "157.240.2.35",
        "2606:4700:4700::1111",  # Cloudflare DNS, IPv6 global de verdad
        "::ffff:93.184.216.34",  # IPv4-mapped de una IP global de verdad
        "2002:5db8:d834::",  # 6to4 que embebe una IP global (93.184.216.52)
    ],
)
def test_allows_genuinely_global_addresses(address: str) -> None:
    assert is_blocked_ip(_ip(address)) is False


def test_default_allowed_schemes_and_ports() -> None:
    assert DEFAULT_ALLOWED_SCHEMES == {"http", "https"}
    assert DEFAULT_ALLOWED_PORTS == {80, 443}


class TestValidateEgressUrl:
    def test_accepts_https_with_implicit_port(self) -> None:
        assert validate_egress_url("https://Example.Test/path") == "example.test"

    def test_accepts_http_with_implicit_port(self) -> None:
        assert validate_egress_url("http://example.test") == "example.test"

    def test_accepts_explicit_allowed_port(self) -> None:
        assert validate_egress_url("https://example.test:443/") == "example.test"

    def test_rejects_scheme_outside_allow_list(self) -> None:
        with pytest.raises(UnsafeEgressUrlError, match="esquema"):
            validate_egress_url("ftp://example.test")

    def test_rejects_embedded_credentials(self) -> None:
        with pytest.raises(UnsafeEgressUrlError, match="credenciales"):
            validate_egress_url("https://user:pass@example.test")

    def test_rejects_url_without_host(self) -> None:
        with pytest.raises(UnsafeEgressUrlError, match="sin host"):
            validate_egress_url("https:///path")

    def test_rejects_ip_literal_host(self) -> None:
        with pytest.raises(UnsafeEgressUrlError, match="literal IP"):
            validate_egress_url("https://203.0.113.10/")

    @pytest.mark.parametrize("port", [22, 6379, 9200, 8080, 3306])
    def test_rejects_ports_outside_the_allow_list(self, port: int) -> None:
        with pytest.raises(UnsafeEgressUrlError, match="puerto"):
            validate_egress_url(f"https://example.test:{port}/")

    def test_rejects_malformed_idna_host(self) -> None:
        oversized_label = "a" * 300
        with pytest.raises(UnsafeEgressUrlError, match="IDNA"):
            validate_egress_url(f"https://{oversized_label}.test/")

    def test_custom_allowed_ports_widen_the_allow_list(self) -> None:
        assert (
            validate_egress_url("https://example.test:8443/", allowed_ports=frozenset({8443}))
            == "example.test"
        )

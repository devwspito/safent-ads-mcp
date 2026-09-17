"""`resolve_client_ip` (code review 17-sep, threat-model.md C-42/C-51/C-76/
C-79): unica resolucion del IP real del llamador -- honra los saltos de
confianza y NUNCA devuelve un marcador de texto que pueda colarse en una
columna `INET`."""

from __future__ import annotations

from safent_ads.shared.net.client_ip import resolve_client_ip


def test_zero_hops_ignores_x_forwarded_for_and_uses_the_peer_ip() -> None:
    resolved = resolve_client_ip(
        forwarded_for="10.0.0.1", peer_ip="203.0.113.9", trusted_proxy_hops=0
    )

    assert resolved == "203.0.113.9"


def test_one_hop_trusts_only_the_rightmost_forwarded_for_value() -> None:
    resolved = resolve_client_ip(
        forwarded_for="attacker-forged, 198.51.100.7",
        peer_ip="127.0.0.1",
        trusted_proxy_hops=1,
    )

    assert resolved == "198.51.100.7"


def test_two_hops_trusts_the_second_value_from_the_right() -> None:
    resolved = resolve_client_ip(
        forwarded_for="198.51.100.1, 198.51.100.2", peer_ip="127.0.0.1", trusted_proxy_hops=2
    )

    assert resolved == "198.51.100.1"


def test_header_with_fewer_values_than_hops_falls_back_to_the_peer_ip() -> None:
    resolved = resolve_client_ip(
        forwarded_for="198.51.100.7", peer_ip="203.0.113.9", trusted_proxy_hops=2
    )

    assert resolved == "203.0.113.9"


def test_missing_header_falls_back_to_the_peer_ip() -> None:
    resolved = resolve_client_ip(forwarded_for=None, peer_ip="203.0.113.9", trusted_proxy_hops=1)

    assert resolved == "203.0.113.9"


def test_a_forwarded_for_value_that_is_not_an_ip_falls_back_to_the_peer_ip() -> None:
    """Cabecera falseada con basura en vez de una IP: nunca se persiste tal
    cual -- cae a la IP del socket, que si es de confianza."""
    resolved = resolve_client_ip(
        forwarded_for="not-an-ip-address", peer_ip="203.0.113.9", trusted_proxy_hops=1
    )

    assert resolved == "203.0.113.9"


def test_no_valid_ip_anywhere_returns_none_never_a_placeholder_string() -> None:
    """`None`, nunca `"unknown"`: la unica columna que guarda esto es
    `INET` y nullable a proposito para este caso -- un texto la revienta
    con un `DataError`."""
    resolved = resolve_client_ip(forwarded_for=None, peer_ip=None, trusted_proxy_hops=0)

    assert resolved is None


def test_an_invalid_peer_ip_also_returns_none() -> None:
    resolved = resolve_client_ip(forwarded_for=None, peer_ip="unknown", trusted_proxy_hops=0)

    assert resolved is None


def test_ipv6_addresses_are_accepted() -> None:
    resolved = resolve_client_ip(forwarded_for=None, peer_ip="::1", trusted_proxy_hops=0)

    assert resolved == "::1"

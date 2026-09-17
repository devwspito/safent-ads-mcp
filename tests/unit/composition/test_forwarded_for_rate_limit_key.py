"""`_make_forwarded_for_rate_limit_key`/`ApiSettings.trusted_proxy_hops`
(composition/api.py, composition/settings.py): H1 de la revision de
seguridad (16-sep, threat-model.md C-42/C-51). Sin proxy de confianza
delante (`trusted_proxy_hops=0`, el valor de `compose.companion.yaml`),
`X-Forwarded-For` NUNCA se lee -- cualquier cliente puede escribirla, asi
que rotarla en cada peticion no debe servir para esquivar el limite de
tasa del registro dinamico de clientes OAuth (C-42). Con un proxy de
confianza (`trusted_proxy_hops=1`, Caddy delante de `ads.example.com`
en la VM o `tailscale serve`/`tailscale funnel`), se confia el valor MAS A
LA DERECHA -- el que ese proxy anadio el mismo -- y se cae a la IP del
socket si la cabecera trae menos valores de los esperados."""

from __future__ import annotations

from safent_ads.composition.api import (
    _OAUTH_REGISTER_RATE_LIMIT_CAPACITY,
    RateLimitRule,
    TokenBucketRateLimiter,
    _make_forwarded_for_rate_limit_key,
)


def _scope(*, peer_ip: str, forwarded_for: bytes | None) -> dict[str, object]:
    headers = [(b"x-forwarded-for", forwarded_for)] if forwarded_for is not None else []
    return {"type": "http", "headers": headers, "client": (peer_ip, 12345)}


def _register_rate_limit_rule(*, trusted_proxy_hops: int) -> RateLimitRule:
    return RateLimitRule(
        TokenBucketRateLimiter(
            capacity=_OAUTH_REGISTER_RATE_LIMIT_CAPACITY,
            refill_per_second=_OAUTH_REGISTER_RATE_LIMIT_CAPACITY / 3600,
        ),
        key_of=_make_forwarded_for_rate_limit_key(trusted_proxy_hops),
    )


def test_zero_hops_ignores_x_forwarded_for_and_rate_limits_by_peer() -> None:
    """Doce registros, cada uno con un valor DISTINTO de `X-Forwarded-For`
    -- sin proxy de confianza, todos comparten la misma clave (la IP real
    del socket): el numero 11 (capacidad = 10) ya esta bloqueado."""
    rule = _register_rate_limit_rule(trusted_proxy_hops=0)

    allowed = [
        rule.limiter.allow(
            rule.key_of(_scope(peer_ip="203.0.113.9", forwarded_for=f"10.0.0.{i}".encode()))
        )
        for i in range(12)
    ]

    assert allowed[:10] == [True] * 10
    assert allowed[10] is False
    assert allowed[11] is False


def test_zero_hops_falls_back_to_peer_without_any_header() -> None:
    key_of = _make_forwarded_for_rate_limit_key(0)

    key = key_of(_scope(peer_ip="203.0.113.9", forwarded_for=None))

    assert key == "203.0.113.9"


def test_one_hop_trusts_only_the_rightmost_forwarded_for_value() -> None:
    """Con exactamente un proxy de confianza, el valor que ese proxy anadio
    el mismo -- el mas a la derecha -- es el que particiona el limite; lo
    que el cliente haya podido escribir a su izquierda no cuenta."""
    key_of = _make_forwarded_for_rate_limit_key(1)

    key = key_of(_scope(peer_ip="127.0.0.1", forwarded_for=b"attacker-forged, 198.51.100.7"))

    assert key == "198.51.100.7"


def test_one_hop_falls_back_to_peer_when_header_has_fewer_values_than_hops() -> None:
    """Cabecera presente pero vacia/malformada (menos valores de los
    saltos configurados): cae a la IP del socket, nunca a una clave vacia
    ni a un valor no confiable."""
    key_of = _make_forwarded_for_rate_limit_key(1)

    key = key_of(_scope(peer_ip="203.0.113.9", forwarded_for=b" , "))

    assert key == "203.0.113.9"


def test_two_hops_trusts_the_second_value_from_the_right() -> None:
    key_of = _make_forwarded_for_rate_limit_key(2)

    key = key_of(_scope(peer_ip="127.0.0.1", forwarded_for=b"198.51.100.1, 198.51.100.2"))

    assert key == "198.51.100.1"


def test_a_forwarded_for_value_that_is_not_an_ip_falls_back_to_the_peer_ip() -> None:
    """Un proxy de confianza que reenviara basura en vez de una IP (bug o
    manipulacion aguas arriba) no debe convertirse en la clave del cubo:
    cae a la IP del socket, la unica en la que se puede confiar de verdad."""
    key_of = _make_forwarded_for_rate_limit_key(1)

    key = key_of(_scope(peer_ip="203.0.113.9", forwarded_for=b"not-an-ip-address"))

    assert key == "203.0.113.9"

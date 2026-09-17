"""Primitiva SSRF pura (F-4/F-11, threat-model.md C-11/C-12): lista
BLANCA de direcciones IP -- no negra -- mas validacion sintactica de una
URL de egreso (esquema, credenciales, host, puerto). Sin dependencias de
terceros ni de infraestructura (solo `ipaddress`/`urllib.parse`): esto es
lo que permite que `brand/domain/discovery.py` lo importe sin dejar de
ser dominio puro. Resolver DNS y conectar es infraestructura
(`shared/net/safe_egress.py`); este modulo nunca hace I/O.

La revision de seguridad (checklists/website-brand-extractor-review.md,
F-4) *midio* como aceptados por la lista negra anterior (repetida en
`website_brand_extractor.py`, `http_asset_fetcher.py`, `egress_guard.py`):
`::ffff:127.0.0.1`, `::ffff:169.254.169.254`, `0.0.0.0`, `64:ff9b::7f00:1`
(NAT64), `2002:7f00:1::` (6to4), `198.18.0.0/15`, `192.0.0.0/24`,
`224.0.0.0/4`, `240.0.0.0/4`. Invertir a lista blanca (`is_global`, sin
multicast/reservado/no-especificado/loopback/link-local) mas desenrollar
las codificaciones de transicion IPv6 que Python no resuelve por si solo
(NAT64) cierra los nueve a la vez -- no una lista de parches ad-hoc."""

from __future__ import annotations

import ipaddress
from typing import Final
from urllib.parse import SplitResult, urlsplit

IpAddress = ipaddress.IPv4Address | ipaddress.IPv6Address

DEFAULT_ALLOWED_SCHEMES: Final = frozenset({"http", "https"})
DEFAULT_ALLOWED_PORTS: Final = frozenset({80, 443})

_DEFAULT_PORT_BY_SCHEME: Final[dict[str, int]] = {"http": 80, "https": 443}

# RFC 6052 "Well-Known Prefix" para NAT64: `64:ff9b::/96` es GLOBAL de
# verdad (a diferencia de `64:ff9b:1::/48`, que Python ya marca privado
# via `IPv6Address.is_private`) -- los ultimos 32 bits son literalmente
# una IPv4 sin cifrar, por eso hay que desenrollarla a mano.
_NAT64_WELL_KNOWN_PREFIX: Final = ipaddress.ip_network("64:ff9b::/96")
_IPV4_BIT_MASK: Final = 0xFFFFFFFF


class UnsafeEgressUrlError(ValueError):
    """La URL no supera la validacion sintactica de egreso -- esquema,
    puerto, credenciales embebidas, host -- antes de resolver DNS
    siquiera. Nunca requiere I/O; distinto de un fallo de resolucion o de
    una direccion bloqueada (`shared/net/safe_egress.BlockedEgressAddressError`)."""


def _embedded_ipv4(ip: IpAddress) -> ipaddress.IPv4Address | None:
    """La IPv4 que `ip` lleva embebida bajo una codificacion de
    transicion IPv6 conocida -- mapeada (`::ffff:a.b.c.d`), NAT64
    (`64:ff9b::/96`), 6to4 (`2002::/16`) o Teredo (`2001::/32`, IP de
    cliente) -- o `None` si `ip` ya es IPv4 o no lleva ninguna."""
    if isinstance(ip, ipaddress.IPv4Address):
        return None
    if ip in _NAT64_WELL_KNOWN_PREFIX:
        return ipaddress.IPv4Address(int(ip) & _IPV4_BIT_MASK)
    if ip.ipv4_mapped is not None:
        return ip.ipv4_mapped
    if ip.sixtofour is not None:
        return ip.sixtofour
    if ip.teredo is not None:
        _server_ip, client_ip = ip.teredo
        return client_ip
    return None


def _is_globally_routable(ip: IpAddress) -> bool:
    return ip.is_global and not (
        ip.is_multicast or ip.is_reserved or ip.is_unspecified or ip.is_loopback
        or ip.is_link_local
    )


def is_blocked_ip(ip: IpAddress) -> bool:
    """`True` a menos que `ip` -- o, si lleva una IPv4 embebida bajo una
    codificacion de transicion, esa IPv4 -- sea publica de verdad
    (fail-closed, lista blanca). Un unico punto de verdad para las tres
    llamadas que antes repetian su propia `_BLOCKED_NETWORKS` (F-4)."""
    embedded = _embedded_ipv4(ip)
    target = embedded if embedded is not None else ip
    return not _is_globally_routable(target)


def _is_ip_literal(host: str) -> bool:
    try:
        ipaddress.ip_address(host.strip("[]"))
    except ValueError:
        return False
    return True


def _resolved_port(scheme: str, explicit_port: int | None) -> int | None:
    return explicit_port if explicit_port is not None else _DEFAULT_PORT_BY_SCHEME.get(scheme)


def validate_egress_url(
    url: str,
    *,
    allowed_schemes: frozenset[str] = DEFAULT_ALLOWED_SCHEMES,
    allowed_ports: frozenset[int] = DEFAULT_ALLOWED_PORTS,
) -> str:
    """Puerta de entrada sintactica de toda URL de egreso (F-11): esquema
    en lista blanca, sin credenciales embebidas, host presente y no un
    literal IP, puerto (implicito o explicito) en lista blanca, host
    valido segun IDNA. Nunca resuelve DNS -- lanza `UnsafeEgressUrlError`,
    nunca una excepcion de infraestructura. Devuelve el host normalizado
    en minusculas."""
    parts = urlsplit(url)
    if parts.scheme not in allowed_schemes:
        raise UnsafeEgressUrlError(f"esquema no permitido: {url!r}")
    if parts.username or parts.password:
        raise UnsafeEgressUrlError(f"URL con credenciales embebidas no permitida: {url!r}")
    host = _hostname_of(url, parts)
    if not host:
        raise UnsafeEgressUrlError(f"URL sin host: {url!r}")
    if _is_ip_literal(host):
        raise UnsafeEgressUrlError(f"host como literal IP no permitido: {host!r}")
    port = _resolved_port(parts.scheme, _port_of(url, parts))
    if port not in allowed_ports:
        allowed = sorted(allowed_ports)
        raise UnsafeEgressUrlError(f"puerto no permitido, se exige {allowed}: {url!r}")
    return _validate_idna(host)


def _hostname_of(url: str, parts: SplitResult) -> str | None:
    try:
        return parts.hostname
    except ValueError as exc:
        raise UnsafeEgressUrlError(f"host no valido: {url!r}") from exc


def _port_of(url: str, parts: SplitResult) -> int | None:
    try:
        return parts.port
    except ValueError as exc:
        raise UnsafeEgressUrlError(f"puerto no valido: {url!r}") from exc


def _validate_idna(host: str) -> str:
    try:
        host.encode("idna")
    except UnicodeError as exc:
        raise UnsafeEgressUrlError(f"host no valido (IDNA): {host!r}") from exc
    return host.lower()

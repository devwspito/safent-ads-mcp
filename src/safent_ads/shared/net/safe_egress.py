"""Resolucion DNS y fijado de conexion sobre `ip_guard` (F-3/F-4/F-9,
threat-model.md C-11/C-12): I/O real, por eso vive fuera de `ip_guard.py`
(dominio no puede depender de esto). Reemplaza las tres copias de
`_is_blocked`/`_default_resolver`/`_assert_host_resolves_safely` que
`brand/infrastructure/website_brand_extractor.py`,
`creative/infrastructure/http_asset_fetcher.py` y
`broker/infrastructure/egress_guard.py` mantenian por separado.

`pin_request` es el nucleo de la defensa contra DNS rebinding (F-3,
CWE-367): resuelve el host UNA vez, valida que TODAS las direcciones
devueltas son publicas (si alguna cae en un rango bloqueado, se deniega
el conjunto entero -- fail closed) y reescribe la peticion para conectar
a esa MISMA IP ya validada, conservando el `Host`/SNI originales
(`sni_hostname`, que `httpcore` respeta en el handshake TLS). Sin esto,
`self._resolver(hostname)` valida una resolucion y `httpx`/`httpcore`
hacen una segunda por su cuenta al conectar -- la ventana en la que un
DNS con TTL 0 puede cambiar de respuesta entre ambas."""

from __future__ import annotations

import asyncio
import ipaddress
from collections.abc import Awaitable, Callable, Sequence
from typing import Any, Final, Protocol

import httpx

from safent_ads.shared.errors import InfrastructureError
from safent_ads.shared.net.ip_guard import IpAddress, is_blocked_ip

Resolver = Callable[[str], Awaitable[Sequence[str]]]

_RESOLVE_TIMEOUT_SECONDS: Final = 2.0
_DEFAULT_CLIENT_TIMEOUT_SECONDS: Final = 10.0


class PinnableRequest(Protocol):
    """Structural shape shared by `httpx.Request` and `httpx2.Request` --
    independent packages (M2, secreview-mac-integration.md: `httpx2` is
    the `mcp` SDK's own successor dependency, not a fork), same fields.
    `pin_request` stays the SINGLE guard for both instead of a second copy
    living in `egress_guard.py`'s `httpx2` transport."""

    extensions: dict[str, Any]

    @property
    def url(self) -> Any: ...  # httpx.URL or httpx2.URL, same shape

    @url.setter
    def url(self, value: Any) -> None: ...


class BlockedEgressAddressError(InfrastructureError):
    """El host no resolvio a ninguna direccion, la resolucion agoto el
    tiempo, fallo (F-9: NXDOMAIN, `%scope` invalido...) o alguna de las
    direcciones devueltas cae en un rango bloqueado (F-4). Cada adaptador
    (`WebsiteFetchDeniedError`, `AssetFetchDeniedError`, `EgressDeniedError`)
    atrapa esto y lo reenvia como su propio tipo publico -- este error es
    un detalle de implementacion compartido, no una API estable."""


async def default_resolver(hostname: str) -> Sequence[str]:
    loop = asyncio.get_running_loop()
    infos = await loop.getaddrinfo(hostname, None)
    return [str(info[4][0]) for info in infos]


async def resolve_pinned_ip(hostname: str, *, resolver: Resolver) -> str:
    """Resuelve `hostname` UNA vez y devuelve una direccion ya validada
    para fijar la conexion (F-3): la primera del conjunto, tras
    comprobar que NINGUNA de las devueltas esta bloqueada."""
    addresses = await _resolve_with_timeout(hostname, resolver)
    if not addresses:
        raise BlockedEgressAddressError(f"{hostname} no resolvio a ninguna direccion")
    parsed = _parse_addresses(hostname, addresses)
    if any(is_blocked_ip(ip) for ip in parsed):
        raise BlockedEgressAddressError(f"{hostname} resuelve a un rango bloqueado")
    return str(parsed[0])


async def _resolve_with_timeout(hostname: str, resolver: Resolver) -> Sequence[str]:
    try:
        async with asyncio.timeout(_RESOLVE_TIMEOUT_SECONDS):
            return await resolver(hostname)
    except TimeoutError as exc:
        raise BlockedEgressAddressError(f"resolucion de {hostname} agoto el tiempo") from exc
    except (OSError, ValueError) as exc:
        raise BlockedEgressAddressError(f"no se pudo resolver {hostname}: {exc}") from exc


def _parse_addresses(hostname: str, addresses: Sequence[str]) -> list[IpAddress]:
    try:
        return [ipaddress.ip_address(addr) for addr in addresses]
    except ValueError as exc:
        raise BlockedEgressAddressError(f"{hostname} resolvio a una direccion invalida") from exc


async def pin_request(request: PinnableRequest, *, resolver: Resolver) -> None:
    """Reescribe `request` en el sitio para conectar a la IP ya validada
    de `request.url.host` (F-3), preservando `Host`/SNI. `original_url`
    queda en `request.extensions` -- nunca en la URL de conexion -- solo
    para que los dobles de test (`httpx.MockTransport`) puedan enrutar
    por la URL logica sin acoplarse a que la IP fijada sea estable."""
    hostname = request.url.host
    pinned_ip = await resolve_pinned_ip(hostname, resolver=resolver)
    original_url = str(request.url)
    request.url = request.url.copy_with(host=pinned_ip)
    request.extensions["sni_hostname"] = hostname
    request.extensions["original_url"] = original_url


async def open_pinned_stream(
    client: httpx.AsyncClient,
    method: str,
    url: str,
    *,
    resolver: Resolver,
    timeout: float | httpx.Timeout,
    headers: dict[str, str] | None = None,
) -> httpx.Response:
    """Construye la peticion, la fija a la IP validada (F-3) y la envia
    en modo streaming. El llamante es responsable de `await
    response.aclose()` (mismo contrato que `httpx.AsyncClient.stream`,
    pero building block en vez de gestor de contexto: el llamante
    necesita inspeccionar `response.is_redirect` antes de decidir si
    sigue leyendo el cuerpo o repite la llamada con una URL nueva)."""
    request = client.build_request(method, url, headers=headers, timeout=timeout)
    await pin_request(request, resolver=resolver)
    return await client.send(request, stream=True)


class _AllowlistedPinnedTransport(httpx.AsyncHTTPTransport):
    """Transporte `httpx` que aplica una lista blanca de host EXACTO y fija la
    conexion a la IP ya validada (F-3, CWE-367) antes de abrirla. Mismo guard
    unico (`pin_request`/`is_blocked_ip`) que `open_pinned_stream` y que
    `broker/infrastructure/egress_guard.py`: aqui solo cambia que la lista
    blanca llega por parametro en vez de fija, para que un llamante con hosts
    conocidos en tiempo de compilacion (el canje OIDC de 002b contra
    `oauth2.googleapis.com`) la use sin copiar ni una linea de bloqueo.

    `trust_env` se propaga a `httpx.AsyncHTTPTransport`: es el transporte, no
    el cliente, quien construye el contexto TLS, y con el entorno honrado
    `SSL_CERT_FILE`/`SSL_CERT_DIR` sustituyen el almacen de confianza del
    canal por el que viaja el `client_secret`."""

    def __init__(
        self,
        *,
        allowed_hosts: frozenset[str],
        resolver: Resolver | None = None,
        trust_env: bool = False,
    ) -> None:
        super().__init__(trust_env=trust_env)
        self._allowed_hosts = allowed_hosts
        self._resolver = resolver or default_resolver

    async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
        host = request.url.host
        if host not in self._allowed_hosts:
            raise BlockedEgressAddressError(f"host fuera de la lista blanca: {host!r}")
        if request.url.userinfo:
            # `httpx.HTTPStatusError` imprime la URL entera, credenciales
            # incluidas; el unico modo de que un error de este transporte no
            # las lleve nunca es que no lleguen a salir. Los mensajes de aqui
            # nombran el host, jamas la URL.
            raise BlockedEgressAddressError(f"URL con credenciales embebidas hacia {host!r}")
        await pin_request(request, resolver=self._resolver)
        return await super().handle_async_request(request)


def build_pinned_async_client(
    *,
    allowed_hosts: frozenset[str],
    timeout: float = _DEFAULT_CLIENT_TIMEOUT_SECONDS,
    resolver: Resolver | None = None,
    trust_env: bool = False,
) -> httpx.AsyncClient:
    """`httpx.AsyncClient` que solo habla con `allowed_hosts` y fija cada
    conexion a la IP validada. Building block compartido: el llamante trae su
    propia lista de hosts fijos conocidos en tiempo de compilacion.

    `trust_env=False` por defecto (y no `True`, como en el borrador del que
    procede): un cliente fijado a IP cuyo `HTTPS_PROXY`/`SSL_CERT_FILE` salga
    del entorno deja de estar fijado a nada. Las redirecciones tampoco se
    siguen -- es el defecto de `httpx` y aqui es una garantia, no una
    casualidad: un 3xx resolveria y conectaria otra vez fuera del guard."""
    return httpx.AsyncClient(
        transport=_AllowlistedPinnedTransport(
            allowed_hosts=allowed_hosts, resolver=resolver, trust_env=trust_env
        ),
        timeout=timeout,
        trust_env=trust_env,
        follow_redirects=False,
    )

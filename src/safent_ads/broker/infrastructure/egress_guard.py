"""Allow-list de egreso del proceso `ads-broker` (T028, threat-model.md
C-12: "bloqueo de loopback, link-local y RFC1918"), sobre
`shared/net/safe_egress.py` (F-3/F-4): mismo bloqueo de IP que
`brand.infrastructure.website_brand_extractor`/
`creative.infrastructure.http_asset_fetcher`, sin repetir la lista aqui.

Decision documentada: Telegram (`api.telegram.org`) **no** esta en la
lista. Lo usa `ads-api` (notifications, plan.md §5), no el broker — el
broker solo habla con Google/Meta/OAuth. Incluirlo aqui ampliaria
innecesariamente la superficie de egreso del proceso que guarda las
credenciales de plataforma.

Integracion: los adaptadores de `broker/platforms/` usan hosts fijos
conocidos en tiempo de compilacion (nunca un host que llegue de fuera), asi
que la comprobacion de mas valor es sobre la propia resolucion DNS de esos
hosts fijos, hecha una vez al construir el cliente real del SDK (frontera
de infraestructura) — no en cada llamada de los tests unitarios, que
sustituyen el SDK por un doble y no tocan red (T025/T026).

M2 (secreview-mac-integration.md): `native_mcp.py`'s Meta path DOES speak
`httpx`/`httpx2` directly (the MCP `streamable_http_client` transport is
hard-typed to `httpx2.AsyncClient` upstream) -- `assert_egress_allowed` alone
is only a pre-flight resolve, not a pin; both clients now go through a
guarded transport built on `shared/net/safe_egress.pin_request` (single
guard, never copied) so the ACTUAL connection is fixed to the resolved IP,
not just checked ahead of time."""

from __future__ import annotations

from typing import Final

import httpx
import httpx2

from safent_ads.shared.errors import InfrastructureError
from safent_ads.shared.net.safe_egress import (
    BlockedEgressAddressError,
    Resolver,
    default_resolver,
    pin_request,
    resolve_pinned_ip,
)

ALLOWED_EGRESS_HOSTS: Final = frozenset(
    {
        "graph.facebook.com",
        "mcp.facebook.com",
        "googleads.googleapis.com",
        "tagmanager.googleapis.com",
        "oauth2.googleapis.com",
        "backend.composio.dev",
    }
)


class EgressDeniedError(InfrastructureError):
    """Host fuera de la lista blanca, o resuelve a un rango bloqueado."""


async def assert_egress_allowed(hostname: str, *, resolver: Resolver | None = None) -> None:
    """Lanza `EgressDeniedError` si `hostname` no esta en la lista blanca o
    si resuelve a un rango bloqueado (F-4, `shared.net.ip_guard`).
    `resolver` inyectable para tests deterministas sin DNS real."""
    if hostname not in ALLOWED_EGRESS_HOSTS:
        raise EgressDeniedError(f"host fuera de la lista blanca: {hostname!r}")
    try:
        await resolve_pinned_ip(hostname, resolver=resolver or default_resolver)
    except BlockedEgressAddressError as exc:
        raise EgressDeniedError(str(exc)) from exc


async def _guard_and_pin(
    hostname: str, request: httpx.Request | httpx2.Request, resolver: Resolver
) -> None:
    if hostname not in ALLOWED_EGRESS_HOSTS:
        raise EgressDeniedError(f"host fuera de la lista blanca: {hostname!r}")
    try:
        await pin_request(request, resolver=resolver)
    except BlockedEgressAddressError as exc:
        raise EgressDeniedError(str(exc)) from exc


class _EgressGuardedTransport(httpx.AsyncHTTPTransport):
    """Transporte `httpx` que aplica la lista blanca de host y fija la
    conexion a la IP ya validada (F-3, CWE-367: sin esto, `httpcore`
    resolveria una segunda vez al conectar, la ventana en la que un DNS
    con TTL 0 puede colar una IP bloqueada) antes de abrir la conexion
    real. Usado por `build_guarded_async_client` (OAuth, Meta `me/
    permissions`); ver `_EgressGuardedTransport2` para el equivalente
    `httpx2` (MCP `streamable_http_client`)."""

    def __init__(self, *, resolver: Resolver | None = None) -> None:
        super().__init__()
        self._resolver = resolver or default_resolver

    async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
        await _guard_and_pin(request.url.host, request, self._resolver)
        return await super().handle_async_request(request)


def build_guarded_async_client(
    *, timeout: float = 10.0, resolver: Resolver | None = None, trust_env: bool = True
) -> httpx.AsyncClient:
    return httpx.AsyncClient(
        transport=_EgressGuardedTransport(resolver=resolver), timeout=timeout, trust_env=trust_env
    )


class _EgressGuardedTransport2(httpx2.AsyncHTTPTransport):
    """`httpx2` twin of `_EgressGuardedTransport` (M2, secreview-mac-
    integration.md): the MCP SDK's `streamable_http_client` only accepts an
    `httpx2.AsyncClient`, so the Meta native MCP session cannot go through
    plain `httpx`. Same guard (`_guard_and_pin`/`pin_request`), no second
    copy of the allow-list or the IP-blocking logic -- only the transport
    base class differs, because `httpx`/`httpx2` are independent packages."""

    def __init__(self, *, resolver: Resolver | None = None) -> None:
        super().__init__()
        self._resolver = resolver or default_resolver

    async def handle_async_request(self, request: httpx2.Request) -> httpx2.Response:
        await _guard_and_pin(request.url.host, request, self._resolver)
        return await super().handle_async_request(request)


def build_guarded_async_client2(
    *,
    timeout: float = 10.0,
    resolver: Resolver | None = None,
    trust_env: bool = True,
    headers: dict[str, str] | None = None,
) -> httpx2.AsyncClient:
    # `headers` at construction, not per-call: upstream `streamable_http_
    # client` (mcp.client.streamable_http) issues its own requests on this
    # client without letting the caller attach headers per request.
    return httpx2.AsyncClient(
        transport=_EgressGuardedTransport2(resolver=resolver),
        timeout=timeout,
        trust_env=trust_env,
        headers=headers,
    )

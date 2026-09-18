"""`assert_egress_allowed` (T028, threat-model.md C-12): lista blanca +
bloqueo de metadata cloud/loopback/RFC1918/CGNAT, con resolver inyectable
para no depender de DNS real en los tests."""

from __future__ import annotations

from collections.abc import Sequence

import httpx
import httpx2
import pytest

from safent_ads.broker.infrastructure.egress_guard import (
    ALLOWED_EGRESS_HOSTS,
    EgressDeniedError,
    _EgressGuardedTransport,
    _EgressGuardedTransport2,
    assert_egress_allowed,
    build_guarded_async_client,
    build_guarded_async_client2,
)


def _resolver(addresses: Sequence[str]):
    async def resolve(hostname: str) -> Sequence[str]:  # noqa: ARG001 - firma del resolver
        return addresses

    return resolve


def _flip_resolver(first: Sequence[str], second: Sequence[str]):
    """Returns `first` on the FIRST call, `second` on every call after --
    models a DNS answer that changes between a pre-flight resolve and the
    resolution `httpcore` would otherwise do again at connect time."""
    calls = {"count": 0}

    async def resolve(hostname: str) -> Sequence[str]:  # noqa: ARG001 - firma del resolver
        calls["count"] += 1
        return first if calls["count"] == 1 else second

    return resolve


async def test_egress_denies_169_254_169_254() -> None:
    with pytest.raises(EgressDeniedError):
        await assert_egress_allowed(
            "googleads.googleapis.com", resolver=_resolver(["169.254.169.254"])
        )


async def test_egress_allows_legitimate_public_ip() -> None:
    await assert_egress_allowed("graph.facebook.com", resolver=_resolver(["157.240.2.35"]))


@pytest.mark.parametrize(
    "blocked_ip",
    [
        "127.0.0.1",
        "10.0.0.5",
        "172.16.0.5",
        "192.168.1.1",
        "100.64.0.1",
        "::1",
        "fe80::1",
        "fc00::1",
    ],
)
async def test_denies_all_blocked_ranges(blocked_ip: str) -> None:
    with pytest.raises(EgressDeniedError):
        await assert_egress_allowed("oauth2.googleapis.com", resolver=_resolver([blocked_ip]))


async def test_denies_host_outside_allow_list() -> None:
    with pytest.raises(EgressDeniedError):
        await assert_egress_allowed("evil.example.com", resolver=_resolver(["8.8.8.8"]))


def test_allow_list_matches_native_ads_tag_manager_and_managed_hosts_only() -> None:
    assert ALLOWED_EGRESS_HOSTS == {
        "graph.facebook.com",
        "mcp.facebook.com",
        "googleads.googleapis.com",
        "tagmanager.googleapis.com",
        "oauth2.googleapis.com",
        "backend.composio.dev",
    }


def test_telegram_is_not_in_the_broker_allow_list() -> None:
    """Decision documentada: Telegram lo usa `ads-api`, no el broker."""
    assert "api.telegram.org" not in ALLOWED_EGRESS_HOSTS


async def test_transport_denies_before_connecting_when_dns_rebinds_to_a_blocked_ip() -> None:
    """F-3: `_EgressGuardedTransport` fija la conexion a la IP ya
    validada (`pin_request`) antes de delegar en el transporte real --
    aqui el resolver devuelve una direccion bloqueada para un host
    permitido, y la peticion debe fallar sin llegar nunca a abrir un
    socket real (`super().handle_async_request` no se alcanza)."""
    transport = _EgressGuardedTransport(resolver=_resolver(["169.254.169.254"]))
    request = httpx.Request("GET", "https://googleads.googleapis.com/")

    with pytest.raises(EgressDeniedError, match="rango bloqueado"):
        await transport.handle_async_request(request)


def test_build_guarded_async_client_accepts_an_injectable_resolver() -> None:
    client = build_guarded_async_client(resolver=_resolver(["157.240.2.35"]))

    assert isinstance(client._transport, _EgressGuardedTransport)  # noqa: SLF001


async def test_transport2_denies_before_connecting_when_dns_rebinds_to_a_blocked_ip() -> None:
    """M2 (secreview-mac-integration.md): `httpx2` twin of
    `test_transport_denies_before_connecting_when_dns_rebinds_to_a_blocked_ip`
    -- the MCP SDK's `streamable_http_client` only accepts an
    `httpx2.AsyncClient`, so the Meta native MCP session needs its own
    guarded transport, built on the same `pin_request` guard."""
    transport = _EgressGuardedTransport2(resolver=_resolver(["169.254.169.254"]))
    request = httpx2.Request("GET", "https://mcp.facebook.com/ads")

    with pytest.raises(EgressDeniedError, match="rango bloqueado"):
        await transport.handle_async_request(request)


@pytest.mark.parametrize(
    ("transport_cls", "request_cls"),
    [(_EgressGuardedTransport, httpx.Request), (_EgressGuardedTransport2, httpx2.Request)],
)
async def test_a_dns_answer_that_changes_after_the_preflight_is_still_refused(
    transport_cls, request_cls
) -> None:
    """M2: `assert_egress_allowed` is only a pre-flight resolve -- the
    FIRST lookup. Without a pinned connection, the real connect triggers a
    SECOND, independent resolution (CWE-367 DNS rebinding); here that
    second lookup returns a private IP after a clean pre-flight, and the
    guarded transport used for the actual connection must still refuse."""
    resolver = _flip_resolver(["157.240.2.35"], ["169.254.169.254"])
    await assert_egress_allowed("mcp.facebook.com", resolver=resolver)  # first lookup: OK

    transport = transport_cls(resolver=resolver)
    request = request_cls("GET", "https://mcp.facebook.com/ads")

    with pytest.raises(EgressDeniedError, match="rango bloqueado"):
        await transport.handle_async_request(request)


def test_build_guarded_async_client2_accepts_an_injectable_resolver() -> None:
    client = build_guarded_async_client2(resolver=_resolver(["157.240.2.35"]))

    assert isinstance(client._transport, _EgressGuardedTransport2)  # noqa: SLF001


def test_build_guarded_async_client2_sets_headers_at_construction() -> None:
    """`streamable_http_client` issues its own requests on this client
    without a way to attach headers per call -- the Authorization header
    for the Meta MCP session has to live on the client itself."""
    client = build_guarded_async_client2(headers={"Authorization": "Bearer token"})

    assert client.headers["authorization"] == "Bearer token"

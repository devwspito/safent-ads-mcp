"""`safe_egress`: resolucion con resolver inyectable (F-9) y fijado de
conexion (F-3, DNS rebinding, CWE-367) -- sin red real, `httpx.MockTransport`
mismo patron que `tests/unit/creative/infrastructure/test_http_asset_fetcher.py`."""

from __future__ import annotations

import asyncio
from collections.abc import Callable, Sequence

import httpx
import pytest

from safent_ads.shared.net.safe_egress import (
    BlockedEgressAddressError,
    build_pinned_async_client,
    open_pinned_stream,
    pin_request,
    resolve_pinned_ip,
)

_PUBLIC_IP = "93.184.216.34"


def _resolver_returning(*addresses: str):
    async def _resolve(hostname: str) -> Sequence[str]:  # noqa: ARG001
        return list(addresses)

    return _resolve


def _failing_resolver(exc: Exception):
    async def _resolve(hostname: str) -> Sequence[str]:  # noqa: ARG001
        raise exc

    return _resolve


async def test_resolve_pinned_ip_returns_the_first_address_when_all_are_public() -> None:
    ip = await resolve_pinned_ip("example.test", resolver=_resolver_returning(_PUBLIC_IP))

    assert ip == _PUBLIC_IP


async def test_resolve_pinned_ip_denies_when_any_resolved_address_is_blocked() -> None:
    """Varios registros A (F-4 "seguro por diseno"): si UNA cae en un
    rango bloqueado, se deniega el conjunto entero."""
    resolver = _resolver_returning(_PUBLIC_IP, "127.0.0.1")

    with pytest.raises(BlockedEgressAddressError, match="rango bloqueado"):
        await resolve_pinned_ip("example.test", resolver=resolver)


async def test_resolve_pinned_ip_denies_when_resolution_is_empty() -> None:
    with pytest.raises(BlockedEgressAddressError):
        await resolve_pinned_ip("example.test", resolver=_resolver_returning())


async def test_resolve_pinned_ip_wraps_resolver_os_errors(monkeypatch: pytest.MonkeyPatch) -> None:
    """F-9: NXDOMAIN u otro fallo del resolver del sistema nunca escapa
    como excepcion generica no controlada."""
    resolver = _failing_resolver(OSError("Name or service not known"))

    with pytest.raises(BlockedEgressAddressError, match="no se pudo resolver"):
        await resolve_pinned_ip("nonexistent.example.test", resolver=resolver)


async def test_resolve_pinned_ip_wraps_resolver_value_errors() -> None:
    """F-9: un host con un `%scope` de IPv6 invalido revienta `getaddrinfo`
    con `ValueError`, no con `OSError`."""
    resolver = _failing_resolver(ValueError("invalid IPv6 scope"))

    with pytest.raises(BlockedEgressAddressError, match="no se pudo resolver"):
        await resolve_pinned_ip("fe80--bad.example.test", resolver=resolver)


async def test_resolve_pinned_ip_times_out_a_stalled_resolver() -> None:
    async def _stalled_resolver(hostname: str) -> Sequence[str]:  # noqa: ARG001
        await asyncio.sleep(10)
        return [_PUBLIC_IP]

    with pytest.raises(BlockedEgressAddressError, match="agoto el tiempo"):
        await resolve_pinned_ip("slow.example.test", resolver=_stalled_resolver)


async def test_pin_request_rewrites_the_connection_target_and_keeps_host_and_sni() -> None:
    client = httpx.AsyncClient(transport=httpx.MockTransport(lambda _request: httpx.Response(200)))
    request = client.build_request("GET", "https://example.test/path?x=1")

    await pin_request(request, resolver=_resolver_returning(_PUBLIC_IP))

    assert request.url.host == _PUBLIC_IP
    assert request.headers["host"] == "example.test"
    assert request.extensions["sni_hostname"] == "example.test"
    assert request.extensions["original_url"] == "https://example.test/path?x=1"


async def test_pin_request_rejects_when_dns_rebinds_between_check_and_connect() -> None:
    """F-3: si el mismo resolver que valida es el que conecta, un DNS con
    TTL 0 que cambie de respuesta entre la validacion y el `connect()`
    nunca puede colar una IP bloqueada -- aqui el resolver YA devuelve la
    direccion interna, exactamente lo que pasaria en el segundo salto de
    una resolucion con rebinding."""
    client = httpx.AsyncClient(transport=httpx.MockTransport(lambda _request: httpx.Response(200)))
    request = client.build_request("GET", "https://attacker-controlled.test/")

    with pytest.raises(BlockedEgressAddressError, match="rango bloqueado"):
        await pin_request(request, resolver=_resolver_returning("169.254.169.254"))


async def test_open_pinned_stream_lets_the_mock_transport_see_the_logical_url() -> None:
    seen: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request.extensions["original_url"])
        return httpx.Response(200, content=b"ok")

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))

    response = await open_pinned_stream(
        client,
        "GET",
        "https://example.test/",
        resolver=_resolver_returning(_PUBLIC_IP),
        timeout=5.0,
    )
    try:
        body = await response.aread()
    finally:
        await response.aclose()

    assert body == b"ok"
    assert seen == ["https://example.test/"]


async def test_open_pinned_stream_denies_before_sending_when_blocked() -> None:
    def handler(request: httpx.Request) -> httpx.Response:  # noqa: ARG001
        raise AssertionError("no deberia llegar a enviar la peticion")

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))

    with pytest.raises(BlockedEgressAddressError):
        await open_pinned_stream(
            client,
            "GET",
            "https://internal.test/",
            resolver=_resolver_returning("10.0.0.5"),
            timeout=5.0,
        )


# --- lane 002b/T010: cliente httpx con lista blanca de host + IP fijada ---

_ALLOWED_HOST = "oauth2.googleapis.com"
_ALLOWED_URL = f"https://{_ALLOWED_HOST}/token"


def _stub_upstream(
    monkeypatch: pytest.MonkeyPatch,
    responder: Callable[[httpx.Request], httpx.Response],
) -> list[httpx.Request]:
    """Sustituye el envio real de `httpx.AsyncHTTPTransport` (la clase base de
    `_AllowlistedPinnedTransport`) para ejercitar el guard entero -- lista
    blanca, fijado y configuracion del cliente -- sin abrir un socket."""
    seen: list[httpx.Request] = []

    async def _handle(self: httpx.AsyncHTTPTransport, request: httpx.Request) -> httpx.Response:  # noqa: ARG001
        seen.append(request)
        return responder(request)

    monkeypatch.setattr(httpx.AsyncHTTPTransport, "handle_async_request", _handle)
    return seen


def _ok(_request: httpx.Request) -> httpx.Response:
    return httpx.Response(200, json={"ok": True})


async def test_pinned_client_denies_a_host_outside_the_allowlist(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    seen = _stub_upstream(monkeypatch, _ok)
    client = build_pinned_async_client(
        allowed_hosts=frozenset({_ALLOWED_HOST}), resolver=_resolver_returning(_PUBLIC_IP)
    )

    async with client:
        with pytest.raises(BlockedEgressAddressError, match="lista blanca"):
            await client.post("https://accounts.google.test/token", data={})

    assert seen == []


async def test_pinned_client_denies_an_ipv4_mapped_private_address(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """El agujero real: `::ffff:169.254.169.254` es la metadata de la nube
    disfrazada de IPv6. Un host en la lista blanca cuyo DNS resuelva a una
    IPv4 mapeada tiene que morir igual que la IPv4 desnuda."""
    seen = _stub_upstream(monkeypatch, _ok)
    client = build_pinned_async_client(
        allowed_hosts=frozenset({_ALLOWED_HOST}),
        resolver=_resolver_returning("::ffff:169.254.169.254"),
    )

    async with client:
        with pytest.raises(BlockedEgressAddressError, match="rango bloqueado"):
            await client.post(_ALLOWED_URL, data={})

    assert seen == []


async def test_pinned_client_pins_the_connection_and_keeps_host_and_sni(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    seen = _stub_upstream(monkeypatch, _ok)
    client = build_pinned_async_client(
        allowed_hosts=frozenset({_ALLOWED_HOST}), resolver=_resolver_returning(_PUBLIC_IP)
    )

    async with client:
        response = await client.post(_ALLOWED_URL, data={"grant_type": "authorization_code"})

    assert response.status_code == 200
    assert seen[0].url.host == _PUBLIC_IP
    assert seen[0].headers["host"] == _ALLOWED_HOST
    assert seen[0].extensions["sni_hostname"] == _ALLOWED_HOST


async def test_pinned_client_does_not_follow_redirects(monkeypatch: pytest.MonkeyPatch) -> None:
    """Seguir una redireccion volveria a resolver y a conectar fuera del
    control del guard: la respuesta 3xx se devuelve tal cual y no se emite
    una segunda peticion."""
    seen = _stub_upstream(
        monkeypatch,
        lambda _request: httpx.Response(302, headers={"location": "https://attacker.test/"}),
    )
    client = build_pinned_async_client(
        allowed_hosts=frozenset({_ALLOWED_HOST}), resolver=_resolver_returning(_PUBLIC_IP)
    )

    async with client:
        response = await client.post(_ALLOWED_URL, data={})

    assert response.status_code == 302
    assert len(seen) == 1


async def test_pinned_client_ignores_an_https_proxy_from_the_environment(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """`trust_env=False`: un `HTTPS_PROXY` en el entorno del contenedor no
    puede desviar el canje del `code` a un intermediario que vea el
    `client_secret`."""
    monkeypatch.setenv("HTTPS_PROXY", "http://127.0.0.1:9/")
    monkeypatch.setenv("ALL_PROXY", "http://127.0.0.1:9/")
    seen = _stub_upstream(monkeypatch, _ok)
    client = build_pinned_async_client(
        allowed_hosts=frozenset({_ALLOWED_HOST}), resolver=_resolver_returning(_PUBLIC_IP)
    )

    async with client:
        assert client.trust_env is False
        response = await client.post(_ALLOWED_URL, data={})

    assert response.status_code == 200
    assert seen[0].url.host == _PUBLIC_IP


async def test_pinned_client_ignores_a_ca_bundle_from_the_environment(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """`trust_env=False` tiene que llegar tambien al transporte: es ahi donde
    `httpx` construye el contexto TLS y donde `SSL_CERT_FILE`/`SSL_CERT_DIR`
    cambiarian el almacen de confianza del cliente que lleva el
    `client_secret`. Con el entorno honrado, este fichero inexistente
    reventaria la construccion."""
    monkeypatch.setenv("SSL_CERT_FILE", "/nonexistent/rogue-ca.pem")

    client = build_pinned_async_client(allowed_hosts=frozenset({_ALLOWED_HOST}))

    async with client:
        assert client.trust_env is False


async def test_pinned_client_applies_the_requested_timeout(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    seen = _stub_upstream(monkeypatch, _ok)
    client = build_pinned_async_client(
        allowed_hosts=frozenset({_ALLOWED_HOST}),
        timeout=10.0,
        resolver=_resolver_returning(_PUBLIC_IP),
    )

    async with client:
        assert client.timeout == httpx.Timeout(10.0)
        await client.post(_ALLOWED_URL, data={})

    assert seen[0].extensions["timeout"] == {
        "connect": 10.0,
        "pool": 10.0,
        "read": 10.0,
        "write": 10.0,
    }


async def test_pinned_client_denies_a_url_with_embedded_credentials(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """`httpx.HTTPStatusError` imprime la URL completa, con `user:password`
    dentro. Un cliente de egreso fijado no tiene por que llevar credenciales
    en la URL nunca, asi que se deniegan antes de enviar y el mensaje del
    error solo nombra el host."""
    seen = _stub_upstream(monkeypatch, _ok)
    client = build_pinned_async_client(
        allowed_hosts=frozenset({_ALLOWED_HOST}), resolver=_resolver_returning(_PUBLIC_IP)
    )

    async with client:
        with pytest.raises(BlockedEgressAddressError) as raised:
            await client.post(f"https://user:sup3rsecret@{_ALLOWED_HOST}/token", data={})

    assert "sup3rsecret" not in str(raised.value)
    assert seen == []

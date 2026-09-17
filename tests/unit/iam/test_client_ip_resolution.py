"""`_client_ip` (iam/presentation/router.py, reauth.py; code review 17-sep,
item 2): ninguna de las dos puede devolver el literal `"unknown"` cuando
`request.client` esta ausente -- `login_attempts.ip_address` es `INET`, y
ese texto no es una IP valida (`DataError`, 500). Las dos delegan en
`shared/net/client_ip.py`, la MISMA funcion que ya prueba
`tests/unit/shared/net/test_client_ip.py`."""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any

from starlette.requests import Request

from safent_ads.iam.presentation.reauth import _client_ip as reauth_client_ip
from safent_ads.iam.presentation.router import _client_ip as router_client_ip


def _request(
    *,
    client: tuple[str, int] | None,
    forwarded_for: str | None = None,
    trusted_proxy_hops: int = 0,
) -> Request:
    headers: list[tuple[bytes, bytes]] = []
    if forwarded_for is not None:
        headers.append((b"x-forwarded-for", forwarded_for.encode()))
    app: Any = SimpleNamespace(
        state=SimpleNamespace(
            container=SimpleNamespace(
                settings=SimpleNamespace(trusted_proxy_hops=trusted_proxy_hops)
            )
        )
    )
    scope = {"type": "http", "headers": headers, "client": client, "app": app}
    return Request(scope)


def test_router_client_ip_returns_none_without_a_peer_or_a_valid_header() -> None:
    request = _request(client=None)

    assert router_client_ip(request, trusted_proxy_hops=0) is None


def test_router_client_ip_returns_the_peer_ip_by_default() -> None:
    request = _request(client=("203.0.113.9", 12345))

    assert router_client_ip(request, trusted_proxy_hops=0) == "203.0.113.9"


def test_router_client_ip_honors_one_trusted_hop() -> None:
    request = _request(
        client=("127.0.0.1", 1), forwarded_for="198.51.100.7", trusted_proxy_hops=1
    )

    assert router_client_ip(request, trusted_proxy_hops=1) == "198.51.100.7"


def test_reauth_client_ip_returns_none_without_a_peer_or_a_valid_header() -> None:
    request = _request(client=None, trusted_proxy_hops=0)

    assert reauth_client_ip(request) is None


def test_reauth_client_ip_reads_trusted_proxy_hops_from_the_apps_container() -> None:
    """`reauth.py` no cambia de firma (muchos llamadores indirectos via
    `require_fresh_identification`): el salto de confianza sale de
    `request.app.state.container.settings`, mismo patron que
    `action_confirmation.py` ya usa en este paquete de presentacion."""
    request = _request(
        client=("127.0.0.1", 1), forwarded_for="198.51.100.7", trusted_proxy_hops=1
    )

    assert reauth_client_ip(request) == "198.51.100.7"


def test_reauth_client_ip_ignores_the_header_with_zero_trusted_hops() -> None:
    request = _request(
        client=("203.0.113.9", 1), forwarded_for="198.51.100.7", trusted_proxy_hops=0
    )

    assert reauth_client_ip(request) == "203.0.113.9"

"""`LOOPBACK_HOSTS`/`is_loopback_http_origin` (revision de PR 44, T049):
unica fuente del bucle local que `ApiSettings`, el asistente de primer
arranque, `RedirectUri` y el callback de OAuth gestionado comparten."""

from __future__ import annotations

import pytest

from safent_ads.shared.net.loopback import LOOPBACK_HOSTS, is_loopback_http_origin


def test_loopback_hosts_is_exactly_the_three_rfc_8252_literals() -> None:
    assert LOOPBACK_HOSTS == {"127.0.0.1", "localhost", "::1"}


@pytest.mark.parametrize(
    "url",
    [
        "http://127.0.0.1",
        "http://127.0.0.1:8410",
        "http://localhost",
        "http://localhost:8410",
        "http://LOCALHOST:8410",
        "http://[::1]",
        "http://[::1]:8410",
    ],
)
def test_accepts_http_to_every_loopback_literal(url: str) -> None:
    assert is_loopback_http_origin(url) is True


@pytest.mark.parametrize(
    "url",
    [
        "https://127.0.0.1",
        "https://localhost",
        "https://[::1]",
        "http://ads.example.com",
        "http://127.0.0.1.evil.example:8410",
        "http://localhost.evil",
        "http://127.0.0.2",
        "ftp://127.0.0.1",
        "http://[::1",
    ],
)
def test_rejects_everything_else(url: str) -> None:
    assert is_loopback_http_origin(url) is False

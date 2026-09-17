"""`RedirectUri` (data-model.md, RFC 8252 §7.3, threat-model.md C-37 y
C-70 pieza 4): bucle local EXCLUSIVAMENTE desde D-11 -- `http` y `https`
a `127.0.0.1`, `[::1]` o `localhost`, con o sin puerto. Cualquier otro
destino (incluido `https` a un host de Internet, que hasta 0.2.40 se
aceptaba) se rechaza al construir, que es tanto como decir al registrar.

Revision de seguridad (17-sep): ademas del conjunto cerrado de
autoridades, aqui se fija que NADA salga de esta clase que no sea
`InvalidRedirectUriError` -- ni el `ValueError` de `urlsplit` con una
autoridad rota, ni un puerto fuera de rango, ni una URI con caracteres de
control que `urlsplit` borra en silencio."""

from __future__ import annotations

import pytest

from safent_ads.mcp_oauth.domain.client import RedirectUri
from safent_ads.mcp_oauth.domain.errors import InvalidRedirectUriError

_LOOPBACK_AUTHORITIES = ("127.0.0.1", "[::1]", "localhost")


# ── lo que D-11 acepta ──────────────────────────────────────────────────


@pytest.mark.parametrize("authority", _LOOPBACK_AUTHORITIES)
@pytest.mark.parametrize("scheme", ("http", "https"))
def test_accepts_the_three_loopback_hosts_with_a_port(scheme: str, authority: str) -> None:
    RedirectUri(f"{scheme}://{authority}:54321/callback")


@pytest.mark.parametrize("authority", _LOOPBACK_AUTHORITIES)
@pytest.mark.parametrize("scheme", ("http", "https"))
def test_accepts_the_three_loopback_hosts_without_a_port(scheme: str, authority: str) -> None:
    RedirectUri(f"{scheme}://{authority}/callback")


def test_accepts_a_loopback_uri_with_a_nested_path_and_query() -> None:
    """La ruta la elige el agente (Codex usa `/auth/callback`): D-11
    cierra el HOST, no la forma de la ruta."""
    RedirectUri("http://127.0.0.1:1455/auth/callback?flow=mcp")


def test_the_host_is_compared_case_insensitively() -> None:
    RedirectUri("http://LOCALHOST:54321/callback")


def test_the_scheme_is_compared_case_insensitively() -> None:
    RedirectUri("HTTP://LOCALHOST")


def test_accepts_a_loopback_uri_without_a_path() -> None:
    RedirectUri("http://127.0.0.1")


def test_accepts_the_highest_valid_port() -> None:
    RedirectUri("http://127.0.0.1:65535/callback")


# ── lo que D-11 cierra ──────────────────────────────────────────────────


def test_rejects_https_non_loopback() -> None:
    """El cambio de D-11: hasta 0.2.40 esto se registraba sin rechistar y
    el acceso podia acabar en un servidor de Internet."""
    with pytest.raises(InvalidRedirectUriError):
        RedirectUri("https://agent.example/callback")


def test_rejects_http_non_loopback() -> None:
    with pytest.raises(InvalidRedirectUriError):
        RedirectUri("http://agent.example/callback")


@pytest.mark.parametrize(
    "value",
    (
        "http://127.0.0.1.evil.com/callback",
        "https://127.0.0.1.evil.com/callback",
        "http://localhost.evil/callback",
        "http://evil.localhost/callback",
    ),
)
def test_rejects_a_host_that_only_looks_like_loopback(value: str) -> None:
    with pytest.raises(InvalidRedirectUriError):
        RedirectUri(value)


@pytest.mark.parametrize(
    "authority",
    (
        "[0:0:0:0:0:0:0:1]",  # la forma expandida de ::1 NO esta en el conjunto
        "[::ffff:127.0.0.1]",  # IPv4 mapeada en IPv6
        "127.0.0.2",  # el resto de 127.0.0.0/8 no entra
        "127.1",  # forma abreviada de 127.0.0.1
        "2130706433",  # 127.0.0.1 en decimal
        "0.0.0.0",  # noqa: S104 - "todas las interfaces": el caso que se RECHAZA
        "localhost.",  # raiz DNS explicita
    ),
)
def test_the_closed_set_is_exactly_three_authorities(authority: str) -> None:
    """Revision de seguridad (17-sep): el conjunto son TRES autoridades
    literales, no "cualquier cosa que resuelva a 127.0.0.1". Cada una de
    estas es equivalente a nivel de red y aun asi se rechaza -- ampliar la
    lista es una decision, no un arreglo (`contracts/federated-login.md`
    §5)."""
    with pytest.raises(InvalidRedirectUriError):
        RedirectUri(f"http://{authority}/callback")


def test_rejects_userinfo_that_hides_a_remote_host() -> None:
    """`127.0.0.1@evil.example` resuelve a `evil.example`: la autoridad
    con `@` se rechaza antes de mirar el host."""
    with pytest.raises(InvalidRedirectUriError):
        RedirectUri("http://127.0.0.1@evil.example/callback")


def test_rejects_userinfo_even_towards_a_loopback_host() -> None:
    with pytest.raises(InvalidRedirectUriError):
        RedirectUri("http://user:pass@127.0.0.1:54321/callback")


def test_rejects_a_fragment() -> None:
    with pytest.raises(InvalidRedirectUriError):
        RedirectUri("http://127.0.0.1:54321/callback#frag")


def test_rejects_custom_scheme() -> None:
    with pytest.raises(InvalidRedirectUriError):
        RedirectUri("myapp://callback")


def test_rejects_a_custom_scheme_towards_a_loopback_host() -> None:
    with pytest.raises(InvalidRedirectUriError):
        RedirectUri("myapp://127.0.0.1/callback")


def test_rejects_a_uri_without_authority() -> None:
    with pytest.raises(InvalidRedirectUriError):
        RedirectUri("/callback")


def test_rejects_a_scheme_relative_uri() -> None:
    with pytest.raises(InvalidRedirectUriError):
        RedirectUri("//localhost/callback")


def test_rejects_a_malformed_authority_without_leaking_a_value_error() -> None:
    """`urlsplit("http://[::1")` levanta `ValueError` (corchete IPv6 sin
    cerrar). Sin traducirlo, ese error cruzaria el dominio y acabaria en
    un 500 de `/authorize` o impidiendo arrancar `ads-api` desde la
    auditoria (revision de seguridad, 17-sep)."""
    with pytest.raises(InvalidRedirectUriError):
        RedirectUri("http://[::1")


@pytest.mark.parametrize("port", (":99999", ":-1", ":0", ":abc"))
def test_rejects_a_port_outside_the_valid_range(port: str) -> None:
    with pytest.raises(InvalidRedirectUriError):
        RedirectUri(f"http://127.0.0.1{port}/callback")


@pytest.mark.parametrize(
    "value",
    (
        "http://127.0.0.1:54321/callback\r\nSet-Cookie: sesion=robada",
        "http://localhost\t.evil.com/callback",
        "http://127.0.0.1:54321/call\x00back",
        "http://127.0.0.1:54321/callback\x7f",
    ),
)
def test_rejects_control_characters_that_urlsplit_would_swallow(value: str) -> None:
    """`urlsplit` BORRA CR, LF y TAB antes de parsear: sin esta
    comprobacion, `RedirectUri.value` conservaria unos caracteres que la
    validacion nunca vio (revision de seguridad, 17-sep)."""
    with pytest.raises(InvalidRedirectUriError):
        RedirectUri(value)


# ── comparacion (`matches`) ─────────────────────────────────────────────


def test_loopback_port_may_vary_but_host_and_path_may_not() -> None:
    registered = RedirectUri("http://127.0.0.1:54321/callback")

    assert registered.matches("http://127.0.0.1:9999/callback") is True


def test_exact_match_still_works() -> None:
    registered = RedirectUri("http://127.0.0.1:54321/callback")

    assert registered.matches("http://127.0.0.1:54321/callback") is True


def test_loopback_match_requires_the_same_host() -> None:
    registered = RedirectUri("http://127.0.0.1:54321/callback")

    assert registered.matches("http://localhost:54321/callback") is False


def test_loopback_match_requires_the_same_path() -> None:
    registered = RedirectUri("http://127.0.0.1:54321/callback")

    assert registered.matches("http://127.0.0.1:54321/other") is False


def test_https_loopback_requires_exact_equality_including_the_port() -> None:
    """La holgura de puerto de RFC 8252 §7.3 es para el `http` de bucle
    local del agente nativo; con TLS propio el puerto lo fija quien monta
    el certificado, asi que no se afloja."""
    registered = RedirectUri("https://127.0.0.1:8443/callback")

    assert registered.matches("https://127.0.0.1:8443/callback") is True
    assert registered.matches("https://127.0.0.1:9999/callback") is False


def test_a_non_matching_redirect_uri_never_matches() -> None:
    registered = RedirectUri("http://127.0.0.1:54321/callback")

    assert registered.matches("https://evil.example/callback") is False


def test_a_malformed_candidate_never_matches_and_never_raises() -> None:
    """`matches()` recibe cadenas CRUDAS: una autoridad rota no coincide,
    y sobre todo no levanta `ValueError` en mitad de `/authorize`."""
    registered = RedirectUri("http://127.0.0.1:54321/callback")

    assert registered.matches("http://[::1") is False


def test_loopback_match_rejects_a_candidate_with_a_fragment() -> None:
    """L10 de la revision de seguridad (16-sep): `matches()` recibe una
    cadena CRUDA, no necesariamente validada por `RedirectUri()` -- sin
    esta comprobacion, un candidato con fragmento colaba porque
    `_matches_ignoring_loopback_port` nunca miraba `fragment`."""
    registered = RedirectUri("http://127.0.0.1:54321/callback")

    assert registered.matches("http://127.0.0.1:9999/callback#evil-fragment") is False

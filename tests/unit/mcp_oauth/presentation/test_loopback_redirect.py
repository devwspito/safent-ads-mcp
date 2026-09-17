"""`LoopbackAwareClientInformation` (tasks.md T008, threat-model.md C-37 y
C-70 pieza 4): el `http` de bucle local casa con otro puerto; el `https`
de bucle local exige igualdad exacta; desde D-11 ningun host remoto llega
siquiera a compararse."""

from __future__ import annotations

import pytest
from mcp.shared.auth import InvalidRedirectUriError
from pydantic import AnyUrl, TypeAdapter

from safent_ads.mcp_oauth.presentation.loopback_client import LoopbackAwareClientInformation

_URL = TypeAdapter(AnyUrl)


def _client(*redirect_uris: str) -> LoopbackAwareClientInformation:
    return LoopbackAwareClientInformation(
        client_id="client-1",
        redirect_uris=[_URL.validate_python(uri) for uri in redirect_uris],
    )


def test_loopback_port_may_vary_but_host_and_path_may_not() -> None:
    client = _client("http://127.0.0.1:54321/callback")

    validated = client.validate_redirect_uri(_URL.validate_python("http://127.0.0.1:9999/callback"))

    assert str(validated) == "http://127.0.0.1:9999/callback"


def test_exact_match_still_works() -> None:
    client = _client("http://127.0.0.1:54321/callback")

    validated = client.validate_redirect_uri(_URL.validate_python("http://127.0.0.1:54321/callback"))

    assert str(validated) == "http://127.0.0.1:54321/callback"


def test_loopback_path_change_is_rejected() -> None:
    client = _client("http://127.0.0.1:54321/callback")

    with pytest.raises(InvalidRedirectUriError):
        client.validate_redirect_uri(_URL.validate_python("http://127.0.0.1:9999/other"))


def test_https_loopback_requires_exact_match_even_with_different_port() -> None:
    """La holgura de RFC 8252 §7.3 es del `http` de bucle local; con TLS
    propio el puerto lo fija quien monta el certificado."""
    client = _client("https://127.0.0.1:8443/callback")

    with pytest.raises(InvalidRedirectUriError):
        client.validate_redirect_uri(_URL.validate_python("https://127.0.0.1:9999/callback"))


def test_https_loopback_exact_match_is_accepted() -> None:
    client = _client("https://127.0.0.1:8443/callback")

    validated = client.validate_redirect_uri(
        _URL.validate_python("https://127.0.0.1:8443/callback")
    )

    assert str(validated) == "https://127.0.0.1:8443/callback"


@pytest.mark.parametrize(
    "candidate",
    (
        "http://evil.example:54321/callback",
        "https://evil.example/callback",
        "http://127.0.0.1.evil.com:54321/callback",
    ),
)
def test_remote_host_never_matches_regardless_of_scheme_or_port(candidate: str) -> None:
    """D-11: un candidato remoto no "no casa", es que ni siquiera es una
    `RedirectUri` valida -- se rechaza antes de compararlo con nada."""
    client = _client("http://127.0.0.1:54321/callback")

    with pytest.raises(InvalidRedirectUriError):
        client.validate_redirect_uri(_URL.validate_python(candidate))


def test_unregistered_uri_is_rejected() -> None:
    client = _client("http://127.0.0.1:54321/callback")

    with pytest.raises(InvalidRedirectUriError):
        client.validate_redirect_uri(_URL.validate_python("http://127.0.0.1:54321/elsewhere"))


def test_missing_redirect_uri_falls_back_to_the_sole_registered_one() -> None:
    client = _client("http://127.0.0.1:54321/callback")

    validated = client.validate_redirect_uri(None)

    assert str(validated) == "http://127.0.0.1:54321/callback"


def test_missing_redirect_uri_with_several_registered_is_rejected() -> None:
    client = _client("http://127.0.0.1:54321/callback", "http://127.0.0.1:11111/callback")

    with pytest.raises(InvalidRedirectUriError):
        client.validate_redirect_uri(None)


def test_candidate_with_a_fragment_is_rejected_even_with_matching_path_and_port() -> None:
    """L10 de la revision de seguridad (16-sep): el candidato tambien pasa
    por `RedirectUri()` -- las mismas reglas de forma que las registradas
    (sin fragmento, RFC 8252) se aplican a lo que el cliente PRESENTA, no
    solo a lo que quedo registrado."""
    client = _client("http://127.0.0.1:54321/callback")

    with pytest.raises(InvalidRedirectUriError):
        client.validate_redirect_uri(
            _URL.validate_python("http://127.0.0.1:9999/callback#evil-fragment")
        )


def test_candidate_with_userinfo_is_rejected_even_with_matching_host_and_path() -> None:
    client = _client("http://127.0.0.1:54321/callback")

    with pytest.raises(InvalidRedirectUriError):
        client.validate_redirect_uri(
            _URL.validate_python("http://user:pass@127.0.0.1:9999/callback")
        )

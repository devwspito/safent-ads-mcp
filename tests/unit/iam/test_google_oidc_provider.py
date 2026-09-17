"""`GoogleOidcProvider` (002b T023): SOLO transporte. Sin red -- el envio de
`httpx.AsyncHTTPTransport` se dobla y el resolver se inyecta, asi que el guard
de egreso real (lista blanca de host + IP fijada) se ejercita entero.

La validacion de claims se dobla a proposito: quien decide si unas claims
valen es `iam/application/federated_id_token.py` (T018, otro carril) y tiene
sus propios tests puros. Aqui solo se comprueba que el adaptador le entrega el
payload decodificado, la audiencia esperada, el nonce esperado y su reloj."""

from __future__ import annotations

import base64
import json
from collections.abc import Callable, Sequence
from datetime import UTC, datetime
from typing import Any

import httpx
import pytest

from safent_ads.iam.application.federated_id_token import FederatedIdTokenInvalidError
from safent_ads.iam.domain.federated_transaction import ReferenceHash
from safent_ads.iam.infrastructure import google_oidc_provider as provider_module
from safent_ads.iam.infrastructure.google_oidc_provider import (
    GoogleOidcConfig,
    GoogleOidcError,
    GoogleOidcProvider,
)
from safent_ads.shared.clock import FixedClock

_CLIENT_ID = "1234.apps.googleusercontent.com"
_CLIENT_SECRET = "GOCSPX-super-secreto"  # noqa: S105 - valor de prueba
_REDIRECT_URI = "https://ads.example.com/api/v1/auth/federated/callback"
_GOOGLE_IP = "142.250.185.10"
_NOW = datetime(2026, 9, 16, 10, 0, tzinfo=UTC)
_VALIDATED = object()
# JWT sintetico cuyo payload NO es JSON: base64 de "header"/"not-json"/"sig".
# Es el caso que la prueba necesita, no una credencial. La marca de
# `gitleaks` va en la linea del valor: es la unica que esa herramienta mira.
_UNDECODABLE_ID_TOKEN = "aGVhZGVy.bm90LWpzb24.c2ln"  # noqa: S105 gitleaks:allow


def _resolver_returning(*addresses: str) -> Callable[[str], Any]:
    async def _resolve(hostname: str) -> Sequence[str]:  # noqa: ARG001
        return list(addresses)

    return _resolve


def _stub_upstream(
    monkeypatch: pytest.MonkeyPatch,
    responder: Callable[[httpx.Request], httpx.Response],
) -> list[httpx.Request]:
    seen: list[httpx.Request] = []

    async def _handle(self: httpx.AsyncHTTPTransport, request: httpx.Request) -> httpx.Response:  # noqa: ARG001
        seen.append(request)
        return responder(request)

    monkeypatch.setattr(httpx.AsyncHTTPTransport, "handle_async_request", _handle)
    return seen


def _spy_on_validation(monkeypatch: pytest.MonkeyPatch) -> list[dict[str, Any]]:
    calls: list[dict[str, Any]] = []

    def _validate(
        claims: dict[str, Any],
        *,
        expected_audience: str,
        expected_nonce_hash: ReferenceHash,
        now: datetime,
    ) -> object:
        calls.append(
            {
                "claims": claims,
                "expected_audience": expected_audience,
                "expected_nonce_hash": expected_nonce_hash,
                "now": now,
            }
        )
        return _VALIDATED

    monkeypatch.setattr(provider_module, "validate_id_token_claims", _validate)
    return calls


def _failing_validation(
    monkeypatch: pytest.MonkeyPatch, error: FederatedIdTokenInvalidError
) -> None:
    def _validate(*_args: Any, **_kwargs: Any) -> object:
        raise error

    monkeypatch.setattr(provider_module, "validate_id_token_claims", _validate)


def _segment(payload: object) -> str:
    raw = json.dumps(payload).encode()
    return base64.urlsafe_b64encode(raw).decode().rstrip("=")


def _id_token(payload: object) -> str:
    return f"{_segment({'alg': 'RS256'})}.{_segment(payload)}.ZmFrZS1zaWc"


def _responding(
    body: object, *, status_code: int = 200
) -> Callable[[httpx.Request], httpx.Response]:
    return lambda _request: httpx.Response(status_code, json=body)


def _provider() -> GoogleOidcProvider:
    return GoogleOidcProvider(
        GoogleOidcConfig(client_id=_CLIENT_ID, client_secret=_CLIENT_SECRET),
        FixedClock(_NOW),
        resolver=_resolver_returning(_GOOGLE_IP),
    )


_EXPECTED_NONCE_HASH = ReferenceHash.of("nonce-esperado")


async def _exchange(provider: GoogleOidcProvider) -> object:
    return await provider.exchange_code(
        code="4/0Ab_code",
        redirect_uri=_REDIRECT_URI,
        expected_nonce_hash=_EXPECTED_NONCE_HASH,
    )


def test_authorization_url_asks_for_the_minimum_and_forces_the_account_choice() -> None:
    url = httpx.URL(
        _provider().authorization_url(state="st4te", nonce="n0nce", redirect_uri=_REDIRECT_URI)
    )

    assert str(url).startswith("https://accounts.google.com/o/oauth2/v2/auth?")
    assert url.params["scope"] == "openid email"
    assert url.params["prompt"] == "select_account"
    assert url.params["response_type"] == "code"
    assert url.params["client_id"] == _CLIENT_ID
    assert url.params["redirect_uri"] == _REDIRECT_URI
    assert url.params["state"] == "st4te"
    assert url.params["nonce"] == "n0nce"


def test_authorization_url_never_carries_the_client_secret() -> None:
    url = _provider().authorization_url(state="st4te", nonce="n0nce", redirect_uri=_REDIRECT_URI)

    assert _CLIENT_SECRET not in url


def test_config_never_reprs_the_client_secret() -> None:
    config = GoogleOidcConfig(client_id=_CLIENT_ID, client_secret=_CLIENT_SECRET)

    assert _CLIENT_SECRET not in repr(config)


async def test_exchange_code_posts_to_the_pinned_google_host_with_the_secret_in_the_body(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _spy_on_validation(monkeypatch)
    seen = _stub_upstream(monkeypatch, _responding({"id_token": _id_token({"sub": "108"})}))

    await _exchange(_provider())

    request = seen[0]
    assert request.method == "POST"
    assert request.url.host == _GOOGLE_IP
    assert request.url.path == "/token"
    assert request.headers["host"] == "oauth2.googleapis.com"
    assert request.extensions["sni_hostname"] == "oauth2.googleapis.com"
    assert not request.url.query
    body = request.content.decode()
    assert "grant_type=authorization_code" in body
    assert "client_secret=" in body


async def test_exchange_code_hands_the_decoded_payload_to_the_claim_validator(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls = _spy_on_validation(monkeypatch)
    claims = {"sub": "108", "email": "duena@example.com", "nonce": "nonce-esperado"}
    _stub_upstream(monkeypatch, _responding({"id_token": _id_token(claims)}))

    result = await _exchange(_provider())

    assert result is _VALIDATED
    assert calls == [
        {
            "claims": claims,
            "expected_audience": _CLIENT_ID,
            "expected_nonce_hash": _EXPECTED_NONCE_HASH,
            "now": _NOW,
        }
    ]


async def test_exchange_code_applies_a_ten_second_timeout(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """NFR-103: el tramo contra el proveedor tiene espera acotada."""
    _spy_on_validation(monkeypatch)
    seen = _stub_upstream(monkeypatch, _responding({"id_token": _id_token({"sub": "108"})}))

    await _exchange(_provider())

    assert seen[0].extensions["timeout"] == {
        "connect": 10.0,
        "pool": 10.0,
        "read": 10.0,
        "write": 10.0,
    }


async def test_exchange_code_denies_when_the_pinned_address_is_blocked(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """El guard compartido sigue puesto: un DNS que devuelva la metadata de
    la nube para el host de Google no llega a conectar."""
    seen = _stub_upstream(monkeypatch, _responding({"id_token": "x"}))
    blocked = GoogleOidcProvider(
        GoogleOidcConfig(client_id=_CLIENT_ID, client_secret=_CLIENT_SECRET),
        FixedClock(_NOW),
        resolver=_resolver_returning("::ffff:169.254.169.254"),
    )

    with pytest.raises(GoogleOidcError, match="egreso hacia Google bloqueado"):
        await _exchange(blocked)

    assert seen == []


async def test_exchange_code_reports_an_http_error_without_body_or_url(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _stub_upstream(
        monkeypatch,
        _responding({"error": "invalid_grant", "access_token": "ya29.leak"}, status_code=400),
    )

    with pytest.raises(GoogleOidcError) as raised:
        await _exchange(_provider())

    message = str(raised.value)
    assert message == "Google token endpoint HTTP 400"
    assert "ya29.leak" not in message
    assert raised.value.__cause__ is None


async def test_exchange_code_does_not_follow_a_redirect_from_google(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    seen = _stub_upstream(
        monkeypatch,
        lambda _request: httpx.Response(302, headers={"location": "https://attacker.test/"}),
    )

    with pytest.raises(GoogleOidcError, match="HTTP 302"):
        await _exchange(_provider())

    assert len(seen) == 1


async def test_exchange_code_reports_a_non_json_response_without_the_body(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _stub_upstream(
        monkeypatch, lambda _request: httpx.Response(200, content=b"<html>ya29.leak</html>")
    )

    with pytest.raises(GoogleOidcError) as raised:
        await _exchange(_provider())

    assert str(raised.value) == "respuesta de Google no es JSON valido"
    # `json.JSONDecodeError` guarda el cuerpo entero en `.doc`; sin
    # `raise ... from None` el encadenado implicito lo imprime en la traza.
    assert raised.value.__suppress_context__ is True


async def test_exchange_code_rejects_a_json_response_that_is_not_an_object(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _stub_upstream(monkeypatch, _responding(["no", "soy", "un", "objeto"]))

    with pytest.raises(GoogleOidcError, match="no es un objeto JSON"):
        await _exchange(_provider())


async def test_exchange_code_rejects_a_response_without_id_token(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _stub_upstream(monkeypatch, _responding({"access_token": "ya29.sin-id-token"}))

    with pytest.raises(GoogleOidcError, match="no devolvio id_token"):
        await _exchange(_provider())


async def test_exchange_code_rejects_a_malformed_id_token(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _stub_upstream(monkeypatch, _responding({"id_token": "solo.dos"}))

    with pytest.raises(GoogleOidcError, match="id_token mal formado"):
        await _exchange(_provider())


async def test_exchange_code_rejects_an_undecodable_payload(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _stub_upstream(monkeypatch, _responding({"id_token": _UNDECODABLE_ID_TOKEN}))

    with pytest.raises(GoogleOidcError) as raised:
        await _exchange(_provider())

    assert str(raised.value) == "payload del id_token invalido"
    assert raised.value.__suppress_context__ is True


async def test_exchange_code_rejects_a_payload_that_is_not_an_object(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _stub_upstream(monkeypatch, _responding({"id_token": _id_token(["lista"])}))

    with pytest.raises(GoogleOidcError, match="no es un objeto"):
        await _exchange(_provider())


async def test_exchange_code_reports_a_network_failure_without_the_url(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def _fail(_request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("All connection attempts failed")

    _stub_upstream(monkeypatch, _fail)

    with pytest.raises(GoogleOidcError) as raised:
        await _exchange(_provider())

    assert str(raised.value) == "fallo de red al hablar con Google"


async def test_exchange_code_translates_an_invalid_id_token_into_its_own_infrastructure_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """La presentacion solo conoce `GoogleOidcError` (mapeado a
    `federated_error=provider_unavailable`): el tipo interno de
    `federated_id_token.py` no debe escapar del adaptador."""
    _failing_validation(monkeypatch, FederatedIdTokenInvalidError("nonce del id_token no coincide"))
    _stub_upstream(monkeypatch, _responding({"id_token": _id_token({"sub": "108"})}))

    with pytest.raises(GoogleOidcError) as raised:
        await _exchange(_provider())

    assert str(raised.value) == "id_token de Google invalido"
    assert isinstance(raised.value.__cause__, FederatedIdTokenInvalidError)

"""`CloudflareHttpClient` contra un transporte HTTP falso (`httpx.MockTransport`,
sin red real): mismo criterio que `test_http_asset_fetcher.py` -- resolver
inyectable y determinista, cero DNS real. Barre exito, error de
autenticacion, host bloqueado (F-3/F-4) y tope de tamano; toda la seccion
final es la exigencia del encargo: ningun error puede filtrar el token ni el
cuerpo crudo de Cloudflare."""

from __future__ import annotations

import json

import httpx
import pytest

from safent_ads.integrations.cloudflare.client import (
    CloudflareAccountIdError,
    CloudflareApiError,
    CloudflareHttpClient,
    CloudflareResponseTooLargeError,
    CloudflareTransportError,
)

_FAKE_TOKEN = "sk-cloudflare-super-secret-token-do-not-leak"  # noqa: S105 - fixture, no real secret
_PUBLIC_IP = "104.16.132.229"
_ZONE_ID = "023e105f4ecef8ad9ca31a8372d0c353"
_ZONE_JSON = {"id": _ZONE_ID, "name": "example.com", "status": "active"}
_RECORD_JSON = {
    "id": "372e67954025e0ba6aaa6d586b9e0b59",
    "zone_id": _ZONE_ID,
    "type": "A",
    "name": "www.example.com",
    "content": "1.2.3.4",
    "ttl": 300,
    "proxied": False,
    "comment": None,
}


def _resolver_returning(*addresses: str):
    async def _resolve(hostname: str) -> list[str]:  # noqa: ARG001 - firma del resolver
        return list(addresses)

    return _resolve


def _envelope(result: object, *, success: bool = True) -> bytes:
    return json.dumps({"success": success, "errors": [], "messages": [], "result": result}).encode()


def _client(handler, **kwargs: object) -> CloudflareHttpClient:
    return CloudflareHttpClient(
        _FAKE_TOKEN,
        transport=httpx.MockTransport(handler),
        resolver=_resolver_returning(_PUBLIC_IP),
        **kwargs,
    )


async def test_list_zones_returns_the_unwrapped_result() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/client/v4/zones"
        assert request.headers["authorization"] == f"Bearer {_FAKE_TOKEN}"
        return httpx.Response(200, content=_envelope([_ZONE_JSON]))

    zones = await _client(handler).list_zones()

    assert zones == [_ZONE_JSON]


async def test_list_dns_records_sends_type_and_name_filters() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == f"/client/v4/zones/{_ZONE_ID}/dns_records"
        assert request.url.params["type"] == "A"
        assert request.url.params["name"] == "www.example.com"
        return httpx.Response(200, content=_envelope([_RECORD_JSON]))

    records = await _client(handler).list_dns_records(
        _ZONE_ID, record_type="A", name="www.example.com"
    )

    assert records == [_RECORD_JSON]


async def test_create_dns_record_posts_the_payload() -> None:
    sent_bodies: list[bytes] = []

    def handler(request: httpx.Request) -> httpx.Response:
        assert request.method == "POST"
        sent_bodies.append(request.content)
        return httpx.Response(200, content=_envelope(_RECORD_JSON))

    payload = {"type": "A", "name": "www.example.com", "content": "1.2.3.4", "ttl": 300}
    record = await _client(handler).create_dns_record(_ZONE_ID, payload)

    assert record == _RECORD_JSON
    assert json.loads(sent_bodies[0]) == payload


async def test_update_dns_record_puts_to_the_record_path() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.method == "PUT"
        assert request.url.path == f"/client/v4/zones/{_ZONE_ID}/dns_records/{_RECORD_JSON['id']}"
        return httpx.Response(200, content=_envelope(_RECORD_JSON))

    payload = {"type": "A", "name": "x", "content": "1.2.3.4", "ttl": 300}
    record = await _client(handler).update_dns_record(_ZONE_ID, str(_RECORD_JSON["id"]), payload)

    assert record == _RECORD_JSON


async def test_delete_dns_record_sends_delete() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.method == "DELETE"
        return httpx.Response(200, content=_envelope({"id": _RECORD_JSON["id"]}))

    result = await _client(handler).delete_dns_record(_ZONE_ID, str(_RECORD_JSON["id"]))

    assert result == {"id": _RECORD_JSON["id"]}


async def test_verify_user_token_hits_the_user_verify_path() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/client/v4/user/tokens/verify"
        return httpx.Response(200, content=_envelope({"id": "tok", "status": "active"}))

    result = await _client(handler).verify_user_token()

    assert result == {"id": "tok", "status": "active"}


async def test_verify_account_token_hits_the_account_scoped_verify_path() -> None:
    account_id = "a" * 32

    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == f"/client/v4/accounts/{account_id}/tokens/verify"
        return httpx.Response(200, content=_envelope({"id": "tok", "status": "active"}))

    result = await _client(handler).verify_account_token(account_id)

    assert result == {"id": "tok", "status": "active"}


async def test_verify_account_token_rejects_a_malformed_account_id_without_a_request() -> None:
    def handler(request: httpx.Request) -> httpx.Response:  # noqa: ARG001
        raise AssertionError("no deberia construir la peticion")

    with pytest.raises(CloudflareAccountIdError):
        await _client(handler).verify_account_token("../etc/passwd")


async def test_list_accounts_returns_the_unwrapped_result() -> None:
    accounts = [{"id": "a" * 32, "name": "Acme"}]

    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/client/v4/accounts"
        return httpx.Response(200, content=_envelope(accounts))

    result = await _client(handler).list_accounts()

    assert result == accounts


async def test_auth_error_raises_api_error_with_only_the_status_code() -> None:
    def handler(request: httpx.Request) -> httpx.Response:  # noqa: ARG001
        return httpx.Response(
            401,
            content=_envelope(None, success=False),
        )

    with pytest.raises(CloudflareApiError) as excinfo:
        await _client(handler).list_zones()

    assert excinfo.value.status_code == 401


async def test_success_status_with_success_false_still_raises_api_error() -> None:
    def handler(request: httpx.Request) -> httpx.Response:  # noqa: ARG001
        return httpx.Response(200, content=_envelope(None, success=False))

    with pytest.raises(CloudflareApiError) as excinfo:
        await _client(handler).list_zones()

    assert excinfo.value.status_code == 200


async def test_malformed_json_body_raises_transport_error() -> None:
    def handler(request: httpx.Request) -> httpx.Response:  # noqa: ARG001
        return httpx.Response(200, content=b"not json at all")

    with pytest.raises(CloudflareTransportError):
        await _client(handler).list_zones()


async def test_unexpected_result_shape_raises_transport_error() -> None:
    def handler(request: httpx.Request) -> httpx.Response:  # noqa: ARG001
        # `result` deberia ser una lista para `list_zones`; aqui es un dict.
        return httpx.Response(200, content=_envelope({"unexpected": "shape"}))

    with pytest.raises(CloudflareTransportError):
        await _client(handler).list_zones()


async def test_response_over_the_byte_cap_is_cut_and_rejected() -> None:
    big_result = [{"id": str(i), "name": "x", "status": "active"} for i in range(10_000)]

    def handler(request: httpx.Request) -> httpx.Response:  # noqa: ARG001
        return httpx.Response(200, content=_envelope(big_result))

    with pytest.raises(CloudflareResponseTooLargeError):
        await _client(handler, max_response_bytes=64).list_zones()


async def test_host_resolving_to_a_blocked_range_never_reaches_the_transport() -> None:
    def handler(request: httpx.Request) -> httpx.Response:  # noqa: ARG001
        raise AssertionError("no deberia llegar a hacer la peticion HTTP")

    client = CloudflareHttpClient(
        _FAKE_TOKEN,
        transport=httpx.MockTransport(handler),
        resolver=_resolver_returning("127.0.0.1"),
    )

    with pytest.raises(CloudflareTransportError):
        await client.list_zones()


# --- barrido de sanitizacion (encargo del dueno): ningun error puede llevar
# el token ni el cuerpo crudo del proveedor -----------------------------------

_POISONED_BODY = (
    f'{{"success": false, "errors": [{{"message": "leak {_FAKE_TOKEN} at '
    'https://internal.cloudflare.example/secret-path"}}]}}'
).encode()


@pytest.mark.parametrize(
    ("status_code", "body", "resolver_ip"),
    [
        (401, _POISONED_BODY, _PUBLIC_IP),
        (500, _POISONED_BODY, _PUBLIC_IP),
        (200, b"not json, but leaks " + _FAKE_TOKEN.encode(), _PUBLIC_IP),
        (200, _envelope([{"a": 1}] * 10_000), _PUBLIC_IP),
    ],
)
async def test_every_error_path_is_sanitised(
    status_code: int, body: bytes, resolver_ip: str
) -> None:
    def handler(request: httpx.Request) -> httpx.Response:  # noqa: ARG001
        return httpx.Response(status_code, content=body)

    client = CloudflareHttpClient(
        _FAKE_TOKEN,
        transport=httpx.MockTransport(handler),
        resolver=_resolver_returning(resolver_ip),
        max_response_bytes=256,
    )

    expected_errors = (
        CloudflareApiError,
        CloudflareTransportError,
        CloudflareResponseTooLargeError,
    )
    with pytest.raises(expected_errors) as excinfo:
        await client.list_zones()

    message = str(excinfo.value)
    assert _FAKE_TOKEN not in message
    assert "https://" not in message
    assert "http://" not in message


async def test_blocked_host_error_message_never_names_the_token() -> None:
    def handler(request: httpx.Request) -> httpx.Response:  # noqa: ARG001
        raise AssertionError("no deberia llegar a hacer la peticion HTTP")

    client = CloudflareHttpClient(
        _FAKE_TOKEN,
        transport=httpx.MockTransport(handler),
        resolver=_resolver_returning("10.0.0.5"),
    )

    with pytest.raises(CloudflareTransportError) as excinfo:
        await client.list_zones()

    assert _FAKE_TOKEN not in str(excinfo.value)

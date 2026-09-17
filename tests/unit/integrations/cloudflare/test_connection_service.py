"""`connection_service.py` (lane 006-cloudflare-ui, owner decision: "crear
una conexion con Cloudflare pidiendo el token e indicando el enlace donde
crearlo"): `GetCloudflareConnectionStatus`, `ConnectCloudflareToken`
(valida contra la API antes de guardar, descubre zonas y cuenta),
`DisconnectCloudflareToken` y `DynamicCloudflareService` (panel primero,
`CLOUDFLARE_API_TOKEN` de respaldo despues, en cada llamada)."""

from __future__ import annotations

import json
import uuid
from datetime import UTC, datetime

import httpx
import pytest

from safent_ads.integrations.cloudflare.client import CloudflareHttpClient
from safent_ads.integrations.cloudflare.connection_port import (
    CloudflareConnectionRecord,
    CloudflareTokenInvalidError,
)
from safent_ads.integrations.cloudflare.connection_service import (
    REQUIRED_PERMISSIONS,
    ConnectCloudflareToken,
    DisconnectCloudflareToken,
    DynamicCloudflareService,
    GetCloudflareConnectionStatus,
)
from safent_ads.integrations.cloudflare.port import CloudflareNotConfigured, CloudflareZone
from safent_ads.shared.clock import FixedClock

_NOW = datetime(2026, 9, 15, 12, 0, tzinfo=UTC)
_OWNER_ID = uuid.uuid4()
_TOKEN = "sk-cloudflare-super-secret-token-do-not-leak"  # noqa: S105 - fixture
_ACCOUNT_ID = "a" * 32
_ZONE_JSON = {"id": "zone-1", "name": "example.com", "status": "active"}
_PROFILE_TOKENS_URL = "https://dash.cloudflare.com/profile/api-tokens"


class _FakeStore:
    def __init__(self, record: CloudflareConnectionRecord | None = None) -> None:
        self.record = record
        self.saved: list[CloudflareConnectionRecord] = []
        self.deleted = False

    async def get(self) -> CloudflareConnectionRecord | None:
        return self.record

    async def save(self, record: CloudflareConnectionRecord) -> None:
        self.record = record
        self.saved.append(record)

    async def delete(self) -> None:
        self.record = None
        self.deleted = True


def _envelope(result: object, *, success: bool = True) -> bytes:
    return json.dumps({"success": success, "errors": [], "messages": [], "result": result}).encode()


def _client_factory(handler):  # noqa: ANN001, ANN202 - firma de fabrica de cliente de test
    def factory(token: str) -> CloudflareHttpClient:
        return CloudflareHttpClient(token, transport=httpx.MockTransport(handler))

    return factory


# --- GetCloudflareConnectionStatus -------------------------------------------


async def test_status_reports_the_stored_connection() -> None:
    record = CloudflareConnectionRecord(
        token=_TOKEN,
        account_id=_ACCOUNT_ID,
        zones=("example.com",),
        connected_at=_NOW,
        connected_by_owner_id=_OWNER_ID,
    )
    status = await GetCloudflareConnectionStatus(
        _FakeStore(record), fallback_token_configured=False
    ).execute()

    assert status.connected is True
    assert status.account_id == _ACCOUNT_ID
    assert status.zones == ("example.com",)
    assert status.connected_at == _NOW
    assert status.create_token_url == f"https://dash.cloudflare.com/{_ACCOUNT_ID}/api-tokens"
    assert status.required_permissions == REQUIRED_PERMISSIONS


async def test_status_falls_back_to_the_env_token_when_nothing_is_stored() -> None:
    status = await GetCloudflareConnectionStatus(
        _FakeStore(None), fallback_token_configured=True
    ).execute()

    assert status.connected is True
    assert status.account_id is None
    assert status.zones == ()
    assert status.create_token_url == _PROFILE_TOKENS_URL


async def test_status_disconnected_when_neither_stored_nor_fallback() -> None:
    status = await GetCloudflareConnectionStatus(
        _FakeStore(None), fallback_token_configured=False
    ).execute()

    assert status.connected is False
    assert status.create_token_url == _PROFILE_TOKENS_URL


# --- ConnectCloudflareToken ---------------------------------------------------


async def test_connect_with_account_id_verifies_the_account_scoped_endpoint_and_saves() -> None:
    requested_paths: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requested_paths.append(request.url.path)
        if request.url.path == f"/client/v4/accounts/{_ACCOUNT_ID}/tokens/verify":
            return httpx.Response(200, content=_envelope({"id": "tok", "status": "active"}))
        if request.url.path == "/client/v4/zones":
            return httpx.Response(200, content=_envelope([_ZONE_JSON]))
        raise AssertionError(f"peticion inesperada: {request.url.path}")

    store = _FakeStore()
    use_case = ConnectCloudflareToken(
        store, clock=FixedClock(_NOW), client_factory=_client_factory(handler)
    )

    status = await use_case.execute(token=_TOKEN, account_id=_ACCOUNT_ID, owner_id=_OWNER_ID)

    assert f"/client/v4/accounts/{_ACCOUNT_ID}/tokens/verify" in requested_paths
    # `account_id` ya lo dio el propietario: nunca hace falta descubrirlo.
    assert "/client/v4/accounts" not in requested_paths
    assert status.connected is True
    assert status.account_id == _ACCOUNT_ID
    assert status.zones == ("example.com",)
    assert store.record is not None
    assert store.record.token == _TOKEN
    assert store.record.connected_by_owner_id == _OWNER_ID


async def test_connect_without_account_id_discovers_the_account() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/client/v4/user/tokens/verify":
            return httpx.Response(200, content=_envelope({"id": "tok", "status": "active"}))
        if request.url.path == "/client/v4/zones":
            return httpx.Response(200, content=_envelope([_ZONE_JSON]))
        if request.url.path == "/client/v4/accounts":
            return httpx.Response(200, content=_envelope([{"id": _ACCOUNT_ID, "name": "Acme"}]))
        raise AssertionError(f"peticion inesperada: {request.url.path}")

    store = _FakeStore()
    use_case = ConnectCloudflareToken(
        store, clock=FixedClock(_NOW), client_factory=_client_factory(handler)
    )

    status = await use_case.execute(token=_TOKEN, account_id=None, owner_id=_OWNER_ID)

    assert status.account_id == _ACCOUNT_ID
    assert status.create_token_url == f"https://dash.cloudflare.com/{_ACCOUNT_ID}/api-tokens"


async def test_connect_account_discovery_is_best_effort_and_never_blocks_the_connection() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/client/v4/user/tokens/verify":
            return httpx.Response(200, content=_envelope({"id": "tok", "status": "active"}))
        if request.url.path == "/client/v4/zones":
            return httpx.Response(200, content=_envelope([_ZONE_JSON]))
        if request.url.path == "/client/v4/accounts":
            return httpx.Response(403, content=_envelope(None, success=False))
        raise AssertionError(f"peticion inesperada: {request.url.path}")

    store = _FakeStore()
    use_case = ConnectCloudflareToken(
        store, clock=FixedClock(_NOW), client_factory=_client_factory(handler)
    )

    status = await use_case.execute(token=_TOKEN, account_id=None, owner_id=_OWNER_ID)

    assert status.connected is True
    assert status.account_id is None
    assert status.create_token_url == _PROFILE_TOKENS_URL


async def test_connect_with_multiple_visible_accounts_leaves_account_id_unresolved() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/client/v4/user/tokens/verify":
            return httpx.Response(200, content=_envelope({"id": "tok", "status": "active"}))
        if request.url.path == "/client/v4/zones":
            return httpx.Response(200, content=_envelope([_ZONE_JSON]))
        if request.url.path == "/client/v4/accounts":
            accounts = [{"id": "a" * 32, "name": "One"}, {"id": "b" * 32, "name": "Two"}]
            return httpx.Response(200, content=_envelope(accounts))
        raise AssertionError(f"peticion inesperada: {request.url.path}")

    status = await ConnectCloudflareToken(
        _FakeStore(), clock=FixedClock(_NOW), client_factory=_client_factory(handler)
    ).execute(token=_TOKEN, account_id=None, owner_id=_OWNER_ID)

    assert status.account_id is None


async def test_connect_rejects_a_malformed_discovered_account_id() -> None:
    """Hallazgo info (revision de seguridad de la conexion Cloudflare,
    2026-09-15): `_discover_account_id` guardaba el `id` de `GET
    /accounts` (respuesta de Cloudflare, no del propietario) sin
    validarlo -- fail closed, mismo patron que `rest.py`/`client.py`
    validan cuando el propietario TECLEA el `account_id`."""

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/client/v4/user/tokens/verify":
            return httpx.Response(200, content=_envelope({"id": "tok", "status": "active"}))
        if request.url.path == "/client/v4/zones":
            return httpx.Response(200, content=_envelope([_ZONE_JSON]))
        if request.url.path == "/client/v4/accounts":
            return httpx.Response(
                200, content=_envelope([{"id": "not-a-valid-account-id", "name": "Acme"}])
            )
        raise AssertionError(f"peticion inesperada: {request.url.path}")

    store = _FakeStore()
    status = await ConnectCloudflareToken(
        store, clock=FixedClock(_NOW), client_factory=_client_factory(handler)
    ).execute(token=_TOKEN, account_id=None, owner_id=_OWNER_ID)

    assert status.account_id is None
    assert status.create_token_url == _PROFILE_TOKENS_URL
    assert store.record is not None
    assert store.record.account_id is None


async def test_connect_rejects_an_invalid_token_and_never_saves_it() -> None:
    def handler(request: httpx.Request) -> httpx.Response:  # noqa: ARG001
        return httpx.Response(401, content=_envelope(None, success=False))

    store = _FakeStore()
    use_case = ConnectCloudflareToken(
        store, clock=FixedClock(_NOW), client_factory=_client_factory(handler)
    )

    with pytest.raises(CloudflareTokenInvalidError):
        await use_case.execute(token=_TOKEN, account_id=None, owner_id=_OWNER_ID)

    assert store.saved == []


async def test_connect_without_account_id_accepts_a_modern_account_token() -> None:
    """Hallazgo produccion 16-sep: Cloudflare emite ahora tokens de CUENTA
    (`cfat_...`) que `GET /user/tokens/verify` rechaza con `{code: 1000}`
    aunque `GET /accounts/{id}/tokens/verify` responda activo y `GET
    /zones` liste zonas de esa cuenta -- el propietario que pega uno de
    estos tokens SIN teclear el id de cuenta ya no debe caer en
    `CLOUDFLARE_TOKEN_INVALID`."""
    requested_paths: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requested_paths.append(request.url.path)
        if request.url.path == "/client/v4/user/tokens/verify":
            # Cloudflare responde `{code: 1000, message: "Invalid API Token"}`
            # para un token de CUENTA -- `CloudflareApiError` solo propaga el
            # `status_code` (nunca el cuerpo), asi que el test fija ese estado.
            return httpx.Response(400, content=_envelope(None, success=False))
        if request.url.path == "/client/v4/zones":
            zone = {**_ZONE_JSON, "account": {"id": _ACCOUNT_ID, "name": "Acme"}}
            return httpx.Response(200, content=_envelope([zone]))
        if request.url.path == f"/client/v4/accounts/{_ACCOUNT_ID}/tokens/verify":
            return httpx.Response(200, content=_envelope({"id": "tok", "status": "active"}))
        raise AssertionError(f"peticion inesperada: {request.url.path}")

    store = _FakeStore()
    use_case = ConnectCloudflareToken(
        store, clock=FixedClock(_NOW), client_factory=_client_factory(handler)
    )

    status = await use_case.execute(token=_TOKEN, account_id=None, owner_id=_OWNER_ID)

    assert requested_paths[0] == "/client/v4/user/tokens/verify"
    assert f"/client/v4/accounts/{_ACCOUNT_ID}/tokens/verify" in requested_paths
    # El id de cuenta se deriva de las zonas -- nunca hace falta `GET /accounts`.
    assert "/client/v4/accounts" not in requested_paths
    assert status.connected is True
    assert status.account_id == _ACCOUNT_ID
    assert status.zones == ("example.com",)
    assert store.record is not None
    assert store.record.account_id == _ACCOUNT_ID


async def test_connect_rejects_an_account_token_whose_zones_call_fails() -> None:
    """Fail closed: si el token de usuario no verifica y `GET /zones`
    tampoco responde, nunca se acepta la conexion."""

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/client/v4/user/tokens/verify":
            return httpx.Response(400, content=_envelope(None, success=False))
        if request.url.path == "/client/v4/zones":
            return httpx.Response(403, content=_envelope(None, success=False))
        raise AssertionError(f"peticion inesperada: {request.url.path}")

    store = _FakeStore()
    use_case = ConnectCloudflareToken(
        store, clock=FixedClock(_NOW), client_factory=_client_factory(handler)
    )

    with pytest.raises(CloudflareTokenInvalidError):
        await use_case.execute(token=_TOKEN, account_id=None, owner_id=_OWNER_ID)

    assert store.saved == []


async def test_connect_rejects_an_account_token_with_a_malformed_derived_account_id() -> None:
    """El `account.id` de una zona viene de la RESPUESTA de Cloudflare, no
    del propietario -- fail closed si no tiene la forma hexadecimal de 32
    caracteres, mismo criterio que `_discover_account_id`."""

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/client/v4/user/tokens/verify":
            return httpx.Response(400, content=_envelope(None, success=False))
        if request.url.path == "/client/v4/zones":
            zone = {**_ZONE_JSON, "account": {"id": "not-a-valid-account-id", "name": "Acme"}}
            return httpx.Response(200, content=_envelope([zone]))
        raise AssertionError(f"peticion inesperada: {request.url.path}")

    store = _FakeStore()
    use_case = ConnectCloudflareToken(
        store, clock=FixedClock(_NOW), client_factory=_client_factory(handler)
    )

    with pytest.raises(CloudflareTokenInvalidError):
        await use_case.execute(token=_TOKEN, account_id=None, owner_id=_OWNER_ID)

    assert store.saved == []


async def test_connect_rejects_a_token_that_cannot_read_zones() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/client/v4/user/tokens/verify":
            return httpx.Response(200, content=_envelope({"id": "tok", "status": "active"}))
        if request.url.path == "/client/v4/zones":
            return httpx.Response(403, content=_envelope(None, success=False))
        raise AssertionError(f"peticion inesperada: {request.url.path}")

    store = _FakeStore()
    use_case = ConnectCloudflareToken(
        store, clock=FixedClock(_NOW), client_factory=_client_factory(handler)
    )

    with pytest.raises(CloudflareTokenInvalidError):
        await use_case.execute(token=_TOKEN, account_id=None, owner_id=_OWNER_ID)

    assert store.saved == []


# --- DisconnectCloudflareToken -------------------------------------------


async def test_disconnect_deletes_the_stored_connection() -> None:
    store = _FakeStore(
        CloudflareConnectionRecord(
            token=_TOKEN, account_id=None, zones=(), connected_at=_NOW, connected_by_owner_id=None
        )
    )

    await DisconnectCloudflareToken(store).execute()

    assert store.deleted is True
    assert store.record is None


# --- DynamicCloudflareService --------------------------------------------


async def test_dynamic_service_prefers_the_stored_token_over_the_fallback() -> None:
    seen_tokens: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen_tokens.append(request.headers["authorization"])
        return httpx.Response(200, content=_envelope([_ZONE_JSON]))

    store = _FakeStore(
        CloudflareConnectionRecord(
            token="stored-token",
            account_id=None,
            zones=("example.com",),
            connected_at=_NOW,
            connected_by_owner_id=None,
        )
    )
    service = DynamicCloudflareService(
        store=store,
        fallback_token="env-fallback-token",
        allowed_zones=frozenset(),
        client_factory=_client_factory(handler),
    )

    zones = await service.list_dns_zones()

    assert zones == (CloudflareZone(zone_id="zone-1", name="example.com", status="active"),)
    assert seen_tokens == ["Bearer stored-token"]


async def test_dynamic_service_falls_back_to_the_env_token_when_nothing_is_stored() -> None:
    seen_tokens: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen_tokens.append(request.headers["authorization"])
        return httpx.Response(200, content=_envelope([_ZONE_JSON]))

    service = DynamicCloudflareService(
        store=_FakeStore(None),
        fallback_token="env-fallback-token",
        allowed_zones=frozenset(),
        client_factory=_client_factory(handler),
    )

    await service.list_dns_zones()

    assert seen_tokens == ["Bearer env-fallback-token"]


async def test_dynamic_service_returns_not_configured_with_the_profile_link() -> None:
    def handler(request: httpx.Request) -> httpx.Response:  # noqa: ARG001
        raise AssertionError("no deberia tocar la red sin token")

    service = DynamicCloudflareService(
        store=_FakeStore(None),
        fallback_token=None,
        allowed_zones=frozenset(),
        client_factory=_client_factory(handler),
    )

    result = await service.list_dns_zones()

    assert isinstance(result, CloudflareNotConfigured)
    assert result.create_token_url == _PROFILE_TOKENS_URL

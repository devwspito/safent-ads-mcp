"""`serve` (T027): lecturas por socket Unix real en un directorio temporal;
`execute_write` con esquema valido llega tipado al adaptador
(test_execute_write_over_socket_reaches_the_adapter); un payload
incompleto o cualquier `op` desconocida siguen `DENIED`
(test_execute_write_is_denied, test_unknown_op_is_denied)."""

from __future__ import annotations

import asyncio
import base64
import json
import os
import tempfile
from collections.abc import AsyncIterator, Mapping, Sequence
from dataclasses import replace
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path

import pytest
import structlog.testing

from safent_ads.accounts.application.ports import (
    AccountRef,
    AdEntitySnapshot,
    AdsPlatformPort,
    AssetUploadRequest,
    EntityStateSnapshot,
    IdempotencyKey,
    MetricFactSnapshot,
    MetricsRequest,
    PlatformAssetHandle,
    SignedAuthorization,
    WriteIntent,
    WriteOutcome,
)
from safent_ads.accounts.domain.ad_entity import AdEntityStatus
from safent_ads.accounts.domain.learning_state import LearningState
from safent_ads.broker.application.app_credentials_service import AppCredentialsService
from safent_ads.broker.application.oauth_connect_flow import OAuthConnectFlow
from safent_ads.broker.application.render_image import RenderImageService
from safent_ads.broker.infrastructure.adapter_registry import PlatformAdapterRegistry
from safent_ads.broker.infrastructure.credential_store import EncryptedCredentialStore
from safent_ads.broker.platforms.google_oauth_adapter import (
    GoogleOAuthAdapter,
    GoogleOAuthAdapterConfig,
)
from safent_ads.broker.platforms.meta_ad_library import map_ads_archive_row
from safent_ads.broker.platforms.meta_oauth_adapter import MetaOAuthAdapter, MetaOAuthAdapterConfig
from safent_ads.broker.presentation import socket_server
from safent_ads.broker.presentation.dispatcher import BrokerRuntime
from safent_ads.broker.presentation.socket_server import (
    _DEFAULT_MAX_FRAME_BYTES,
    _MAX_EXTENDED_FRAME_BYTES,
    _max_response_bytes_for,
    _op_from_raw,
    serve,
)
from safent_ads.broker.presentation.wire_protocol import FrameClient, FrameTooLargeError
from safent_ads.creative.domain.enums import MediaKind, RendererName
from safent_ads.creative.domain.identifiers import AssetId
from safent_ads.creative.domain.money import Money
from safent_ads.creative.domain.render_specs import ImageSpec, RenderedAsset
from safent_ads.creative.infrastructure.in_memory_asset_store import InMemoryAssetStore
from safent_ads.shared.clock import FixedClock
from safent_ads.shared.ids import EntityLevel, EntityRef, PlatformCode

_NOW = datetime(2026, 9, 9, tzinfo=UTC)
_ACCOUNT_REF = AccountRef(PlatformCode.GOOGLE, "123-456-7890")
_CAMPAIGN_REF = EntityRef(PlatformCode.GOOGLE, EntityLevel.CAMPAIGN, "campaign-1")
_ACCOUNT_PARENT_REF = EntityRef(PlatformCode.GOOGLE, EntityLevel.ACCOUNT, "123-456-7890")
_KEY_B64 = base64.b64encode(b"0" * 32).decode()
_PNG_MAGIC = b"\x89PNG\r\n\x1a\n"
_BRAND_KIT_FIELDS = {
    "primary_font": "Inter",
    "secondary_font": "Inter",
    "primary_color_hex": "#112233",
    "secondary_color_hex": "#FFFFFF",
    "logo_asset_id": str(AssetId.new()),
    "safe_area_top": 0.1,
    "safe_area_bottom": 0.1,
    "safe_area_left": 0.05,
    "safe_area_right": 0.05,
}


class _UnreachableHttpClient:
    """Estos tests nunca ejercitan un `op` de OAuth: si algo llegara a
    llamar al cliente HTTP, es un fallo del test, no un doble legitimo."""

    async def post_form(self, url: str, *, data: dict[str, str]) -> dict[str, object]:  # noqa: ARG002
        raise AssertionError

    async def get_json(
        self, url: str, *, headers: object = None, params: object = None  # noqa: ARG002
    ) -> dict[str, object]:
        raise AssertionError

    async def post_json(
        self, url: str, *, json_body: object, headers: object = None  # noqa: ARG002
    ) -> dict[str, object]:
        raise AssertionError


def _runtime(registry: PlatformAdapterRegistry, store_dir: Path) -> BrokerRuntime:
    clock = FixedClock(_NOW)
    store = EncryptedCredentialStore(store_dir, _KEY_B64)
    google = GoogleOAuthAdapter(
        GoogleOAuthAdapterConfig(client_id="c", client_secret="s"),
        _UnreachableHttpClient(),  # type: ignore[arg-type]
        clock,
    )
    meta = MetaOAuthAdapter(
        MetaOAuthAdapterConfig(app_id="a", app_secret="s"), _UnreachableHttpClient(), clock  # type: ignore[arg-type]
    )
    return BrokerRuntime(
        adapters=registry,
        oauth_flow=OAuthConnectFlow(store, google, meta, clock),
        app_credentials=AppCredentialsService(store, clock),
    )


class _StubAdsPlatformPort(AdsPlatformPort):
    def __init__(self) -> None:
        self.run_gaql_calls: list[tuple[AccountRef, str, int]] = []
        self.write_calls: list[tuple[WriteIntent, SignedAuthorization, IdempotencyKey]] = []
        self.upload_asset_calls: list[AssetUploadRequest] = []

    async def fetch_account_inventory(
        self, account_ref: AccountRef  # noqa: ARG002 - forma exacta del puerto
    ) -> Sequence[AdEntitySnapshot]:
        return [
            AdEntitySnapshot(
                entity_ref=_CAMPAIGN_REF,
                parent_ref=_ACCOUNT_PARENT_REF,
                name="Campana Otoño",
                status=AdEntityStatus.ACTIVE,
                is_controllable=True,
                learning_state=LearningState.NOT_APPLICABLE,
                budget=None,
                bid_target=None,
                shared_budget_ref=None,
                canonical_state={"status": "ACTIVE"},
                fetched_at=_NOW,
            )
        ]

    async def fetch_metrics(
        self, request: MetricsRequest  # noqa: ARG002 - forma exacta del puerto
    ) -> Sequence[MetricFactSnapshot]:
        return []

    async def read_entity_state(self, entity_ref: EntityRef) -> EntityStateSnapshot:
        return EntityStateSnapshot(
            entity_ref=entity_ref,
            status=AdEntityStatus.ACTIVE,
            is_controllable=True,
            canonical_state={"status": "ACTIVE"},
            fetched_at=_NOW,
        )

    async def upload_asset(self, request: AssetUploadRequest) -> PlatformAssetHandle:
        self.upload_asset_calls.append(request)
        return PlatformAssetHandle(
            platform_asset_id="asset-1", preview_url="https://cdn.example/asset-1"
        )

    async def run_gaql(
        self, account_ref: AccountRef, query: str, *, max_rows: int
    ) -> Sequence[Mapping[str, object]]:
        self.run_gaql_calls.append((account_ref, query, max_rows))
        rows = [{"campaign.id": str(i)} for i in range(5)]
        return rows[:max_rows]

    async def execute_write(
        self,
        intent: WriteIntent,
        authorization: SignedAuthorization,
        idempotency_key: IdempotencyKey,
    ) -> WriteOutcome:
        self.write_calls.append((intent, authorization, idempotency_key))
        return WriteOutcome(
            outcome="SUCCEEDED",
            applied_value=1,
            state_hash_after="a" * 64,
            error_code=None,
            platform_request_id="req-1",
        )


async def _connect_and_exchange(socket_path: Path, request: dict[str, object]) -> dict[str, object]:
    reader, writer = await asyncio.open_unix_connection(str(socket_path))
    client = FrameClient(reader, writer, max_frame_bytes=64 * 1024)
    await client.write_frame(json.dumps(request).encode("utf-8"))
    raw_response = await client.read_frame()
    client.close()
    await client.wait_closed()
    response: dict[str, object] = json.loads(raw_response)
    return response


@pytest.fixture
async def running_server(tmp_path: Path) -> AsyncIterator[Path]:
    socket_path = tmp_path / "broker.sock"
    registry = PlatformAdapterRegistry({PlatformCode.GOOGLE: _StubAdsPlatformPort()})
    runtime = _runtime(registry, tmp_path / "credentials")
    server = await serve(socket_path, runtime, frozenset({os.getuid()}))
    yield socket_path
    server.close()
    await server.wait_closed()


async def test_read_ops_over_socket(running_server: Path) -> None:
    response = await _connect_and_exchange(
        running_server,
        {
            "op": "fetch_account_inventory",
            "platform": "google",
            "external_account_id": "123-456-7890",
        },
    )

    assert response["ok"] is True
    result = response["result"]
    assert isinstance(result, list)
    assert result[0]["entity_ref"] == "google:campaign:campaign-1"


async def test_read_entity_state_over_socket(running_server: Path) -> None:
    response = await _connect_and_exchange(
        running_server,
        {"op": "read_entity_state", "entity_ref": "google:campaign:campaign-1"},
    )

    assert response["ok"] is True
    assert response["result"]["status"] == "active"  # type: ignore[index]


async def test_run_gaql_over_socket_happy_path(running_server: Path) -> None:
    response = await _connect_and_exchange(
        running_server,
        {
            "op": "run_gaql",
            "platform": "google",
            "external_account_id": "123-456-7890",
            "query": "SELECT campaign.id FROM campaign",
            "max_rows": 3,
        },
    )

    assert response["ok"] is True
    result = response["result"]
    assert result["row_count"] == 3  # type: ignore[index]
    assert result["rows"][0]["campaign.id"] == "0"  # type: ignore[index]


async def test_run_gaql_over_socket_rejects_mutate_without_touching_the_adapter() -> None:
    async def scenario(socket_path: Path) -> tuple[dict[str, object], _StubAdsPlatformPort]:
        stub = _StubAdsPlatformPort()
        registry = PlatformAdapterRegistry({PlatformCode.GOOGLE: stub})
        runtime = _runtime(registry, socket_path.parent / "credentials")
        server = await serve(socket_path, runtime, frozenset({os.getuid()}))
        try:
            response = await _connect_and_exchange(
                socket_path,
                {
                    "op": "run_gaql",
                    "platform": "google",
                    "external_account_id": "123-456-7890",
                    "query": "UPDATE campaign SET status = 'PAUSED'",
                    "max_rows": 10,
                },
            )
            return response, stub
        finally:
            server.close()
            await server.wait_closed()

    with tempfile.TemporaryDirectory() as tmp_dir:
        response, stub = await scenario(Path(tmp_dir) / "broker.sock")

    assert response["ok"] is False
    assert response["error_code"] == "GAQL_VALIDATION_FAILED"
    assert stub.run_gaql_calls == []


async def test_run_gaql_over_socket_rejects_max_rows_over_the_ceiling(
    running_server: Path,
) -> None:
    response = await _connect_and_exchange(
        running_server,
        {
            "op": "run_gaql",
            "platform": "google",
            "external_account_id": "123-456-7890",
            "query": "SELECT campaign.id FROM campaign",
            "max_rows": 999_999,
        },
    )

    assert response["ok"] is False
    assert response["error_code"] == "DENIED"


async def test_execute_write_is_denied() -> None:
    async def scenario(socket_path: Path) -> dict[str, object]:
        registry = PlatformAdapterRegistry({PlatformCode.GOOGLE: _StubAdsPlatformPort()})
        runtime = _runtime(registry, socket_path.parent / "credentials")
        server = await serve(socket_path, runtime, frozenset({os.getuid()}))
        try:
            return await _connect_and_exchange(
                socket_path,
                {
                    "op": "execute_write",
                    "entity_ref": "google:campaign:campaign-1",
                    "operation": "PAUSE",
                },
            )
        finally:
            server.close()
            await server.wait_closed()

    with tempfile.TemporaryDirectory() as tmp_dir:
        response = await scenario(Path(tmp_dir) / "broker.sock")

    assert response["ok"] is False
    assert response["error_code"] == "DENIED"


async def test_execute_write_over_socket_reaches_the_adapter(running_server: Path) -> None:
    response = await _connect_and_exchange(
        running_server,
        {
            "op": "execute_write",
            "entity_ref": "google:campaign:campaign-1",
            "operation": "PAUSE",
            "parametro": "status",
            "valor_actual": "ACTIVE",
            "valor_propuesto": "PAUSED",
            "diff_hash": "a" * 64,
            "expected_state_hash": "b" * 64,
            "authorization": {
                "authorization_id": "auth-1",
                "proposal_id": "proposal-1",
                "kind": "human_approval",
                "diff_hash": "a" * 64,
                "guardrail_verdict_hash": "c" * 64,
                "issued_by": "owner-1",
                "expires_at": _NOW.isoformat(),
                "signature": "deadbeef",
            },
            "idempotency_key": "key-1",
        },
    )

    assert response["ok"] is True
    result = response["result"]
    assert result["outcome"] == "SUCCEEDED"
    assert result["state_hash_after"] == "a" * 64


async def test_execute_write_threads_package_binding_and_approval_to_the_adapter() -> None:
    """003-paquete-de-campana contracts/api.md R2.E: `_handle_execute_write`
    no leia `package_binding`/`package_approval` del sobre --
    `WriteIntent.package_binding` y `SignedAuthorization.package_approval`
    llegaban SIEMPRE en `None` al adaptador sin importar lo que mandara el
    cliente, y `admit_package_step` (R1) denegaba todo `package_step` con
    `PACKAGE_BINDING_REQUIRED`. Este doble (`_StubAdsPlatformPort`) no
    ejecuta esa regla -- solo prueba que el sobre llega intacto hasta el
    adaptador; `tests/unit/broker/domain/test_package_admission.py` prueba
    la regla en si."""

    async def scenario(socket_path: Path) -> tuple[dict[str, object], _StubAdsPlatformPort]:
        stub = _StubAdsPlatformPort()
        registry = PlatformAdapterRegistry({PlatformCode.GOOGLE: stub})
        runtime = _runtime(registry, socket_path.parent / "credentials")
        server = await serve(socket_path, runtime, frozenset({os.getuid()}))
        try:
            response = await _connect_and_exchange(
                socket_path,
                {
                    "op": "execute_write",
                    "entity_ref": "google:campaign:campaign-1",
                    "operation": "PAUSE",
                    "parametro": "status",
                    "valor_actual": "ACTIVE",
                    "valor_propuesto": "PAUSED",
                    "diff_hash": "a" * 64,
                    "expected_state_hash": "b" * 64,
                    "package_binding": {"step_kind": "UPLOAD_CREATIVE", "local_ref": "step-1"},
                    "authorization": {
                        "authorization_id": "auth-1",
                        "proposal_id": "proposal-1",
                        "kind": "package_step",
                        "diff_hash": "a" * 64,
                        "guardrail_verdict_hash": "c" * 64,
                        "issued_by": "owner-1",
                        "expires_at": _NOW.isoformat(),
                        "signature": "deadbeef",
                        "package_approval": {"envelope": {"foo": "bar"}},
                    },
                    "idempotency_key": "key-1",
                },
            )
            return response, stub
        finally:
            server.close()
            await server.wait_closed()

    with tempfile.TemporaryDirectory() as tmp_dir:
        response, stub = await scenario(Path(tmp_dir) / "broker.sock")

    assert response["ok"] is True
    assert len(stub.write_calls) == 1
    intent, authorization, _ = stub.write_calls[0]
    assert intent.package_binding == {"step_kind": "UPLOAD_CREATIVE", "local_ref": "step-1"}
    assert authorization.kind == "package_step"
    assert authorization.package_approval == {"envelope": {"foo": "bar"}}


async def test_unauthorized_peer_gets_connection_closed(tmp_path: Path) -> None:
    socket_path = tmp_path / "broker.sock"
    registry = PlatformAdapterRegistry({PlatformCode.GOOGLE: _StubAdsPlatformPort()})
    runtime = _runtime(registry, tmp_path / "credentials")
    server = await serve(socket_path, runtime, frozenset())  # nadie autorizado
    try:
        reader, writer = await asyncio.open_unix_connection(str(socket_path))
        client = FrameClient(reader, writer, max_frame_bytes=64 * 1024)
        await client.write_frame(b'{"op": "fetch_account_inventory"}')

        with pytest.raises((asyncio.IncompleteReadError, ConnectionError)):
            await client.read_frame()
    finally:
        server.close()
        await server.wait_closed()


async def test_own_healthcheck_self_connection_is_rejected_at_debug_not_warning(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The broker's own healthcheck connects to its own socket every ~15s
    (`peer_uid == os.getuid()`, never in `allowed_uids`) -- expected,
    benign noise every 15s, not an intrusion attempt. A real unauthorized
    peer (any other uid) still logs at WARNING (test above)."""
    socket_path = tmp_path / "broker.sock"
    registry = PlatformAdapterRegistry({PlatformCode.GOOGLE: _StubAdsPlatformPort()})
    runtime = _runtime(registry, tmp_path / "credentials")
    server = await serve(socket_path, runtime, frozenset())  # nadie autorizado, ni nosotros mismos
    # `CapturingLogger` registra la llamada tal cual (`debug`/`warning`), sin
    # pasar por la configuracion global de structlog: con la suite completa
    # otro test deja un `make_filtering_bound_logger(INFO)` y `capture_logs`
    # nunca ve un DEBUG (asi fallo en el CI y no en local).
    capturing = structlog.testing.CapturingLogger()
    monkeypatch.setattr(socket_server, "logger", capturing)
    try:
        reader, writer = await asyncio.open_unix_connection(str(socket_path))
        client = FrameClient(reader, writer, max_frame_bytes=64 * 1024)
        await client.write_frame(b'{"op": "fetch_account_inventory"}')

        with pytest.raises((asyncio.IncompleteReadError, ConnectionError)):
            await client.read_frame()
    finally:
        server.close()
        await server.wait_closed()

    assert len(capturing.calls) == 1
    call = capturing.calls[0]
    assert call.method_name == "debug"
    assert call.args == ("broker_peer_rejected",)
    assert call.kwargs["peer_uid"] == os.getuid()


async def test_serve_uses_the_injected_self_uid_not_an_inline_os_getuid_call(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Code review nit (PR 36): `self_uid` is an explicit parameter threaded
    through `serve()` -> `_handle_connection` (composition passes
    `os.getuid()`), not an inline `os.getuid()` call inside the connection
    handler -- injecting a `self_uid` that can never match the real
    connecting peer proves the parameter, not the live process uid, drives
    the DEBUG/WARNING choice."""
    socket_path = tmp_path / "broker.sock"
    registry = PlatformAdapterRegistry({PlatformCode.GOOGLE: _StubAdsPlatformPort()})
    runtime = _runtime(registry, tmp_path / "credentials")
    impossible_uid = -1  # no real peer ever connects with this uid
    server = await serve(socket_path, runtime, frozenset(), self_uid=impossible_uid)
    monkeypatch.setattr(socket_server, "logger", structlog.get_logger())
    try:
        with structlog.testing.capture_logs() as logs:
            reader, writer = await asyncio.open_unix_connection(str(socket_path))
            client = FrameClient(reader, writer, max_frame_bytes=64 * 1024)
            await client.write_frame(b'{"op": "fetch_account_inventory"}')

            with pytest.raises((asyncio.IncompleteReadError, ConnectionError)):
                await client.read_frame()
    finally:
        server.close()
        await server.wait_closed()

    assert len(logs) == 1
    assert logs[0]["peer_uid"] == os.getuid()
    assert logs[0]["log_level"] == "warning"


async def test_unknown_op_is_denied(running_server: Path) -> None:
    response = await _connect_and_exchange(running_server, {"op": "totally_bogus_op"})

    assert response["ok"] is False
    assert response["error_code"] == "DENIED"


async def test_malformed_json_is_denied(running_server: Path) -> None:
    reader, writer = await asyncio.open_unix_connection(str(running_server))
    client = FrameClient(reader, writer, max_frame_bytes=64 * 1024)
    await client.write_frame(b"not json")

    response = json.loads(await client.read_frame())

    assert response["ok"] is False
    assert response["error_code"] == "DENIED"
    client.close()
    await client.wait_closed()


async def test_unexpected_exception_in_handle_payload_answers_instead_of_dropping_the_connection(
    running_server: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Regression (fix/broker-ad-library-typeerror): `handle_payload` never
    fail-opens BY CONTRACT (contracts/platform-port.md), but nothing
    enforced that before `_handle_connection` -- a bug that raises anywhere
    `dispatcher._dispatch` doesn't itself guard (its own `_ok_response`
    call sits OUTSIDE its try/except, see `test_reference_and_graph_ops_
    dispatch.py::test_meta_ads_archive_with_delivery_dates_end_to_end`) used
    to escape this coroutine entirely: `asyncio.start_unix_server`'s
    `client_connected_cb` then just drops the connection with an
    `Unhandled exception in client_connected_cb` nobody serving the socket
    could ever see or respond to."""

    async def _boom(raw: bytes, runtime: BrokerRuntime) -> bytes:  # noqa: ARG001
        raise TypeError("Object of type date is not JSON serializable")

    monkeypatch.setattr(socket_server, "handle_payload", _boom)
    monkeypatch.setattr(socket_server, "logger", structlog.get_logger())

    with structlog.testing.capture_logs() as logs:
        response = await _connect_and_exchange(running_server, {"op": "meta_ads_archive"})

    assert response == {"ok": False, "error_code": "FAILED", "reason": "broker_request_crashed"}
    assert len(logs) == 1
    assert logs[0]["event"] == "broker_request_crashed"
    assert logs[0]["op"] == "meta_ads_archive"
    assert logs[0]["error_type"] == "TypeError"
    assert logs[0]["log_level"] == "error"
    assert logs[0]["exc_info"] is True
    # `_boom` itself raises from this test module, outside `safent_ads` --
    # `_crash_location` only reports package frames, so the deepest one
    # left is `_handle_connection`'s own `await handle_payload(...)` call
    # site (a real crash inside the package reports a deeper one, see
    # `test_crash_location_reports_the_deepest_safent_ads_frame` below).
    assert logs[0]["where"] is not None
    assert logs[0]["where"].startswith("socket_server:")


async def test_crashed_request_logs_a_truncated_op(
    running_server: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Code review nit (PR 36): `op` is attacker-controlled and unbounded --
    `_op_from_raw` only checks it is a string, never a length."""

    async def _boom(raw: bytes, runtime: BrokerRuntime) -> bytes:  # noqa: ARG001
        raise TypeError("boom")

    monkeypatch.setattr(socket_server, "handle_payload", _boom)
    monkeypatch.setattr(socket_server, "logger", structlog.get_logger())
    long_op = "x" * 200

    with structlog.testing.capture_logs() as logs:
        await _connect_and_exchange(running_server, {"op": long_op})

    assert logs[0]["op"] == long_op[:64]
    assert len(logs[0]["op"]) == 64


def test_crash_location_reports_the_deepest_safent_ads_frame() -> None:
    """Code review nit (PR 36): production logging collapses `exc_info` to
    `exception_type` with no location (`logging_setup.py::
    _exception_type_only`) -- `where` is our own sanitized breadcrumb."""
    with pytest.raises(ValueError) as excinfo:
        map_ads_archive_row({"ad_delivery_start_time": "not-a-date"})

    location = socket_server._crash_location(excinfo.value)

    assert location is not None
    assert location.startswith("meta_ad_library:")


def test_crash_location_is_none_without_any_safent_ads_frame() -> None:
    try:
        raise TypeError("boom")
    except TypeError as exc:
        assert socket_server._crash_location(exc) is None


# ---------------------------------------------------------------------------
# M-3 (revision de seguridad 0.2.22): el limite de trama es por-operacion --
# 64 KiB por defecto para leer CUALQUIER peticion entrante (ninguna `op` de
# hoy manda bytes de imagen en la peticion), 16 MiB SOLO para la respuesta
# de `render_image`. Antes, `composition/broker.py` subia el limite GLOBAL
# del socket a 16 MiB, dejando pasar una peticion de basura de ese tamano
# bajo cualquier `op`.
# ---------------------------------------------------------------------------


def test_op_from_raw_reads_the_op_field() -> None:
    assert _op_from_raw(b'{"op": "render_image", "prompt": "x"}') == "render_image"


def test_op_from_raw_is_none_for_unparseable_or_shapeless_input() -> None:
    assert _op_from_raw(b"not json") is None
    assert _op_from_raw(b"[]") is None
    assert _op_from_raw(b'{"op": 123}') is None


def test_max_response_bytes_for_render_image_is_the_larger_ceiling() -> None:
    assert _max_response_bytes_for("render_image") == _MAX_EXTENDED_FRAME_BYTES
    assert _max_response_bytes_for("upload_asset") == _MAX_EXTENDED_FRAME_BYTES
    assert _MAX_EXTENDED_FRAME_BYTES > _DEFAULT_MAX_FRAME_BYTES


def test_max_response_bytes_for_any_other_op_stays_at_the_default() -> None:
    assert _max_response_bytes_for("meta_reference_read") == _DEFAULT_MAX_FRAME_BYTES
    assert _max_response_bytes_for(None) == _DEFAULT_MAX_FRAME_BYTES


class _BigImageRenderer:
    def __init__(self, name: RendererName, asset_store: InMemoryAssetStore, payload: bytes) -> None:
        self.name = name
        self._asset_store = asset_store
        self._payload = payload

    async def render(self, spec: ImageSpec) -> RenderedAsset:
        storage_uri = await self._asset_store.put(self._payload, MediaKind.IMAGE)
        return RenderedAsset(
            storage_uri=storage_uri,
            media_kind=MediaKind.IMAGE,
            format=spec.format,
            checksum="deadbeef",
            renderer_used=self.name,
            cost_estimate=Money(Decimal("0.02"), "USD"),
            duration_s=1.5,
            generated_at=_NOW,
        )


async def test_oversized_non_render_image_request_is_rejected_before_the_op_is_read(
    running_server: Path,
) -> None:
    """Una peticion de 1 MiB bajo `meta_reference_read` (el op real detras
    de la herramienta MCP `list_meta_pages`) se rechaza en la capa de
    framing -- la conexion se cierra sin respuesta, `handle_payload` nunca
    llega a verla."""
    reader, writer = await asyncio.open_unix_connection(str(running_server))
    client = FrameClient(reader, writer, max_frame_bytes=2 * 1024 * 1024)
    oversized_request = json.dumps(
        {"op": "meta_reference_read", "padding": "x" * (1024 * 1024)}
    ).encode("utf-8")

    with pytest.raises((asyncio.IncompleteReadError, ConnectionError)):
        await client.write_frame(oversized_request)
        await client.read_frame()

    client.close()


async def test_ten_mebibyte_request_claiming_another_op_is_rejected_after_the_probe(
    running_server: Path,
) -> None:
    """El rechazo depende del PREFIJO, nunca del tamano total: una trama
    de 10 MiB que declara `meta_reference_read` (op fuera de la
    ampliacion) se rechaza igual que una de 1 MiB -- el servidor nunca lee
    mas alla de los primeros 64 KiB de sonda."""
    reader, writer = await asyncio.open_unix_connection(str(running_server))
    client = FrameClient(reader, writer, max_frame_bytes=11 * 1024 * 1024)
    oversized_request = json.dumps(
        {"op": "meta_reference_read", "padding": "x" * (10 * 1024 * 1024)}
    ).encode("utf-8")

    with pytest.raises((asyncio.IncompleteReadError, ConnectionError)):
        await client.write_frame(oversized_request)
        await client.read_frame()

    client.close()


async def test_ten_mebibyte_upload_asset_request_is_accepted_at_the_framing_layer(
    running_server: Path,
) -> None:
    """`upload_asset` manda los bytes EN LA PETICION (al reves que
    `render_image`, que los devuelve en la respuesta) -- la capa de
    framing debe dejar pasar hasta 16 MiB para esa `op`, y el round-trip
    completo (framing + esquema + adaptador) debe funcionar de verdad."""
    media = b"0" * (10 * 1024 * 1024)
    response = await _connect_and_exchange(
        running_server,
        {
            "op": "upload_asset",
            "platform": "google",
            "external_account_id": "123-456-7890",
            "file_name": "banner.png",
            "mime_type": "image/png",
            "media_base64": base64.b64encode(media).decode("ascii"),
        },
    )

    assert response["ok"] is True
    assert response["result"] == {
        "platform_asset_id": "asset-1",
        "preview_url": "https://cdn.example/asset-1",
    }


async def test_render_image_response_of_ten_mebibytes_is_accepted(tmp_path: Path) -> None:
    """La respuesta grande de `render_image` (aqui 10 MiB de PNG, ~13.3 MiB
    ya en base64) sigue aceptandose bajo el limite propio de esa `op`
    (16 MiB), aunque la lectura de la peticion se haya quedado en 64 KiB
    por defecto."""
    socket_path = tmp_path / "broker.sock"
    ten_mebibyte_png = _PNG_MAGIC + b"0" * (10 * 1024 * 1024 - len(_PNG_MAGIC))
    asset_store = InMemoryAssetStore()
    renderer = _BigImageRenderer(RendererName.FLUX2_KLEIN_9B, asset_store, ten_mebibyte_png)
    render_image_service = RenderImageService(
        {RendererName.FLUX2_KLEIN_9B: renderer}, asset_store=asset_store, clock=FixedClock(_NOW)
    )
    registry = PlatformAdapterRegistry({PlatformCode.GOOGLE: _StubAdsPlatformPort()})
    runtime = replace(
        _runtime(registry, socket_path.parent / "credentials"),
        render_image_service=render_image_service,
    )
    server = await serve(socket_path, runtime, frozenset({os.getuid()}))
    try:
        reader, writer = await asyncio.open_unix_connection(str(socket_path))
        client = FrameClient(reader, writer, max_frame_bytes=16 * 1024 * 1024)
        await client.write_frame(
            json.dumps(
                {
                    "op": "render_image",
                    "business_id": "biz-1",
                    "renderer": RendererName.FLUX2_KLEIN_9B.value,
                    "prompt": "un perro feliz en un parque",
                    "format": "1080x1080",
                    "seed": 7,
                    "brand_kit": _BRAND_KIT_FIELDS,
                }
            ).encode("utf-8")
        )
        raw_response = await client.read_frame()
        client.close()
        await client.wait_closed()
    finally:
        server.close()
        await server.wait_closed()

    response = json.loads(raw_response)
    assert response["ok"] is True
    assert base64.b64decode(response["result"]["image_base64"]) == ten_mebibyte_png


async def test_write_frame_rejects_a_payload_over_its_own_max_bytes() -> None:
    """Unidad directa de `FrameClient.write_frame`: `max_bytes` es
    independiente del `max_frame_bytes` de lectura -- una respuesta que se
    pasa del limite propio de la `op` nunca sale al socket."""

    class _StubWriter:
        def write(self, data: bytes) -> None:  # noqa: ARG002 - nunca debe llamarse
            raise AssertionError("no deberia escribir nada si supera max_bytes")

        async def drain(self) -> None:  # pragma: no cover - nunca se alcanza
            raise AssertionError

    client = FrameClient(
        reader=None,  # type: ignore[arg-type] - este test nunca lee
        writer=_StubWriter(),  # type: ignore[arg-type]
        max_frame_bytes=64 * 1024,
    )

    with pytest.raises(FrameTooLargeError):
        await client.write_frame(b"0" * 100, max_bytes=64)

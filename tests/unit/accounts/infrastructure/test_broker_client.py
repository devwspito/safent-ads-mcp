"""`BrokerSocketClient` contra un `broker.presentation.socket_server.serve`
real en un socket Unix de un directorio temporal: prueba que las dos
implementaciones de `AdsPlatformPort` (contracts/platform-port.md) son
compatibles de verdad, no solo por contrato en papel."""

from __future__ import annotations

import asyncio
import os
import tempfile
from collections.abc import Mapping, Sequence
from datetime import UTC, datetime
from pathlib import Path

import pytest

from safent_ads.accounts.application.errors import BrokerRequestDeniedError
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
    WriteOperation,
    WriteOutcome,
)
from safent_ads.accounts.domain.ad_entity import AdEntityStatus
from safent_ads.accounts.domain.learning_state import LearningState
from safent_ads.accounts.infrastructure.broker_client import BrokerSocketClient
from safent_ads.accounts.infrastructure.errors import BrokerConnectionError
from safent_ads.broker.infrastructure.adapter_registry import PlatformAdapterRegistry
from safent_ads.broker.presentation.socket_server import serve
from safent_ads.proposals.domain.diff_hash import compute_diff_hash
from safent_ads.shared.ids import EntityLevel, EntityRef, PlatformCode

_NOW = datetime(2026, 9, 9, tzinfo=UTC)
_CAMPAIGN_REF = EntityRef(PlatformCode.GOOGLE, EntityLevel.CAMPAIGN, "campaign-1")
_ACCOUNT_PARENT_REF = EntityRef(PlatformCode.GOOGLE, EntityLevel.ACCOUNT, "123-456-7890")
_SUCCEEDED_OUTCOME = WriteOutcome(
    outcome="SUCCEEDED",
    applied_value="PAUSED",
    state_hash_after="a" * 64,
    error_code=None,
    platform_request_id="req-1",
)


class _StubAdsPlatformPort(AdsPlatformPort):
    def __init__(self, write_outcome: WriteOutcome = _SUCCEEDED_OUTCOME) -> None:
        self.write_outcome = write_outcome
        self.write_calls: list[tuple[WriteIntent, SignedAuthorization, IdempotencyKey]] = []

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

    async def upload_asset(
        self, request: AssetUploadRequest  # noqa: ARG002 - forma exacta del puerto
    ) -> PlatformAssetHandle:
        raise NotImplementedError

    async def run_gaql(
        self, account_ref: AccountRef, query: str, *, max_rows: int  # noqa: ARG002
    ) -> Sequence[Mapping[str, object]]:
        return [{"campaign.id": "111", "query": query}][:max_rows]

    async def execute_write(
        self,
        intent: WriteIntent,
        authorization: SignedAuthorization,
        idempotency_key: IdempotencyKey,
    ) -> WriteOutcome:
        self.write_calls.append((intent, authorization, idempotency_key))
        return self.write_outcome


class _ColdStartAdsPlatformPort(_StubAdsPlatformPort):
    """Simula el arranque en frio (incidente de produccion, 16-sep,
    companion 0.2.31): la PRIMERA llamada de una `op` tarda mas que el
    timeout del cliente -- en produccion eso es el broker construyendo en
    caliente el adaptador/cliente SDK todavia sin usar
    (`DynamicPlatformAdapterRegistry`, cacheado despues); aqui basta un
    `asyncio.sleep` en la primera llamada. Toda llamada posterior responde
    al instante, igual que en produccion tras el primer exito."""

    def __init__(self, *, slow_seconds: float) -> None:
        super().__init__()
        self._slow_seconds = slow_seconds
        self.fetch_account_inventory_calls = 0
        self.execute_write_calls = 0

    async def fetch_account_inventory(self, account_ref: AccountRef) -> Sequence[AdEntitySnapshot]:
        self.fetch_account_inventory_calls += 1
        if self.fetch_account_inventory_calls == 1:
            await asyncio.sleep(self._slow_seconds)
        return await super().fetch_account_inventory(account_ref)

    async def execute_write(
        self,
        intent: WriteIntent,
        authorization: SignedAuthorization,
        idempotency_key: IdempotencyKey,
    ) -> WriteOutcome:
        self.execute_write_calls += 1
        if self.execute_write_calls == 1:
            await asyncio.sleep(self._slow_seconds)
        return await super().execute_write(intent, authorization, idempotency_key)


@pytest.fixture
async def broker_client(tmp_path: Path):
    socket_path = tmp_path / "broker.sock"
    registry = PlatformAdapterRegistry({PlatformCode.GOOGLE: _StubAdsPlatformPort()})
    server = await serve(socket_path, registry, frozenset({os.getuid()}))
    try:
        yield BrokerSocketClient(socket_path)
    finally:
        server.close()
        await server.wait_closed()


async def test_fetch_account_inventory_round_trips_through_the_wire(
    broker_client: BrokerSocketClient,
) -> None:
    snapshots = await broker_client.fetch_account_inventory(
        AccountRef(PlatformCode.GOOGLE, "123-456-7890")
    )

    assert len(snapshots) == 1
    assert snapshots[0].entity_ref == _CAMPAIGN_REF
    assert snapshots[0].status == AdEntityStatus.ACTIVE


async def test_read_entity_state_round_trips_through_the_wire(
    broker_client: BrokerSocketClient,
) -> None:
    snapshot = await broker_client.read_entity_state(_CAMPAIGN_REF)

    assert snapshot.entity_ref == _CAMPAIGN_REF
    assert snapshot.canonical_state == {"status": "ACTIVE"}


def _intent() -> WriteIntent:
    diff_hash = compute_diff_hash(_CAMPAIGN_REF, "status", "ACTIVE", "PAUSED")
    return WriteIntent(
        entity_ref=_CAMPAIGN_REF,
        operation=WriteOperation.PAUSE,
        parametro="status",
        valor_actual="ACTIVE",
        valor_propuesto="PAUSED",
        diff_hash=diff_hash,
        expected_state_hash="b" * 64,
    )


def _authorization() -> SignedAuthorization:
    return SignedAuthorization(
        authorization_id="auth-1",
        proposal_id="proposal-1",
        kind="human_approval",
        diff_hash=_intent().diff_hash,
        guardrail_verdict_hash="c" * 64,
        issued_by="owner-1",
        expires_at=_NOW,
        signature="deadbeef",
    )


async def _run_execute_write(
    socket_path: Path, stub: _StubAdsPlatformPort
) -> WriteOutcome:
    registry = PlatformAdapterRegistry({PlatformCode.GOOGLE: stub})
    server = await serve(socket_path, registry, frozenset({os.getuid()}))
    try:
        client = BrokerSocketClient(socket_path)
        return await client.execute_write(_intent(), _authorization(), IdempotencyKey("key-1"))
    finally:
        server.close()
        await server.wait_closed()


async def test_execute_write_round_trips_a_succeeded_outcome_through_the_wire() -> None:
    stub = _StubAdsPlatformPort()

    with tempfile.TemporaryDirectory() as tmp_dir:
        outcome = await _run_execute_write(Path(tmp_dir) / "broker.sock", stub)

    assert outcome == _SUCCEEDED_OUTCOME
    assert len(stub.write_calls) == 1
    sent_intent, sent_authorization, sent_key = stub.write_calls[0]
    assert sent_intent == _intent()
    assert sent_authorization == _authorization()
    assert str(sent_key) == "key-1"


async def test_execute_write_keeps_a_denied_outcome_typed_not_an_exception() -> None:
    denied = WriteOutcome(
        outcome="DENIED",
        applied_value=None,
        state_hash_after=None,
        error_code="invalid_signature",
        platform_request_id=None,
    )
    stub = _StubAdsPlatformPort(write_outcome=denied)

    with tempfile.TemporaryDirectory() as tmp_dir:
        outcome = await _run_execute_write(Path(tmp_dir) / "broker.sock", stub)

    assert outcome == denied


async def test_connection_error_when_socket_missing(tmp_path: Path) -> None:
    client = BrokerSocketClient(tmp_path / "nonexistent.sock", timeout_seconds=1.0)

    with pytest.raises(BrokerConnectionError):
        await client.fetch_account_inventory(AccountRef(PlatformCode.GOOGLE, "123"))


async def test_upload_asset_is_denied(broker_client: BrokerSocketClient) -> None:
    with pytest.raises(BrokerRequestDeniedError):
        await broker_client.upload_asset(
            AssetUploadRequest(
                account_ref=AccountRef(PlatformCode.GOOGLE, "123"),
                file_name="banner.png",
                mime_type="image/png",
                media=b"",
            )
        )


async def test_run_gaql_round_trips_through_the_wire(
    broker_client: BrokerSocketClient,
) -> None:
    rows = await broker_client.run_gaql(
        AccountRef(PlatformCode.GOOGLE, "123-456-7890"),
        "SELECT campaign.id FROM campaign",
        max_rows=5,
    )

    assert rows == [{"campaign.id": "111", "query": "SELECT campaign.id FROM campaign"}]


async def test_first_read_slower_than_timeout_is_retried_once_and_succeeds(
    tmp_path: Path,
) -> None:
    """Regresion (incidente de produccion, 16-sep): justo tras `docker
    compose up -d` recrear `ads-broker`, la primera lectura de una
    plataforma tardaba mas que `timeout_seconds` -- el cliente cerraba el
    socket (`BrokerConnectionError` -> `BROKER_UNAVAILABLE` en la
    herramienta MCP) mientras el broker seguia trabajando y acababa
    escribiendo sobre una tuberia rota (`BrokenPipeError` en su log). Sin el
    reintento de `BrokerSocketClient._request`, esta prueba falla con
    `BrokerConnectionError` en vez de devolver el inventario."""
    stub = _ColdStartAdsPlatformPort(slow_seconds=1.0)
    registry = PlatformAdapterRegistry({PlatformCode.GOOGLE: stub})
    socket_path = tmp_path / "broker.sock"
    server = await serve(socket_path, registry, frozenset({os.getuid()}))
    try:
        client = BrokerSocketClient(socket_path, timeout_seconds=0.2)
        snapshots = await client.fetch_account_inventory(AccountRef(PlatformCode.GOOGLE, "123"))
    finally:
        server.close()
        await server.wait_closed()

    assert len(snapshots) == 1
    # El lento (descartado) + el reintento, ya en caliente.
    assert stub.fetch_account_inventory_calls == 2


async def test_write_is_never_retried_on_a_cold_broker(tmp_path: Path) -> None:
    """Simetrico a la prueba anterior: `execute_write` no es una lectura
    idempotente (firma/crea algo del lado del proveedor) -- una respuesta
    lenta debe seguir fallando con `BrokerConnectionError`, nunca
    reintentarse sola con una segunda conexion."""
    stub = _ColdStartAdsPlatformPort(slow_seconds=1.0)
    registry = PlatformAdapterRegistry({PlatformCode.GOOGLE: stub})
    socket_path = tmp_path / "broker.sock"
    server = await serve(socket_path, registry, frozenset({os.getuid()}))
    try:
        client = BrokerSocketClient(socket_path, timeout_seconds=0.2)
        with pytest.raises(BrokerConnectionError):
            await client.execute_write(_intent(), _authorization(), IdempotencyKey("key-1"))
    finally:
        server.close()
        await server.wait_closed()

    assert stub.execute_write_calls == 1

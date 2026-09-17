"""`MetaAdsAdapter` (T026/F2): SDK sustituido por `MetaGraphClient` falso.
Cubre `test_write_budget_per_window`, `is_controllable` en Advantage+
campaign budget, backoff+jitter, saneo de errores, y los 8 controles de
`execute_write` sobre una plataforma real."""

from __future__ import annotations

import base64
import random
from collections.abc import Mapping, Sequence
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from textwrap import dedent
from typing import Any

import pytest

from safent_ads.accounts.application.errors import EntityNotFoundError
from safent_ads.accounts.application.ports import (
    AccountRef,
    AssetUploadRequest,
    DateWindow,
    MetricGranularity,
    MetricsRequest,
    SignedAuthorization,
    WriteIntent,
    WriteOperation,
    WriteOutcome,
)
from safent_ads.accounts.domain.ad_entity import AdEntityStatus
from safent_ads.accounts.domain.platform_state_hash import PlatformStateHash
from safent_ads.accounts.domain.refs import IdempotencyKey
from safent_ads.broker.domain.write_authorization import authorization_signing_payload
from safent_ads.broker.infrastructure.caps_config import parse_caps_config
from safent_ads.broker.infrastructure.write_ledger_store import WriteLedgerStore
from safent_ads.broker.platforms.errors import PlatformCapabilityNotImplementedError
from safent_ads.broker.platforms.meta_ads_adapter import (
    MetaAdsAdapter,
    MetaAdsAdapterConfig,
    MetaAdsAdapterError,
    _has_advantage_campaign_budget,
    _map_status,
    backoff_delay_seconds,
    is_retryable_error,
)
from safent_ads.broker.platforms.write_pipeline import WriteAuthorizationPipeline
from safent_ads.proposals.domain.diff_hash import compute_diff_hash
from safent_ads.shared.clock import FixedClock
from safent_ads.shared.crypto.ed25519 import ApprovalSigner, ApprovalVerifier
from safent_ads.shared.ids import EntityLevel, EntityRef, PlatformCode
from tests.unit.broker.ledger_scope_fakes import fake_scope

_NOW = datetime(2026, 9, 9, 12, tzinfo=UTC)
_AD_ACCOUNT_ID = "act_1234567890"


def test_unknown_meta_entity_status_fails_closed() -> None:
    with pytest.raises(MetaAdsAdapterError, match="estado de entidad desconocido"):
        _map_status("IN_PROCESS")


_CAPS_YAML = dedent(
    f"""
    defaults:
      max_step_pct: 100
      max_changes_per_day: 5
      autonomy_enabled: true
    accounts:
      "{_AD_ACCOUNT_ID}":
        daily_cap_minor: 100000000
        monthly_cap_minor: 1000000000
        floor_minor: 100
        ceiling_minor: 100000000
    """
)


class _FakeGraphClient:
    def __init__(
        self,
        nodes: dict[str, Mapping[str, Any]] | None = None,
        edges: dict[tuple[str, str], list[Mapping[str, Any]]] | None = None,
        error: Exception | None = None,
        update_error: Exception | None = None,
        image_response: Mapping[str, Any] | None = None,
    ) -> None:
        self.nodes = nodes or {}
        self.edges = edges or {}
        self.error = error
        self.update_error = update_error
        self.updates: list[tuple[str, Mapping[str, Any]]] = []
        self.edge_calls: list[tuple[str, str, Sequence[str], Mapping[str, Any] | None]] = []
        self.image_response = image_response
        self.create_image_calls: list[tuple[str, str, bytes]] = []

    def create_image(self, account_id: str, file_name: str, media: bytes) -> Mapping[str, Any]:
        self.create_image_calls.append((account_id, file_name, media))
        if self.image_response is not None:
            return self.image_response
        return {"hash": "img-hash-abc", "url": "https://scontent.fmad1.fbcdn.net/x.jpg"}

    def get_node(
        self,
        node_id: str,
        fields: Sequence[str],  # noqa: ARG002 - forma exacta del puerto
    ) -> Mapping[str, Any]:
        if self.error is not None:
            raise self.error
        return self.nodes[node_id]

    def get_edge(
        self,
        node_id: str,
        edge: str,
        fields: Sequence[str],  # noqa: ARG002 - forma exacta del puerto
        params: Mapping[str, Any] | None = None,  # noqa: ARG002
        *,
        paginate: bool = True,  # noqa: ARG002 - forma exacta del puerto (A-1)
    ) -> Sequence[Mapping[str, Any]]:
        if self.error is not None:
            raise self.error
        self.edge_calls.append((node_id, edge, fields, params))
        return self.edges.get((node_id, edge), [])

    def update_node(self, node_id: str, fields: Mapping[str, Any]) -> None:
        if self.update_error is not None:
            raise self.update_error
        self.updates.append((node_id, dict(fields)))


def _config() -> MetaAdsAdapterConfig:
    return MetaAdsAdapterConfig(
        app_id="app-id", app_secret="app-secret", system_user_token="system-user-token"
    )


def test_write_budget_per_window() -> None:
    clock = FixedClock(_NOW)
    adapter = MetaAdsAdapter(_config(), _FakeGraphClient(), clock)

    for _ in range(20):
        assert adapter.can_attempt_write() is True

    assert adapter.can_attempt_write() is False


async def test_fetch_account_inventory_marks_advantage_budget_ad_sets_uncontrollable() -> None:
    campaign = {"id": "111", "name": "Campana ASC", "status": "ACTIVE", "daily_budget": "5000"}
    ad_set = {"id": "222", "name": "Conjunto A", "status": "ACTIVE", "campaign_id": "111"}
    client = _FakeGraphClient(
        nodes={_AD_ACCOUNT_ID: {"currency": "USD"}},
        edges={
            (_AD_ACCOUNT_ID, "campaigns"): [campaign],
            (f"{_AD_ACCOUNT_ID}/111", "adsets"): [ad_set],
        }
    )
    adapter = MetaAdsAdapter(_config(), client, FixedClock(_NOW))

    snapshots = await adapter.fetch_account_inventory(AccountRef(PlatformCode.META, _AD_ACCOUNT_ID))

    ad_set_snapshot = next(s for s in snapshots if s.entity_ref.level == EntityLevel.AD_SET)
    assert ad_set_snapshot.is_controllable is False
    assert ad_set_snapshot.budget is None
    campaign_snapshot = next(s for s in snapshots if s.entity_ref.level == EntityLevel.CAMPAIGN)
    assert campaign_snapshot.budget is not None
    assert campaign_snapshot.budget.amount.currency == "USD"


async def test_fetch_account_inventory_controllable_ad_set_without_cbo() -> None:
    campaign = {"id": "111", "name": "Campana normal", "status": "ACTIVE"}
    ad_set = {
        "id": "222",
        "name": "Conjunto A",
        "status": "ACTIVE",
        "campaign_id": "111",
        "daily_budget": "3000",
    }
    client = _FakeGraphClient(
        nodes={_AD_ACCOUNT_ID: {"currency": "GBP"}},
        edges={
            (_AD_ACCOUNT_ID, "campaigns"): [campaign],
            (f"{_AD_ACCOUNT_ID}/111", "adsets"): [ad_set],
        }
    )
    adapter = MetaAdsAdapter(_config(), client, FixedClock(_NOW))

    snapshots = await adapter.fetch_account_inventory(AccountRef(PlatformCode.META, _AD_ACCOUNT_ID))

    ad_set_snapshot = next(s for s in snapshots if s.entity_ref.level == EntityLevel.AD_SET)
    assert ad_set_snapshot.is_controllable is True
    assert ad_set_snapshot.budget is not None
    assert ad_set_snapshot.budget.amount.currency == "GBP"


@pytest.mark.parametrize(
    ("campaign", "expected"),
    [
        ({"daily_budget": "1000"}, True),
        ({"lifetime_budget": "50000"}, True),
        ({}, False),
        ({"daily_budget": None, "lifetime_budget": None}, False),
    ],
)
def test_has_advantage_campaign_budget(campaign: Mapping[str, Any], expected: bool) -> None:
    assert _has_advantage_campaign_budget(campaign) is expected


async def test_read_entity_state_maps_status() -> None:
    client = _FakeGraphClient(nodes={"111": {"id": "111", "name": "Campana", "status": "PAUSED"}})
    adapter = MetaAdsAdapter(_config(), client, FixedClock(_NOW))

    snapshot = await adapter.read_entity_state(
        EntityRef(PlatformCode.META, EntityLevel.CAMPAIGN, "111")
    )

    assert snapshot.status == AdEntityStatus.PAUSED


async def test_sdk_error_is_redacted() -> None:
    error = RuntimeError("token EAABsomeLongMetaSystemUserTokenValue123, code 613")
    client = _FakeGraphClient(error=error)
    adapter = MetaAdsAdapter(_config(), client, FixedClock(_NOW))

    with pytest.raises(MetaAdsAdapterError) as exc_info:
        await adapter.read_entity_state(EntityRef(PlatformCode.META, EntityLevel.CAMPAIGN, "111"))

    assert "EAABsomeLongMetaSystemUserTokenValue123" not in str(exc_info.value)


@pytest.mark.parametrize("code", [17, 32, 613])
def test_is_retryable_error_for_known_codes(code: int) -> None:
    assert is_retryable_error(code) is True


def test_is_retryable_error_false_for_unknown_code() -> None:
    assert is_retryable_error(190) is False


def test_backoff_delay_grows_with_attempts_and_stays_within_bounds() -> None:
    rng = random.Random(42)  # noqa: S311 - jitter de reintentos, no criptografico

    first = backoff_delay_seconds(0, rng=rng)
    third = backoff_delay_seconds(3, rng=rng)

    assert 0 <= first <= 1
    assert 0 <= third <= 8


def test_backoff_delay_respects_cap() -> None:
    rng = random.Random(1)  # noqa: S311 - jitter de reintentos, no criptografico

    delay = backoff_delay_seconds(20, cap=10.0, rng=rng)

    assert 0 <= delay <= 10.0


async def test_fetch_metrics_uses_time_increment_one_per_day() -> None:
    insight_row = {
        "date_start": "2026-09-08",
        "spend": "12.50",
        "impressions": "500",
        "clicks": "20",
        "reach": "400",
        "frequency": "1.25",
        "actions": [{"action_type": "lead", "value": "3"}],
        "action_values": [{"action_type": "lead", "value": "45.00"}],
        "account_currency": "USD",
    }
    client = _FakeGraphClient(edges={(f"{_AD_ACCOUNT_ID}/111", "insights"): [insight_row]})
    adapter = MetaAdsAdapter(_config(), client, FixedClock(_NOW))
    request = MetricsRequest(
        account_ref=AccountRef(PlatformCode.META, _AD_ACCOUNT_ID),
        window=DateWindow(start=date(2026, 9, 8), end=date(2026, 9, 8)),
        granularity=MetricGranularity.DAILY,
        entity_refs=[EntityRef(PlatformCode.META, EntityLevel.CAMPAIGN, f"{_AD_ACCOUNT_ID}/111")],
    )

    facts = await adapter.fetch_metrics(request)

    assert len(facts) == 1
    assert facts[0].spend.minor_units == 1250
    assert facts[0].spend.currency == "USD"
    assert facts[0].stat_hour is None
    assert facts[0].conversions_by_kind == {"lead": 3}


async def test_fetch_metrics_supports_meta_advertiser_timezone_hourly_breakdown() -> None:
    row = {
        "date_start": "2026-09-08",
        "hourly_stats_aggregated_by_advertiser_time_zone": "07:00:00 - 07:59:59",
        "spend": "1.25",
        "impressions": "50",
        "clicks": "2",
        "account_currency": "EUR",
    }
    node = f"{_AD_ACCOUNT_ID}/111"
    client = _FakeGraphClient(edges={(node, "insights"): [row]})
    adapter = MetaAdsAdapter(_config(), client, FixedClock(_NOW))

    facts = await adapter.fetch_metrics(
        MetricsRequest(
            account_ref=AccountRef(PlatformCode.META, _AD_ACCOUNT_ID),
            window=DateWindow(start=date(2026, 9, 8), end=date(2026, 9, 8)),
            granularity=MetricGranularity.HOURLY,
            entity_refs=[
                EntityRef(PlatformCode.META, EntityLevel.CAMPAIGN, node)
            ],
        )
    )

    assert facts[0].stat_hour == 7
    params = client.edge_calls[-1][3]
    assert params is not None
    assert params["breakdowns"] == ["hourly_stats_aggregated_by_advertiser_time_zone"]


async def test_fetch_metrics_rejects_missing_currency_instead_of_inventing_eur() -> None:
    node = f"{_AD_ACCOUNT_ID}/111"
    client = _FakeGraphClient(
        edges={(node, "insights"): [{"date_start": "2026-09-08", "spend": "1"}]}
    )
    adapter = MetaAdsAdapter(_config(), client, FixedClock(_NOW))

    with pytest.raises(MetaAdsAdapterError, match="account_currency"):
        await adapter.fetch_metrics(
            MetricsRequest(
                account_ref=AccountRef(PlatformCode.META, _AD_ACCOUNT_ID),
                window=DateWindow(start=date(2026, 9, 8), end=date(2026, 9, 8)),
                granularity=MetricGranularity.DAILY,
                entity_refs=[
                    EntityRef(PlatformCode.META, EntityLevel.CAMPAIGN, node)
                ],
            )
        )


# ---------------------------------------------------------------------------
# execute_write (F2): los 8 controles de contracts/platform-port.md
# ---------------------------------------------------------------------------

_CAMPAIGN_NODE: dict[str, Any] = {
    "id": "111",
    "name": "Campana",
    "status": "ACTIVE",
    "daily_budget": "500",
    "account_id": "1234567890",
}
_CAMPAIGN_STATE_HASH = PlatformStateHash.compute(_CAMPAIGN_NODE).value


def _keypair(seed_byte: bytes = b"1") -> tuple[ApprovalSigner, ApprovalVerifier]:
    seed_b64 = base64.b64encode(seed_byte * 32).decode()
    signer = ApprovalSigner.from_seed_b64(seed_b64)
    return signer, ApprovalVerifier.from_public_key_b64(signer.public_key_b64())


def _pipeline(tmp_path: Path, verifier: ApprovalVerifier) -> WriteAuthorizationPipeline:
    caps = parse_caps_config(_CAPS_YAML)
    ledger = WriteLedgerStore(tmp_path / "write_ledger.sqlite3")
    return WriteAuthorizationPipeline(verifier, caps, ledger, scope_resolver=fake_scope)


def _intent(
    entity_ref: EntityRef,
    *,
    parametro: str = "status",
    before: object = "ACTIVE",
    after: object = "PAUSED",
    operation: WriteOperation = WriteOperation.PAUSE,
    expected_state_hash: str = _CAMPAIGN_STATE_HASH,
) -> WriteIntent:
    diff_hash = compute_diff_hash(entity_ref, parametro, before, after)
    return WriteIntent(
        business_id="aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa",
        entity_ref=entity_ref,
        operation=operation,
        parametro=parametro,
        valor_actual=before,
        valor_propuesto=after,
        diff_hash=diff_hash,
        expected_state_hash=expected_state_hash,
    )


def _authorization(
    signer: ApprovalSigner,
    *,
    diff_hash: str,
    kind: str = "human_approval",
    expires_at: datetime = _NOW + timedelta(hours=1),
) -> SignedAuthorization:
    unsigned = SignedAuthorization(
        authorization_id="auth-1",
        proposal_id="proposal-1",
        kind=kind,  # type: ignore[arg-type]
        diff_hash=diff_hash,
        guardrail_verdict_hash="v" * 64,
        issued_by="owner-1",
        expires_at=expires_at,
        signature="",
    )
    signature = signer.sign(authorization_signing_payload(unsigned)).hex()
    return SignedAuthorization(
        authorization_id=unsigned.authorization_id,
        proposal_id=unsigned.proposal_id,
        kind=unsigned.kind,
        diff_hash=unsigned.diff_hash,
        guardrail_verdict_hash=unsigned.guardrail_verdict_hash,
        issued_by=unsigned.issued_by,
        expires_at=unsigned.expires_at,
        signature=signature,
    )


def _campaign_ref() -> EntityRef:
    return EntityRef(PlatformCode.META, EntityLevel.CAMPAIGN, "111")


async def test_execute_write_denied_when_pipeline_not_wired() -> None:
    client = _FakeGraphClient(nodes={"111": _CAMPAIGN_NODE})
    adapter = MetaAdsAdapter(_config(), client, FixedClock(_NOW))
    entity_ref = _campaign_ref()
    intent = _intent(entity_ref)
    signer, _ = _keypair()
    authorization = _authorization(signer, diff_hash=intent.diff_hash)

    outcome = await adapter.execute_write(intent, authorization, IdempotencyKey("key-1"))

    assert outcome.outcome == "DENIED"
    assert outcome.error_code == "write_path_not_wired"


async def test_execute_write_pauses_a_campaign(tmp_path: Path) -> None:
    signer, verifier = _keypair()
    client = _FakeGraphClient(nodes={"111": _CAMPAIGN_NODE})
    adapter = MetaAdsAdapter(
        _config(), client, FixedClock(_NOW), write_pipeline=_pipeline(tmp_path, verifier)
    )
    entity_ref = _campaign_ref()
    intent = _intent(entity_ref)
    authorization = _authorization(signer, diff_hash=intent.diff_hash)

    outcome = await adapter.execute_write(intent, authorization, IdempotencyKey("key-1"))

    assert outcome.outcome == "SUCCEEDED"
    assert outcome.applied_value == "PAUSED"
    assert client.updates == [("111", {"status": "PAUSED"})]


async def test_execute_write_deletes_a_campaign(tmp_path: Path) -> None:
    """design.md §0.7: borrar es irreversible en
    Meta -- se aplica poniendo `status = DELETED` en el mismo nodo que
    pausar/reanudar, ningun endpoint DELETE propio."""
    signer, verifier = _keypair()
    client = _FakeGraphClient(nodes={"111": _CAMPAIGN_NODE})
    adapter = MetaAdsAdapter(
        _config(), client, FixedClock(_NOW), write_pipeline=_pipeline(tmp_path, verifier)
    )
    entity_ref = _campaign_ref()
    intent = _intent(entity_ref, after="DELETED", operation=WriteOperation.DELETE)
    authorization = _authorization(signer, diff_hash=intent.diff_hash)

    outcome = await adapter.execute_write(intent, authorization, IdempotencyKey("key-1"))

    assert outcome.outcome == "SUCCEEDED"
    assert outcome.applied_value == "DELETED"
    assert client.updates == [("111", {"status": "DELETED"})]


async def test_execute_write_lowers_a_campaign_budget(tmp_path: Path) -> None:
    signer, verifier = _keypair()
    client = _FakeGraphClient(nodes={"111": _CAMPAIGN_NODE})
    adapter = MetaAdsAdapter(
        _config(), client, FixedClock(_NOW), write_pipeline=_pipeline(tmp_path, verifier)
    )
    entity_ref = _campaign_ref()
    intent = _intent(
        entity_ref,
        parametro="daily_budget",
        before={"amount": "5.00", "currency": "EUR"},
        after={"amount": "3.50", "currency": "EUR"},
        operation=WriteOperation.LOWER_BUDGET,
    )
    authorization = _authorization(signer, diff_hash=intent.diff_hash)

    outcome = await adapter.execute_write(intent, authorization, IdempotencyKey("key-1"))

    assert outcome.outcome == "SUCCEEDED"
    assert client.updates == [("111", {"daily_budget": 350})]


async def test_signature_mismatch_denied(tmp_path: Path) -> None:
    signer, _own_verifier = _keypair(b"1")
    _, other_verifier = _keypair(b"2")
    client = _FakeGraphClient(nodes={"111": _CAMPAIGN_NODE})
    adapter = MetaAdsAdapter(
        _config(), client, FixedClock(_NOW), write_pipeline=_pipeline(tmp_path, other_verifier)
    )
    entity_ref = _campaign_ref()
    intent = _intent(entity_ref)
    authorization = _authorization(signer, diff_hash=intent.diff_hash)

    outcome = await adapter.execute_write(intent, authorization, IdempotencyKey("key-1"))

    assert outcome.outcome == "DENIED"
    assert outcome.error_code == "invalid_signature"
    assert client.updates == []


async def test_expired_authorization_denied(tmp_path: Path) -> None:
    signer, verifier = _keypair()
    client = _FakeGraphClient(nodes={"111": _CAMPAIGN_NODE})
    adapter = MetaAdsAdapter(
        _config(), client, FixedClock(_NOW), write_pipeline=_pipeline(tmp_path, verifier)
    )
    entity_ref = _campaign_ref()
    intent = _intent(entity_ref)
    authorization = _authorization(
        signer, diff_hash=intent.diff_hash, expires_at=_NOW - timedelta(minutes=1)
    )

    outcome = await adapter.execute_write(intent, authorization, IdempotencyKey("key-1"))

    assert outcome.outcome == "DENIED"
    assert outcome.error_code == "authorization_expired"
    assert client.updates == []


async def test_rule_authorization_cannot_raise_budget(tmp_path: Path) -> None:
    signer, verifier = _keypair()
    client = _FakeGraphClient(nodes={"111": _CAMPAIGN_NODE})
    adapter = MetaAdsAdapter(
        _config(), client, FixedClock(_NOW), write_pipeline=_pipeline(tmp_path, verifier)
    )
    entity_ref = _campaign_ref()
    intent = _intent(
        entity_ref,
        parametro="daily_budget",
        before={"amount": "5.00", "currency": "EUR"},
        after={"amount": "8.00", "currency": "EUR"},
        operation=WriteOperation.RAISE_BUDGET,
    )
    authorization = _authorization(signer, diff_hash=intent.diff_hash, kind="rule_authorization")

    outcome = await adapter.execute_write(intent, authorization, IdempotencyKey("key-1"))

    assert outcome.outcome == "DENIED"
    assert outcome.error_code == "rule_authorization_cannot_increase_spend"
    assert client.updates == []


async def test_drift_skips_write(tmp_path: Path) -> None:
    signer, verifier = _keypair()
    client = _FakeGraphClient(nodes={"111": _CAMPAIGN_NODE})
    adapter = MetaAdsAdapter(
        _config(), client, FixedClock(_NOW), write_pipeline=_pipeline(tmp_path, verifier)
    )
    entity_ref = _campaign_ref()
    intent = _intent(entity_ref, expected_state_hash="d" * 64)
    authorization = _authorization(signer, diff_hash=intent.diff_hash)

    outcome = await adapter.execute_write(intent, authorization, IdempotencyKey("key-1"))

    assert outcome.outcome == "SKIPPED_DRIFT"
    assert client.updates == []


async def test_broker_hard_cap_blocks(tmp_path: Path) -> None:
    signer, verifier = _keypair()
    client = _FakeGraphClient(nodes={"111": _CAMPAIGN_NODE})
    pipeline = _pipeline(tmp_path, verifier)
    adapter = MetaAdsAdapter(_config(), client, FixedClock(_NOW), write_pipeline=pipeline)
    entity_ref = _campaign_ref()
    intent = _intent(
        entity_ref,
        parametro="daily_budget",
        before={"amount": "5.00", "currency": "EUR"},
        after={"amount": "6.00", "currency": "EUR"},
        operation=WriteOperation.RAISE_BUDGET,
    )
    authorization = _authorization(signer, diff_hash=intent.diff_hash)
    stub_outcome = _succeeded_stub_outcome()
    for change in range(5):
        ledger = WriteLedgerStore(tmp_path / "write_ledger.sqlite3")
        ledger.begin_receipt(
            f"prior-{change}",
            intent,
            authorization.authorization_id,
            fake_scope(intent, _AD_ACCOUNT_ID),
            100,
        )
        ledger.close()
        pipeline.finalize(f"prior-{change}", _AD_ACCOUNT_ID, intent, stub_outcome, now=_NOW)

    outcome = await adapter.execute_write(intent, authorization, IdempotencyKey("key-new"))

    assert outcome.outcome == "BLOCKED_HARD_CAP"
    assert outcome.error_code == "max_changes_per_day_reached"
    assert client.updates == []


async def test_replay_returns_original_outcome(tmp_path: Path) -> None:
    signer, verifier = _keypair()
    client = _FakeGraphClient(nodes={"111": _CAMPAIGN_NODE})
    adapter = MetaAdsAdapter(
        _config(), client, FixedClock(_NOW), write_pipeline=_pipeline(tmp_path, verifier)
    )
    entity_ref = _campaign_ref()
    intent = _intent(entity_ref)
    authorization = _authorization(signer, diff_hash=intent.diff_hash)

    first = await adapter.execute_write(intent, authorization, IdempotencyKey("key-1"))
    second = await adapter.execute_write(intent, authorization, IdempotencyKey("key-1"))

    assert second == first
    assert client.updates == [("111", {"status": "PAUSED"})]


async def test_uncertain_provider_write_is_durable_unknown_and_not_retried(tmp_path: Path) -> None:
    signer, verifier = _keypair()
    client = _FakeGraphClient(
        nodes={"111": _CAMPAIGN_NODE}, update_error=TimeoutError("secret provider detail")
    )
    adapter = MetaAdsAdapter(
        _config(), client, FixedClock(_NOW), write_pipeline=_pipeline(tmp_path, verifier)
    )
    entity_ref = _campaign_ref()
    intent = _intent(entity_ref)
    authorization = _authorization(signer, diff_hash=intent.diff_hash)

    first = await adapter.execute_write(intent, authorization, IdempotencyKey("key-unknown"))
    client.update_error = None
    replay = await adapter.execute_write(intent, authorization, IdempotencyKey("key-unknown"))

    assert first.outcome == "UNKNOWN"
    assert first.error_code == "provider_outcome_unknown"
    assert "secret" not in str(first)
    assert replay == first
    assert client.updates == []


async def test_operation_not_supported_is_denied(tmp_path: Path) -> None:
    signer, verifier = _keypair()
    client = _FakeGraphClient(nodes={"111": _CAMPAIGN_NODE})
    adapter = MetaAdsAdapter(
        _config(), client, FixedClock(_NOW), write_pipeline=_pipeline(tmp_path, verifier)
    )
    entity_ref = _campaign_ref()
    intent = _intent(
        entity_ref,
        parametro="negative_keywords",
        before=None,
        after="palabra prohibida",
        operation=WriteOperation.ADD_NEGATIVE_KEYWORD,
    )
    authorization = _authorization(signer, diff_hash=intent.diff_hash)

    outcome = await adapter.execute_write(intent, authorization, IdempotencyKey("key-1"))

    assert outcome.outcome == "DENIED"
    assert outcome.error_code == "operation_not_supported"


def _succeeded_stub_outcome() -> WriteOutcome:
    return WriteOutcome(
        outcome="SUCCEEDED",
        applied_value={"amount": "6.00", "currency": "EUR"},
        state_hash_after="a" * 64,
        error_code=None,
        platform_request_id="req-1",
    )


async def test_run_gaql_is_not_implemented_on_meta() -> None:
    """GAQL es de Google Ads; Meta lo rechaza sin tocar el `GraphClient`
    (mismo tratamiento que `upload_asset`)."""
    adapter = MetaAdsAdapter(_config(), _FakeGraphClient(), FixedClock(_NOW))

    with pytest.raises(PlatformCapabilityNotImplementedError):
        await adapter.run_gaql(
            AccountRef(PlatformCode.META, _AD_ACCOUNT_ID),
            "SELECT campaign.id FROM campaign",
            max_rows=10,
        )


def _upload_request(**overrides: Any) -> AssetUploadRequest:
    defaults: dict[str, Any] = {
        "account_ref": AccountRef(PlatformCode.META, _AD_ACCOUNT_ID),
        "file_name": "asset-1",
        "mime_type": "image/jpeg",
        "media": b"\xff\xd8\xff\xe0fake-jpeg-bytes",
    }
    defaults.update(overrides)
    return AssetUploadRequest(**defaults)


async def test_upload_asset_returns_the_hash_and_the_meta_hosted_preview_url() -> None:
    client = _FakeGraphClient()
    adapter = MetaAdsAdapter(_config(), client, FixedClock(_NOW))

    handle = await adapter.upload_asset(_upload_request())

    assert handle.platform_asset_id == "img-hash-abc"
    assert handle.preview_url == "https://scontent.fmad1.fbcdn.net/x.jpg"
    assert client.create_image_calls == [
        (_AD_ACCOUNT_ID, "asset-1", b"\xff\xd8\xff\xe0fake-jpeg-bytes")
    ]


async def test_upload_asset_rejects_a_disallowed_mime_type_without_calling_the_provider() -> None:
    client = _FakeGraphClient()
    adapter = MetaAdsAdapter(_config(), client, FixedClock(_NOW))

    with pytest.raises(MetaAdsAdapterError):
        await adapter.upload_asset(_upload_request(mime_type="image/gif"))

    assert client.create_image_calls == []


async def test_upload_asset_rejects_media_over_the_size_limit_without_calling_the_provider() -> (
    None
):
    client = _FakeGraphClient()
    adapter = MetaAdsAdapter(_config(), client, FixedClock(_NOW))
    oversized = b"x" * (8 * 1024 * 1024 + 1)

    with pytest.raises(MetaAdsAdapterError):
        await adapter.upload_asset(_upload_request(media=oversized))

    assert client.create_image_calls == []


async def test_upload_asset_discards_a_preview_url_outside_the_meta_allowlist() -> None:
    """B4 (revision de seguridad): un adaptador/proveedor que devolviera
    una URL fuera de la lista blanca de Meta (SSRF, host ajeno) nunca la
    propaga como `preview_url` -- el manejador opaco (`hash`) sigue
    disponible, pero sin URL fotografiable el paso queda en `unknown`
    aguas arriba (`ChokepointStepExecutor`), nunca en `done` con una URL
    no fiable."""
    client = _FakeGraphClient(
        image_response={"hash": "img-hash-abc", "url": "http://169.254.169.254/x"}
    )
    adapter = MetaAdsAdapter(_config(), client, FixedClock(_NOW))

    handle = await adapter.upload_asset(_upload_request())

    assert handle.platform_asset_id == "img-hash-abc"
    assert handle.preview_url is None


async def test_upload_asset_discards_a_preview_url_that_is_a_bare_ip_literal() -> None:
    client = _FakeGraphClient(image_response={"hash": "img-hash-abc", "url": "https://1.2.3.4/x"})
    adapter = MetaAdsAdapter(_config(), client, FixedClock(_NOW))

    handle = await adapter.upload_asset(_upload_request())

    assert handle.preview_url is None


async def test_upload_asset_fails_when_the_provider_confirms_no_hash() -> None:
    client = _FakeGraphClient(image_response={"url": "https://graph.facebook.com/x"})
    adapter = MetaAdsAdapter(_config(), client, FixedClock(_NOW))

    with pytest.raises(MetaAdsAdapterError):
        await adapter.upload_asset(_upload_request())


async def test_read_reference_data_list_meta_pages_projects_whitelisted_fields() -> None:
    """004 tasks-2.md R3/I1: `list_meta_pages` pide la arista `promote_pages`
    sobre la cuenta (con `act_` antepuesto) y solo deja pasar los campos de
    la lista blanca de `meta_graph_reader.py`."""
    client = _FakeGraphClient(
        edges={
            (_AD_ACCOUNT_ID, "promote_pages"): [
                {"id": "p1", "name": "Pagina", "instagram_business_account": {"id": "ig1"}}
            ]
        }
    )
    adapter = MetaAdsAdapter(_config(), client, FixedClock(_NOW))

    rows = await adapter.read_reference_data(
        AccountRef(PlatformCode.META, _AD_ACCOUNT_ID), tool="list_meta_pages", arguments={}
    )

    assert rows == [{"id": "p1", "name": "Pagina", "instagram_business_account": {"id": "ig1"}}]


async def test_read_reference_data_list_meta_audiences_merges_custom_and_saved() -> None:
    client = _FakeGraphClient(
        edges={
            (_AD_ACCOUNT_ID, "customaudiences"): [{"id": "c1", "name": "Custom"}],
            (_AD_ACCOUNT_ID, "saved_audiences"): [{"id": "s1", "name": "Saved"}],
        }
    )
    adapter = MetaAdsAdapter(_config(), client, FixedClock(_NOW))

    rows = await adapter.read_reference_data(
        AccountRef(PlatformCode.META, _AD_ACCOUNT_ID), tool="list_meta_audiences", arguments={}
    )

    assert {"id": "c1", "name": "Custom", "kind": "custom"} in rows
    assert {"id": "s1", "name": "Saved", "kind": "saved"} in rows


async def test_read_reference_data_unknown_tool_fails_closed() -> None:
    adapter = MetaAdsAdapter(_config(), _FakeGraphClient(), FixedClock(_NOW))

    with pytest.raises(PlatformCapabilityNotImplementedError):
        await adapter.read_reference_data(
            AccountRef(PlatformCode.META, _AD_ACCOUNT_ID), tool="list_google_conversion_actions",
            arguments={},
        )


async def test_get_meta_graph_returns_node_when_it_is_the_account_itself() -> None:
    client = _FakeGraphClient(nodes={_AD_ACCOUNT_ID: {"id": _AD_ACCOUNT_ID, "name": "Cuenta"}})
    adapter = MetaAdsAdapter(_config(), client, FixedClock(_NOW))

    rows = await adapter.get_meta_graph(
        AccountRef(PlatformCode.META, _AD_ACCOUNT_ID),
        node=_AD_ACCOUNT_ID,
        edge="",
        fields=("id", "name"),
        params={},
    )

    assert rows == [{"id": _AD_ACCOUNT_ID, "name": "Cuenta"}]


async def test_get_meta_graph_edge_of_owned_node() -> None:
    client = _FakeGraphClient(
        nodes={f"{_AD_ACCOUNT_ID}/111": _CAMPAIGN_NODE},
        edges={(f"{_AD_ACCOUNT_ID}/111", "adsets"): [{"id": "222", "name": "Conjunto"}]},
    )
    adapter = MetaAdsAdapter(_config(), client, FixedClock(_NOW))

    rows = await adapter.get_meta_graph(
        AccountRef(PlatformCode.META, _AD_ACCOUNT_ID),
        node="111",
        edge="adsets",
        fields=("id", "name"),
        params={},
    )

    assert rows == [{"id": "222", "name": "Conjunto"}]


async def test_get_meta_graph_foreign_node_is_entity_not_found() -> None:
    """S-1 (004 tasks-2.md R5): un nodo de otra cuenta da el MISMO
    `EntityNotFoundError` que un nodo inexistente -- nunca se distingue."""
    foreign_node = {**_CAMPAIGN_NODE, "account_id": "999"}
    client = _FakeGraphClient(nodes={f"{_AD_ACCOUNT_ID}/111": foreign_node})
    adapter = MetaAdsAdapter(_config(), client, FixedClock(_NOW))

    with pytest.raises(EntityNotFoundError):
        await adapter.get_meta_graph(
            AccountRef(PlatformCode.META, _AD_ACCOUNT_ID),
            node="111",
            edge="",
            fields=("id",),
            params={},
        )


async def test_get_meta_graph_missing_node_is_entity_not_found_same_as_foreign() -> None:
    client = _FakeGraphClient()
    adapter = MetaAdsAdapter(_config(), client, FixedClock(_NOW))

    with pytest.raises(EntityNotFoundError):
        await adapter.get_meta_graph(
            AccountRef(PlatformCode.META, _AD_ACCOUNT_ID),
            node="999999",
            edge="",
            fields=("id",),
            params={},
        )


class _FakeAdLibraryClient:
    def __init__(self, ads: Sequence[Mapping[str, Any]] = ()) -> None:
        self.ads = ads
        self.calls: list[dict[str, Any]] = []

    def search_ads_archive(
        self,
        *,
        country: str,
        search_terms: str | None,
        search_page_ids: str | None,
        active_status: str,
        fields: Sequence[str],
        limit: int,
        external_account_id: str | None = None,
    ) -> Sequence[Mapping[str, Any]]:
        self.calls.append(
            {
                "country": country,
                "search_terms": search_terms,
                "search_page_ids": search_page_ids,
                "active_status": active_status,
                "fields": fields,
                "limit": limit,
                "external_account_id": external_account_id,
            }
        )
        return self.ads


async def test_search_ads_archive_without_client_fails_closed() -> None:
    adapter = MetaAdsAdapter(_config(), _FakeGraphClient(), FixedClock(_NOW))

    with pytest.raises(PlatformCapabilityNotImplementedError):
        await adapter.search_ads_archive(
            country="ES", search_terms="acme", search_page_ids=None, active_only=True
        )


async def test_search_ads_archive_maps_the_configured_client_response() -> None:
    ad_library = _FakeAdLibraryClient(
        ads=[
            {
                "page_name": "Acme",
                "ad_creative_bodies": ["Hola"],
                "publisher_platforms": ["facebook"],
            }
        ]
    )
    adapter = MetaAdsAdapter(
        _config(), _FakeGraphClient(), FixedClock(_NOW), ad_library_client=ad_library
    )

    rows = await adapter.search_ads_archive(
        country="ES", search_terms="acme", search_page_ids=None, active_only=True
    )

    assert rows[0]["advertiser_name"] == "Acme"


async def test_search_ads_archive_forwards_the_external_account_id_to_the_client() -> None:
    """fix/ad-library-over-composio: `ComposioMetaAdLibraryClient` needs the
    caller-resolved account to authenticate the Composio proxy -- the
    adapter must thread it through unchanged, never derive its own."""
    ad_library = _FakeAdLibraryClient(ads=[])
    adapter = MetaAdsAdapter(
        _config(), _FakeGraphClient(), FixedClock(_NOW), ad_library_client=ad_library
    )

    await adapter.search_ads_archive(
        country="ES",
        search_terms="acme",
        search_page_ids=None,
        active_only=True,
        external_account_id="act_123",
    )

    assert ad_library.calls[0]["external_account_id"] == "act_123"
    assert ad_library.calls[0]["active_status"] == "ACTIVE"


async def test_execute_write_native_write_updates_node_via_graph_client(tmp_path: Path) -> None:
    """W3 (historia 20, I1): `NATIVE_WRITE` pasa por el MISMO pipeline de
    autorizacion (tope duro, firma, idempotencia) y muta via
    `MetaGraphClient.update_node` -- ningun atajo."""
    signer, verifier = _keypair()
    client = _FakeGraphClient(nodes={"111": _CAMPAIGN_NODE})
    adapter = MetaAdsAdapter(
        _config(), client, FixedClock(_NOW), write_pipeline=_pipeline(tmp_path, verifier)
    )
    entity_ref = _campaign_ref()
    payload = {"custom_audiences": ["123"]}
    intent = _intent(
        entity_ref,
        parametro="native:meta:update_targeting",
        before=None,
        after=payload,
        operation=WriteOperation.NATIVE_WRITE,
    )
    authorization = _authorization(signer, diff_hash=intent.diff_hash)

    outcome = await adapter.execute_write(intent, authorization, IdempotencyKey("key-1"))

    assert outcome.outcome == "SUCCEEDED"
    assert client.updates == [("111", payload)]

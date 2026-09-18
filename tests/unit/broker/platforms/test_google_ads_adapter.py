"""`GoogleAdsAdapter` (T025/F2): SDK sustituido por `GoogleAdsSearchClient`
falso (contracts/platform-port.md: "SDK mocked in tests"). Cubre lecturas
(`test_gaql_rejects_mutate`, `test_sdk_error_redacted`,
`test_daily_ops_budget_throttles`) y los 8 controles de `execute_write`
sobre una plataforma real."""

from __future__ import annotations

import base64
import hashlib
from collections.abc import Iterator, Mapping
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from textwrap import dedent
from typing import Any
from uuid import uuid4

import pytest

from safent_ads.accounts.application.ports import (
    AccountRef,
    AssetUploadRequest,
    DateWindow,
    MetricGranularity,
    MetricsRequest,
    PlatformAssetHandle,
    SignedAuthorization,
    WriteIntent,
    WriteOperation,
    WriteOutcome,
)
from safent_ads.accounts.domain.ad_entity import AdEntityStatus
from safent_ads.accounts.domain.budget import BudgetKind
from safent_ads.accounts.domain.platform_state_hash import PlatformStateHash
from safent_ads.accounts.domain.refs import IdempotencyKey
from safent_ads.broker.domain.write_authorization import authorization_signing_payload
from safent_ads.broker.infrastructure.caps_config import parse_caps_config
from safent_ads.broker.infrastructure.write_ledger_store import WriteLedgerStore
from safent_ads.broker.platforms.errors import (
    DailyOperationBudgetExhaustedError,
    GaqlValidationError,
    PlatformCapabilityNotImplementedError,
)
from safent_ads.broker.platforms.google_ads_adapter import (
    GoogleAdsAdapter,
    GoogleAdsAdapterConfig,
    GoogleAdsAdapterError,
    GoogleAdsQueryTemplates,
    _map_generic_status,
)
from safent_ads.broker.platforms.google_asset_upload import MAX_IMAGE_UPLOAD_BYTES
from safent_ads.broker.platforms.write_pipeline import (
    PackageUploadDeniedError,
    WriteAuthorizationPipeline,
)
from safent_ads.proposals.domain.diff_hash import canonical_json_bytes, compute_diff_hash
from safent_ads.shared.clock import FixedClock
from safent_ads.shared.crypto.ed25519 import ApprovalSigner, ApprovalVerifier
from safent_ads.shared.ids import EntityLevel, EntityRef, PlatformCode
from tests.unit.broker.ledger_scope_fakes import fake_scope
from tests.unit.execution.test_campaign_creation_budget import creation_payload

_NOW = datetime(2026, 9, 9, 12, tzinfo=UTC)
_CUSTOMER_ID = "1234567890"
_CAMPAIGN_RESOURCE = f"customers/{_CUSTOMER_ID}/campaigns/111"
_AD_GROUP_RESOURCE = f"customers/{_CUSTOMER_ID}/adGroups/222"
_AD_RESOURCE = f"customers/{_CUSTOMER_ID}/adGroupAds/222~333"
_BUDGET_RESOURCE = f"customers/{_CUSTOMER_ID}/campaignBudgets/1"


def test_unknown_google_entity_status_fails_closed() -> None:
    with pytest.raises(GoogleAdsAdapterError, match="estado de entidad desconocido"):
        _map_generic_status("UNSPECIFIED")


_CAPS_YAML = dedent(
    f"""
    defaults:
      max_step_pct: 100
      max_changes_per_day: 5
      autonomy_enabled: true
    accounts:
      "{_CUSTOMER_ID}":
        daily_cap_minor: 100000000
        monthly_cap_minor: 1000000000
        floor_minor: 100
        ceiling_minor: 100000000
    """
)


class _FakeSearchClient:
    def __init__(
        self,
        rows_by_query: dict[str, list[Mapping[str, Any]]] | None = None,
        error: Exception | None = None,
        mutate_error: Exception | None = None,
    ) -> None:
        self.rows_by_query = rows_by_query or {}
        self.error = error
        self.mutate_error = mutate_error
        self.received_queries: list[str] = []
        self.budget_mutations: list[tuple[str, str, int]] = []
        self.status_mutations: list[tuple[str, str, EntityLevel, str]] = []
        self.negative_keyword_mutations: list[tuple[str, str, str]] = []

    def search_stream(
        self,
        customer_id: str,  # noqa: ARG002 - shape of the SDK port
        query: str,  # noqa: ARG002 - forma exacta del puerto
    ) -> Iterator[Mapping[str, Any]]:
        self.received_queries.append(query)
        if self.error is not None:
            raise self.error
        for prefix, rows in self.rows_by_query.items():
            if query.startswith(prefix):
                yield from rows
                return
        yield from ()

    def mutate_campaign_budget(
        self, customer_id: str, budget_resource_name: str, amount_micros: int
    ) -> str:
        if self.mutate_error is not None:
            raise self.mutate_error
        self.budget_mutations.append((customer_id, budget_resource_name, amount_micros))
        return budget_resource_name

    def mutate_status(
        self, customer_id: str, resource_name: str, level: EntityLevel, status: str
    ) -> str:
        if self.mutate_error is not None:
            raise self.mutate_error
        self.status_mutations.append((customer_id, resource_name, level, status))
        return resource_name

    def mutate_negative_keyword(
        self, customer_id: str, ad_group_resource_name: str, keyword_text: str
    ) -> str:
        if self.mutate_error is not None:
            raise self.mutate_error
        self.negative_keyword_mutations.append((customer_id, ad_group_resource_name, keyword_text))
        return f"{ad_group_resource_name}~1"


class _FakeTagManagerClient:
    def __init__(self) -> None:
        self.changes: list[tuple[str, Mapping[str, object]]] = []

    async def read(
        self,
        external_account_id: str,  # noqa: ARG002 - protocol shape
        *,
        resource: str,  # noqa: ARG002 - protocol shape
        parent_path: str | None,  # noqa: ARG002 - protocol shape
    ) -> Mapping[str, Any]:
        return {}

    async def apply_change(
        self, external_account_id: str, payload: Mapping[str, object]
    ) -> Mapping[str, Any]:
        self.changes.append((external_account_id, payload))
        return {"path": "accounts/1/containers/2/workspaces/3"}


def _config(**overrides: object) -> GoogleAdsAdapterConfig:
    defaults: dict[str, object] = {
        "client_id": "client-id",
        "client_secret": "client-secret",
        "refresh_token": "refresh-token",
        "login_customer_id": _CUSTOMER_ID,
    }
    defaults.update(overrides)
    return GoogleAdsAdapterConfig(**defaults)  # type: ignore[arg-type]


_CAMPAIGN_ROW: dict[str, Any] = {
    "campaign.resource_name": _CAMPAIGN_RESOURCE,
    "campaign.id": 111,
    "campaign.name": "Campana Otoño",
    "campaign.status": "ENABLED",
    "campaign_budget.resource_name": _BUDGET_RESOURCE,
    "campaign_budget.amount_micros": 5_000_000,
    "campaign_budget.type": "STANDARD",
    "campaign_budget.explicitly_shared": False,
    "customer.currency_code": "EUR",
}
_AD_GROUP_ROW: dict[str, Any] = {
    "ad_group.resource_name": _AD_GROUP_RESOURCE,
    "ad_group.id": 222,
    "ad_group.name": "Grupo A",
    "ad_group.status": "ENABLED",
    "ad_group.campaign": _CAMPAIGN_RESOURCE,
    "customer.currency_code": "EUR",
}
_AD_ROW: dict[str, Any] = {
    "ad_group_ad.resource_name": _AD_RESOURCE,
    "ad_group_ad.status": "ENABLED",
    "ad_group_ad.ad.id": 333,
    "ad_group_ad.ad_group": _AD_GROUP_RESOURCE,
    "customer.currency_code": "EUR",
}


async def test_fetch_account_inventory_maps_all_three_hierarchy_levels() -> None:
    client = _FakeSearchClient(
        rows_by_query={
            "SELECT campaign.resource_name": [_CAMPAIGN_ROW],
            "SELECT ad_group.resource_name": [_AD_GROUP_ROW],
            "SELECT ad_group_ad.resource_name": [_AD_ROW],
        }
    )
    adapter = GoogleAdsAdapter(_config(), client, FixedClock(_NOW))

    snapshots = await adapter.fetch_account_inventory(AccountRef(PlatformCode.GOOGLE, _CUSTOMER_ID))

    assert len(snapshots) == 3
    ad = next(s for s in snapshots if s.entity_ref.level == EntityLevel.AD)
    assert ad.entity_ref.external_id == _AD_RESOURCE
    assert ad.parent_ref.external_id == _AD_GROUP_RESOURCE
    assert ad.canonical_state == {
        k: v for k, v in _AD_ROW.items() if not k.endswith(".resource_name")
    }
    campaign = next(s for s in snapshots if s.entity_ref.level == EntityLevel.CAMPAIGN)
    assert campaign.entity_ref.external_id == _CAMPAIGN_RESOURCE
    assert campaign.status == AdEntityStatus.ACTIVE
    assert campaign.budget is not None
    assert campaign.budget.amount.minor_units == 500
    assert campaign.budget.kind == BudgetKind.DAILY


async def test_fetch_account_inventory_uses_explicit_shared_budget_flag() -> None:
    shared_row = {**_CAMPAIGN_ROW, "campaign_budget.explicitly_shared": True}
    client = _FakeSearchClient(
        rows_by_query={
            "SELECT campaign.resource_name": [shared_row],
            "SELECT ad_group.resource_name": [],
        }
    )
    adapter = GoogleAdsAdapter(_config(), client, FixedClock(_NOW))

    snapshots = await adapter.fetch_account_inventory(AccountRef(PlatformCode.GOOGLE, _CUSTOMER_ID))

    assert snapshots[0].budget is not None
    assert snapshots[0].budget.kind == BudgetKind.SHARED


def _dummy_templates(**overrides: str) -> GoogleAdsQueryTemplates:
    """Texto deliberadamente distinto de las constantes de produccion: si
    el adaptador ignorase `templates` y siguiera reconstruyendo la consulta
    desde `_CAMPAIGN_FIELDS`/`_build_select`, estas pruebas fallarian."""
    defaults: dict[str, str] = {
        "campaign_inventory": "SELECT campaign.resource_name FROM campaign",
        "ad_group_inventory": "SELECT ad_group.resource_name FROM ad_group",
        "ad_inventory": "SELECT ad_group_ad.resource_name FROM ad_group_ad",
        "campaign_metrics": (
            "SELECT campaign.resource_name, segments.date, metrics.cost_micros FROM campaign"
        ),
        "campaign_budget_lookup": "SELECT campaign_budget.resource_name FROM campaign",
    }
    defaults.update(overrides)
    return GoogleAdsQueryTemplates(**defaults)  # type: ignore[arg-type]


async def test_fetch_account_inventory_sends_the_injected_templates_verbatim() -> None:
    templates = _dummy_templates()
    client = _FakeSearchClient(
        rows_by_query={
            templates.campaign_inventory: [_CAMPAIGN_ROW],
            templates.ad_group_inventory: [_AD_GROUP_ROW],
        }
    )
    adapter = GoogleAdsAdapter(_config(), client, FixedClock(_NOW), templates=templates)

    await adapter.fetch_account_inventory(AccountRef(PlatformCode.GOOGLE, _CUSTOMER_ID))

    assert client.received_queries == [
        templates.campaign_inventory,
        templates.ad_group_inventory,
        templates.ad_inventory,
    ]


async def test_read_entity_state_uses_the_injected_template_with_a_safe_resource_name() -> None:
    templates = _dummy_templates()
    client = _FakeSearchClient(rows_by_query={templates.campaign_inventory: [_CAMPAIGN_ROW]})
    adapter = GoogleAdsAdapter(_config(), client, FixedClock(_NOW), templates=templates)

    await adapter.read_entity_state(
        EntityRef(PlatformCode.GOOGLE, EntityLevel.CAMPAIGN, _CAMPAIGN_RESOURCE)
    )

    assert client.received_queries == [
        f"{templates.campaign_inventory} AND campaign.resource_name = '{_CAMPAIGN_RESOURCE}'"
    ]


async def test_read_entity_state_rejects_hostile_resource_names_with_injected_templates() -> None:
    """El sustento de seguridad no cambia con T162: el nombre de recurso se
    valida ANTES de entrar en el literal, nunca se interpola sin filtro
    aunque la base de la consulta ahora venga de una plantilla inyectada."""
    client = _FakeSearchClient()
    adapter = GoogleAdsAdapter(_config(), client, FixedClock(_NOW), templates=_dummy_templates())

    with pytest.raises(GoogleAdsAdapterError):
        await adapter.read_entity_state(
            EntityRef(PlatformCode.GOOGLE, EntityLevel.CAMPAIGN, "'; DROP TABLE campaign; --")
        )

    assert client.received_queries == []


async def test_campaign_budget_lookup_uses_the_injected_template_verbatim() -> None:
    templates = _dummy_templates()
    client = _FakeSearchClient(
        rows_by_query={
            (
                f"{templates.campaign_budget_lookup} "
                f"WHERE campaign.resource_name = '{_CAMPAIGN_RESOURCE}'"
            ): [{"campaign_budget.resource_name": _BUDGET_RESOURCE}]
        }
    )
    adapter = GoogleAdsAdapter(_config(), client, FixedClock(_NOW), templates=templates)

    resource_name = await adapter._campaign_budget_resource_name(_CUSTOMER_ID, _CAMPAIGN_RESOURCE)

    assert resource_name == _BUDGET_RESOURCE
    assert client.received_queries == [
        f"{templates.campaign_budget_lookup} WHERE campaign.resource_name = '{_CAMPAIGN_RESOURCE}'"
    ]


async def test_fetch_metrics_applies_hourly_granularity_to_the_injected_template() -> None:
    templates = _dummy_templates(
        campaign_metrics=(
            "SELECT campaign.resource_name, segments.date, metrics.cost_micros FROM campaign"
        )
    )
    client = _FakeSearchClient(rows_by_query={"SELECT campaign.resource_name": []})
    adapter = GoogleAdsAdapter(_config(), client, FixedClock(_NOW), templates=templates)
    request = MetricsRequest(
        account_ref=AccountRef(PlatformCode.GOOGLE, _CUSTOMER_ID),
        window=DateWindow(start=date(2026, 9, 8), end=date(2026, 9, 8)),
        granularity=MetricGranularity.HOURLY,
    )

    await adapter.fetch_metrics(request)

    assert client.received_queries == [
        "SELECT campaign.resource_name, segments.date, segments.hour, metrics.cost_micros "
        "FROM campaign WHERE segments.date BETWEEN '2026-09-08' AND '2026-09-08'"
    ]


async def test_default_templates_match_the_versioned_gaql_files() -> None:
    """Sin `templates=` explicito, el adaptador cae en
    `GoogleAdsQueryTemplates.from_inline_constants()` -- el mismo texto que
    `load_gaql_template` produce a partir de `broker/platforms/gaql/*.gaql`
    (`tests/unit/composition/test_gaql_templates.py` ya lo prueba fichero a
    fichero); esta prueba comprueba que el adaptador de verdad usa ese
    valor por defecto cuando `composition/broker.py` no interviene."""
    default_templates = GoogleAdsQueryTemplates.from_inline_constants()
    client = _FakeSearchClient(
        rows_by_query={default_templates.campaign_inventory: [_CAMPAIGN_ROW]}
    )
    adapter = GoogleAdsAdapter(_config(), client, FixedClock(_NOW))

    await adapter.read_entity_state(
        EntityRef(PlatformCode.GOOGLE, EntityLevel.CAMPAIGN, _CAMPAIGN_RESOURCE)
    )

    expected_query = (
        f"{default_templates.campaign_inventory} AND campaign.resource_name = "
        f"'{_CAMPAIGN_RESOURCE}'"
    )
    assert client.received_queries == [expected_query]


async def test_gaql_rejects_mutate() -> None:
    """El adaptador nunca ejecuta una consulta que no sea SELECT-only,
    aunque el `search_client` inyectado la aceptaria sin rechistar."""
    client = _FakeSearchClient()
    adapter = GoogleAdsAdapter(_config(), client, FixedClock(_NOW))

    with pytest.raises(GaqlValidationError):
        await adapter._run_gaql(_CUSTOMER_ID, "UPDATE campaign SET status = 'PAUSED'")

    assert client.received_queries == []


async def test_sdk_error_redacted() -> None:
    error = RuntimeError("client_secret: 'super-secret-client-secret', customer 123-456-7890")
    client = _FakeSearchClient(error=error)
    adapter = GoogleAdsAdapter(_config(), client, FixedClock(_NOW))

    with pytest.raises(GoogleAdsAdapterError) as exc_info:
        await adapter._run_gaql(_CUSTOMER_ID, "SELECT campaign.id FROM campaign")

    assert "super-secret-client-secret" not in str(exc_info.value)
    assert "123-456-7890" not in str(exc_info.value)


async def test_daily_ops_budget_throttles() -> None:
    client = _FakeSearchClient(rows_by_query={"SELECT campaign.id": []})
    adapter = GoogleAdsAdapter(_config(daily_operation_budget=1), client, FixedClock(_NOW))

    await adapter._run_gaql(_CUSTOMER_ID, "SELECT campaign.id FROM campaign")

    with pytest.raises(DailyOperationBudgetExhaustedError):
        await adapter._run_gaql(_CUSTOMER_ID, "SELECT campaign.id FROM campaign")


async def test_run_gaql_returns_rows_for_the_accounts_customer_id() -> None:
    client = _FakeSearchClient(rows_by_query={"SELECT campaign.id": [{"campaign.id": "111"}]})
    adapter = GoogleAdsAdapter(_config(), client, FixedClock(_NOW))

    rows = await adapter.run_gaql(
        AccountRef(PlatformCode.GOOGLE, _CUSTOMER_ID),
        "SELECT campaign.id FROM campaign",
        max_rows=10,
    )

    assert rows == [{"campaign.id": "111"}]
    assert client.received_queries == ["SELECT campaign.id FROM campaign"]


async def test_run_gaql_caps_rows_at_the_requested_max() -> None:
    rows = [{"campaign.id": str(i)} for i in range(5)]
    client = _FakeSearchClient(rows_by_query={"SELECT campaign.id": rows})
    adapter = GoogleAdsAdapter(_config(), client, FixedClock(_NOW))

    result = await adapter.run_gaql(
        AccountRef(PlatformCode.GOOGLE, _CUSTOMER_ID),
        "SELECT campaign.id FROM campaign",
        max_rows=2,
    )

    assert len(result) == 2


async def test_run_gaql_never_exceeds_the_instance_ceiling_even_if_asked() -> None:
    rows = [{"campaign.id": str(i)} for i in range(5)]
    client = _FakeSearchClient(rows_by_query={"SELECT campaign.id": rows})
    adapter = GoogleAdsAdapter(_config(), client, FixedClock(_NOW), max_rows_per_query=3)

    result = await adapter.run_gaql(
        AccountRef(PlatformCode.GOOGLE, _CUSTOMER_ID),
        "SELECT campaign.id FROM campaign",
        max_rows=1_000,
    )

    assert len(result) == 3


async def test_run_gaql_rejects_mutate_without_touching_the_sdk() -> None:
    client = _FakeSearchClient()
    adapter = GoogleAdsAdapter(_config(), client, FixedClock(_NOW))

    with pytest.raises(GaqlValidationError):
        await adapter.run_gaql(
            AccountRef(PlatformCode.GOOGLE, _CUSTOMER_ID),
            "UPDATE campaign SET status = 'PAUSED'",
            max_rows=10,
        )

    assert client.received_queries == []


async def test_read_entity_state_filters_by_resource_name() -> None:
    client = _FakeSearchClient(rows_by_query={"SELECT campaign.resource_name": [_CAMPAIGN_ROW]})
    adapter = GoogleAdsAdapter(_config(), client, FixedClock(_NOW))

    snapshot = await adapter.read_entity_state(
        EntityRef(PlatformCode.GOOGLE, EntityLevel.CAMPAIGN, _CAMPAIGN_RESOURCE)
    )

    assert snapshot.status == AdEntityStatus.ACTIVE
    assert f"'{_CAMPAIGN_RESOURCE}'" in client.received_queries[0]


async def test_read_entity_state_rejects_malformed_resource_name() -> None:
    client = _FakeSearchClient()
    adapter = GoogleAdsAdapter(_config(), client, FixedClock(_NOW))

    with pytest.raises(GoogleAdsAdapterError):
        await adapter.read_entity_state(
            EntityRef(PlatformCode.GOOGLE, EntityLevel.CAMPAIGN, "'; DROP TABLE campaign; --")
        )


async def test_fetch_metrics_builds_daily_query() -> None:
    metric_row = {
        "campaign.resource_name": _CAMPAIGN_RESOURCE,
        "segments.date": "2026-09-08",
        "metrics.cost_micros": 4_500_000,
        "metrics.impressions": 1000,
        "metrics.clicks": 40,
        "metrics.conversions": 3.0,
        "metrics.conversions_value": 150.0,
        "metrics.search_budget_lost_impression_share": 0.1,
        "metrics.search_rank_lost_impression_share": 0.05,
        "customer.currency_code": "EUR",
    }
    client = _FakeSearchClient(rows_by_query={"SELECT campaign.resource_name": [metric_row]})
    adapter = GoogleAdsAdapter(_config(), client, FixedClock(_NOW))
    request = MetricsRequest(
        account_ref=AccountRef(PlatformCode.GOOGLE, _CUSTOMER_ID),
        window=DateWindow(start=date(2026, 9, 8), end=date(2026, 9, 8)),
        granularity=MetricGranularity.DAILY,
    )

    facts = await adapter.fetch_metrics(request)

    assert len(facts) == 1
    assert facts[0].spend.minor_units == 450
    assert facts[0].conversion_value is not None
    assert facts[0].conversion_value.minor_units == 15_000
    assert facts[0].stat_date == date(2026, 9, 8)


async def test_inventory_and_prewrite_read_hash_the_same_remote_state() -> None:
    client = _FakeSearchClient(
        rows_by_query={
            "SELECT campaign.resource_name": [_CAMPAIGN_ROW],
            "SELECT ad_group.resource_name": [_AD_GROUP_ROW],
        }
    )
    adapter = GoogleAdsAdapter(_config(), client, FixedClock(_NOW))
    inventory = await adapter.fetch_account_inventory(AccountRef(PlatformCode.GOOGLE, _CUSTOMER_ID))
    for entity in inventory:
        fresh = await adapter.read_entity_state(entity.entity_ref)
        assert PlatformStateHash.compute(entity.canonical_state) == PlatformStateHash.compute(
            fresh.canonical_state
        )


# ---------------------------------------------------------------------------
# execute_write (F2): los 8 controles de contracts/platform-port.md
# ---------------------------------------------------------------------------


def _keypair(seed_byte: bytes = b"1") -> tuple[ApprovalSigner, ApprovalVerifier]:
    seed_b64 = base64.b64encode(seed_byte * 32).decode()
    signer = ApprovalSigner.from_seed_b64(seed_b64)
    return signer, ApprovalVerifier.from_public_key_b64(signer.public_key_b64())


def _pipeline(tmp_path: Path, verifier: ApprovalVerifier) -> WriteAuthorizationPipeline:
    caps = parse_caps_config(_CAPS_YAML)
    ledger = WriteLedgerStore(tmp_path / "write_ledger.sqlite3")
    return WriteAuthorizationPipeline(verifier, caps, ledger, scope_resolver=fake_scope)


def _campaign_state_hash() -> str:
    canonical_state = {
        key: value for key, value in _CAMPAIGN_ROW.items() if not key.endswith(".resource_name")
    }
    return PlatformStateHash.compute(canonical_state).value


def _intent(
    entity_ref: EntityRef,
    *,
    parametro: str = "status",
    before: object = "ENABLED",
    after: object = "PAUSED",
    operation: WriteOperation = WriteOperation.PAUSE,
    expected_state_hash: str = _campaign_state_hash(),
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


def _campaign_client(**kwargs: object) -> _FakeSearchClient:
    return _FakeSearchClient(
        rows_by_query={"SELECT campaign.resource_name": [_CAMPAIGN_ROW]},
        **kwargs,  # type: ignore[arg-type]
    )


async def test_execute_write_denied_when_pipeline_not_wired() -> None:
    """Sin `write_pipeline` (composicion aun no lo cablea, plan.md §10) el
    adaptador deniega por diseno -- nunca muta a ciegas."""
    adapter = GoogleAdsAdapter(_config(), _campaign_client(), FixedClock(_NOW))
    entity_ref = EntityRef(PlatformCode.GOOGLE, EntityLevel.CAMPAIGN, _CAMPAIGN_RESOURCE)
    intent = _intent(entity_ref)
    signer, _ = _keypair()
    authorization = _authorization(signer, diff_hash=intent.diff_hash)

    outcome = await adapter.execute_write(intent, authorization, IdempotencyKey("key-1"))

    assert outcome.outcome == "DENIED"
    assert outcome.error_code == "write_path_not_wired"


async def test_execute_write_pauses_a_campaign(tmp_path: Path) -> None:
    signer, verifier = _keypair()
    client = _campaign_client()
    adapter = GoogleAdsAdapter(
        _config(), client, FixedClock(_NOW), write_pipeline=_pipeline(tmp_path, verifier)
    )
    entity_ref = EntityRef(PlatformCode.GOOGLE, EntityLevel.CAMPAIGN, _CAMPAIGN_RESOURCE)
    intent = _intent(entity_ref)
    authorization = _authorization(signer, diff_hash=intent.diff_hash)

    outcome = await adapter.execute_write(intent, authorization, IdempotencyKey("key-1"))

    assert outcome.outcome == "SUCCEEDED"
    assert outcome.applied_value == "PAUSED"
    assert outcome.state_hash_after is not None
    assert client.status_mutations == [
        (_CUSTOMER_ID, _CAMPAIGN_RESOURCE, EntityLevel.CAMPAIGN, "PAUSED")
    ]


async def test_execute_write_applies_approved_gtm_change_through_pipeline(tmp_path: Path) -> None:
    signer, verifier = _keypair()
    client = _campaign_client()
    tag_manager = _FakeTagManagerClient()
    adapter = GoogleAdsAdapter(
        _config(),
        client,
        FixedClock(_NOW),
        write_pipeline=_pipeline(tmp_path, verifier),
        tag_manager_client=tag_manager,
    )
    entity_ref = EntityRef(PlatformCode.GOOGLE, EntityLevel.CAMPAIGN, _CAMPAIGN_RESOURCE)
    payload = {
        "action": "create_workspace",
        "parent_path": "accounts/1/containers/2",
        "body_encoded": base64.b64encode(b'{"name":"Friendog release"}').decode(),
    }
    intent = _intent(
        entity_ref,
        parametro="native:google:gtm_change",
        before=None,
        after=payload,
        operation=WriteOperation.NATIVE_WRITE,
    )
    authorization = _authorization(signer, diff_hash=intent.diff_hash)

    outcome = await adapter.execute_write(intent, authorization, IdempotencyKey("gtm-key-1"))

    assert outcome.outcome == "SUCCEEDED"
    assert outcome.platform_request_id == "accounts/1/containers/2/workspaces/3"
    assert tag_manager.changes == [(_CUSTOMER_ID, payload)]


async def test_execute_write_deletes_a_campaign(tmp_path: Path) -> None:
    """design.md §0.7: Google Ads no tiene una
    mutacion `remove` propia -- se borra poniendo `campaign.status =
    REMOVED`, mismo `mutate_status` que pausar/reanudar. El vocabulario
    firmado (`WriteIntent.valor_propuesto`) es el canonico de dominio
    (`DELETED`, ver `broker.domain.operation_semantics`), no el de la API
    de Google: el adaptador decide `REMOVED` solo a partir de la
    `operation`, igual que ya hace con PAUSE/RESUME."""
    signer, verifier = _keypair()
    client = _campaign_client()
    adapter = GoogleAdsAdapter(
        _config(), client, FixedClock(_NOW), write_pipeline=_pipeline(tmp_path, verifier)
    )
    entity_ref = EntityRef(PlatformCode.GOOGLE, EntityLevel.CAMPAIGN, _CAMPAIGN_RESOURCE)
    intent = _intent(entity_ref, after="DELETED", operation=WriteOperation.DELETE)
    authorization = _authorization(signer, diff_hash=intent.diff_hash)

    outcome = await adapter.execute_write(intent, authorization, IdempotencyKey("key-1"))

    assert outcome.outcome == "SUCCEEDED"
    assert outcome.applied_value == "DELETED"
    assert outcome.state_hash_after is not None
    assert client.status_mutations == [
        (_CUSTOMER_ID, _CAMPAIGN_RESOURCE, EntityLevel.CAMPAIGN, "REMOVED")
    ]


async def test_execute_write_lowers_a_campaign_budget(tmp_path: Path) -> None:
    signer, verifier = _keypair()
    client = _FakeSearchClient(
        rows_by_query={
            "SELECT campaign_budget.resource_name": [_CAMPAIGN_ROW],
            "SELECT campaign.resource_name": [_CAMPAIGN_ROW],
        }
    )
    adapter = GoogleAdsAdapter(
        _config(), client, FixedClock(_NOW), write_pipeline=_pipeline(tmp_path, verifier)
    )
    entity_ref = EntityRef(PlatformCode.GOOGLE, EntityLevel.CAMPAIGN, _CAMPAIGN_RESOURCE)
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
    assert client.budget_mutations == [(_CUSTOMER_ID, _BUDGET_RESOURCE, 3_500_000)]


async def test_add_negative_keyword_mutates_the_ad_group(tmp_path: Path) -> None:
    signer, verifier = _keypair()
    client = _FakeSearchClient(rows_by_query={"SELECT ad_group.resource_name": [_AD_GROUP_ROW]})
    adapter = GoogleAdsAdapter(
        _config(), client, FixedClock(_NOW), write_pipeline=_pipeline(tmp_path, verifier)
    )
    entity_ref = EntityRef(PlatformCode.GOOGLE, EntityLevel.AD_SET, _AD_GROUP_RESOURCE)
    ad_group_state_hash = PlatformStateHash.compute(
        {
            "ad_group.id": 222,
            "ad_group.name": "Grupo A",
            "ad_group.status": "ENABLED",
            "ad_group.campaign": _CAMPAIGN_RESOURCE,
            "customer.currency_code": "EUR",
        }
    ).value
    intent = _intent(
        entity_ref,
        parametro="negative_keywords",
        before=None,
        after="palabra prohibida",
        operation=WriteOperation.ADD_NEGATIVE_KEYWORD,
        expected_state_hash=ad_group_state_hash,
    )
    authorization = _authorization(signer, diff_hash=intent.diff_hash)

    outcome = await adapter.execute_write(intent, authorization, IdempotencyKey("key-1"))

    assert outcome.outcome == "SUCCEEDED"
    assert client.negative_keyword_mutations == [
        (_CUSTOMER_ID, _AD_GROUP_RESOURCE, "palabra prohibida")
    ]


async def test_rotate_out_creative_pauses_the_ad(tmp_path: Path) -> None:
    signer, verifier = _keypair()
    client = _FakeSearchClient(rows_by_query={"SELECT ad_group_ad.resource_name": [_AD_ROW]})
    adapter = GoogleAdsAdapter(
        _config(), client, FixedClock(_NOW), write_pipeline=_pipeline(tmp_path, verifier)
    )
    entity_ref = EntityRef(PlatformCode.GOOGLE, EntityLevel.AD, _AD_RESOURCE)
    ad_state_hash = PlatformStateHash.compute(
        {
            "ad_group_ad.status": "ENABLED",
            "ad_group_ad.ad.id": 333,
            "ad_group_ad.ad_group": _AD_GROUP_RESOURCE,
            "customer.currency_code": "EUR",
        }
    ).value
    intent = _intent(
        entity_ref,
        parametro="creative",
        before="ENABLED",
        after="PAUSED",
        operation=WriteOperation.ROTATE_OUT_CREATIVE,
        expected_state_hash=ad_state_hash,
    )
    authorization = _authorization(signer, diff_hash=intent.diff_hash)

    outcome = await adapter.execute_write(intent, authorization, IdempotencyKey("key-1"))

    assert outcome.outcome == "SUCCEEDED"
    assert client.status_mutations == [(_CUSTOMER_ID, _AD_RESOURCE, EntityLevel.AD, "PAUSED")]


async def test_signature_mismatch_denied(tmp_path: Path) -> None:
    signer, _own_verifier = _keypair(b"1")
    _, other_verifier = _keypair(b"2")
    client = _campaign_client()
    adapter = GoogleAdsAdapter(
        _config(), client, FixedClock(_NOW), write_pipeline=_pipeline(tmp_path, other_verifier)
    )
    entity_ref = EntityRef(PlatformCode.GOOGLE, EntityLevel.CAMPAIGN, _CAMPAIGN_RESOURCE)
    intent = _intent(entity_ref)
    authorization = _authorization(signer, diff_hash=intent.diff_hash)

    outcome = await adapter.execute_write(intent, authorization, IdempotencyKey("key-1"))

    assert outcome.outcome == "DENIED"
    assert outcome.error_code == "invalid_signature"
    assert client.status_mutations == []


async def test_expired_authorization_denied(tmp_path: Path) -> None:
    signer, verifier = _keypair()
    client = _campaign_client()
    adapter = GoogleAdsAdapter(
        _config(), client, FixedClock(_NOW), write_pipeline=_pipeline(tmp_path, verifier)
    )
    entity_ref = EntityRef(PlatformCode.GOOGLE, EntityLevel.CAMPAIGN, _CAMPAIGN_RESOURCE)
    intent = _intent(entity_ref)
    authorization = _authorization(
        signer, diff_hash=intent.diff_hash, expires_at=_NOW - timedelta(minutes=1)
    )

    outcome = await adapter.execute_write(intent, authorization, IdempotencyKey("key-1"))

    assert outcome.outcome == "DENIED"
    assert outcome.error_code == "authorization_expired"
    assert client.status_mutations == []


async def test_rule_authorization_cannot_raise_budget(tmp_path: Path) -> None:
    signer, verifier = _keypair()
    client = _campaign_client()
    adapter = GoogleAdsAdapter(
        _config(), client, FixedClock(_NOW), write_pipeline=_pipeline(tmp_path, verifier)
    )
    entity_ref = EntityRef(PlatformCode.GOOGLE, EntityLevel.CAMPAIGN, _CAMPAIGN_RESOURCE)
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
    assert client.budget_mutations == []


async def test_drift_skips_write(tmp_path: Path) -> None:
    signer, verifier = _keypair()
    client = _campaign_client()
    adapter = GoogleAdsAdapter(
        _config(), client, FixedClock(_NOW), write_pipeline=_pipeline(tmp_path, verifier)
    )
    entity_ref = EntityRef(PlatformCode.GOOGLE, EntityLevel.CAMPAIGN, _CAMPAIGN_RESOURCE)
    intent = _intent(entity_ref, expected_state_hash="d" * 64)
    authorization = _authorization(signer, diff_hash=intent.diff_hash)

    outcome = await adapter.execute_write(intent, authorization, IdempotencyKey("key-1"))

    assert outcome.outcome == "SKIPPED_DRIFT"
    assert client.status_mutations == []


async def test_broker_hard_cap_blocks(tmp_path: Path) -> None:
    signer, verifier = _keypair()
    client = _campaign_client()
    pipeline = _pipeline(tmp_path, verifier)
    adapter = GoogleAdsAdapter(_config(), client, FixedClock(_NOW), write_pipeline=pipeline)
    entity_ref = EntityRef(PlatformCode.GOOGLE, EntityLevel.CAMPAIGN, _CAMPAIGN_RESOURCE)
    intent = _intent(
        entity_ref,
        parametro="daily_budget",
        before={"amount": "5.00", "currency": "EUR"},
        after={"amount": "6.00", "currency": "EUR"},
        operation=WriteOperation.RAISE_BUDGET,
    )
    authorization = _authorization(signer, diff_hash=intent.diff_hash)
    for change in range(5):
        # Seed historical authorized receipts, independently of today's admission.
        ledger = WriteLedgerStore(tmp_path / "write_ledger.sqlite3")
        ledger.begin_receipt(
            f"prior-{change}",
            intent,
            authorization.authorization_id,
            fake_scope(intent, _CUSTOMER_ID),
            100,
        )
        ledger.close()
        pipeline.finalize(
            f"prior-{change}",
            _CUSTOMER_ID,
            intent,
            _succeeded_stub_outcome(),
            now=_NOW,
        )

    outcome = await adapter.execute_write(intent, authorization, IdempotencyKey("key-new"))

    assert outcome.outcome == "BLOCKED_HARD_CAP"
    assert outcome.error_code == "max_changes_per_day_reached"
    assert client.budget_mutations == []


async def test_replay_returns_original_outcome(tmp_path: Path) -> None:
    signer, verifier = _keypair()
    client = _campaign_client()
    adapter = GoogleAdsAdapter(
        _config(), client, FixedClock(_NOW), write_pipeline=_pipeline(tmp_path, verifier)
    )
    entity_ref = EntityRef(PlatformCode.GOOGLE, EntityLevel.CAMPAIGN, _CAMPAIGN_RESOURCE)
    intent = _intent(entity_ref)
    authorization = _authorization(signer, diff_hash=intent.diff_hash)

    first = await adapter.execute_write(intent, authorization, IdempotencyKey("key-1"))
    second = await adapter.execute_write(intent, authorization, IdempotencyKey("key-1"))

    assert second == first
    assert client.status_mutations == [
        (_CUSTOMER_ID, _CAMPAIGN_RESOURCE, EntityLevel.CAMPAIGN, "PAUSED")
    ]


async def test_uncertain_provider_write_is_durable_unknown_and_not_retried(tmp_path: Path) -> None:
    signer, verifier = _keypair()
    client = _campaign_client(mutate_error=TimeoutError("secret provider detail"))
    adapter = GoogleAdsAdapter(
        _config(), client, FixedClock(_NOW), write_pipeline=_pipeline(tmp_path, verifier)
    )
    entity_ref = EntityRef(PlatformCode.GOOGLE, EntityLevel.CAMPAIGN, _CAMPAIGN_RESOURCE)
    intent = _intent(entity_ref)
    authorization = _authorization(signer, diff_hash=intent.diff_hash)

    first = await adapter.execute_write(intent, authorization, IdempotencyKey("key-unknown"))
    client.mutate_error = None
    replay = await adapter.execute_write(intent, authorization, IdempotencyKey("key-unknown"))

    assert first.outcome == "UNKNOWN"
    assert first.error_code == "provider_outcome_unknown"
    assert "secret" not in str(first)
    assert replay == first
    assert client.status_mutations == []


async def test_operation_not_supported_is_denied(tmp_path: Path) -> None:
    signer, verifier = _keypair()
    client = _campaign_client()
    adapter = GoogleAdsAdapter(
        _config(), client, FixedClock(_NOW), write_pipeline=_pipeline(tmp_path, verifier)
    )
    entity_ref = EntityRef(PlatformCode.GOOGLE, EntityLevel.CAMPAIGN, _CAMPAIGN_RESOURCE)
    intent = _intent(
        entity_ref,
        parametro="bid_target",
        before="1.00",
        after="1.50",
        operation=WriteOperation.SET_BID_TARGET,
    )
    authorization = _authorization(signer, diff_hash=intent.diff_hash)

    outcome = await adapter.execute_write(intent, authorization, IdempotencyKey("key-1"))

    assert outcome.outcome == "DENIED"
    assert outcome.error_code == "operation_not_supported"


class _SearchCreationClientWithoutConversionGoalVerification:
    """A `GoogleAdsSearchClient` double shaped like a legacy Search-only
    creation client (`campaign_creation_currency` + `create_paused_campaign`
    only) -- the same shape as the real unix-socket broker's fake native
    client in `test_campaign_creation_path.py`, which never implements
    `verify_conversion_goals` because a Search plan never carries signed
    `conversion_goals` (T032 added that field for Performance Max only)."""

    def campaign_creation_currency(self, customer_id: str) -> str:  # noqa: ARG002
        return "EUR"

    def create_paused_campaign(
        self, customer_id: str, payload: Mapping[str, Any]
    ) -> Mapping[str, Any]:
        assert payload["creation_plan"]["status"] == "PAUSED"
        return {"campaign_resource": f"customers/{customer_id}/campaigns/456", "status": "PAUSED"}


async def test_execute_write_creates_a_search_campaign_without_a_conversion_goal_verifier(
    tmp_path: Path,
) -> None:
    """Regression (spec 005 T032 merge): `execute_write` used to read
    `search_client.verify_conversion_goals` unconditionally to build the
    `create_paused_campaign` call, so ANY search client missing that method
    -- like this one, or the real broker's fake native client in
    `test_campaign_creation_path.py::test_real_socket_broker_receipt_path`
    -- raised `AttributeError` before `create_paused_campaign` ever ran its
    own guard (`if resource_names and verify_conversion_goals is not None`),
    turning a valid Search creation into a fail-closed `UNKNOWN` instead of
    `SUCCEEDED`."""
    signer, verifier = _keypair()
    client = _SearchCreationClientWithoutConversionGoalVerification()
    adapter = GoogleAdsAdapter(
        _config(), client, FixedClock(_NOW), write_pipeline=_pipeline(tmp_path, verifier)
    )
    payload = creation_payload()
    entity_ref = EntityRef.parse(f"google:account:{uuid4()}:{uuid4()}:{_CUSTOMER_ID}")
    diff_hash = compute_diff_hash(entity_ref, "new_campaign:explicit", None, payload)
    intent = WriteIntent(
        entity_ref,
        WriteOperation.CREATE_CAMPAIGN,
        "new_campaign:explicit",
        None,
        payload,
        diff_hash,
        "",
        str(entity_ref.business_id),
    )
    authorization = _authorization(signer, diff_hash=intent.diff_hash)

    outcome = await adapter.execute_write(intent, authorization, IdempotencyKey("key-1"))

    assert outcome.outcome == "SUCCEEDED"


def _succeeded_stub_outcome() -> WriteOutcome:
    return WriteOutcome(
        outcome="SUCCEEDED",
        applied_value={"amount": "6.00", "currency": "EUR"},
        state_hash_after="a" * 64,
        error_code=None,
        platform_request_id="req-1",
    )


class _FakeKeywordIdeaClient:
    def __init__(self, rows: list[Mapping[str, Any]] | None = None) -> None:
        self.rows = rows or []
        self.calls: list[dict[str, Any]] = []

    def generate_keyword_ideas(
        self,
        customer_id: str,
        *,
        seed_keywords: Any,
        geo_target_constant: str,
        language_constant: str,
        limit: int,
    ) -> list[Mapping[str, Any]]:
        self.calls.append(
            {
                "customer_id": customer_id,
                "seed_keywords": tuple(seed_keywords),
                "geo_target_constant": geo_target_constant,
                "language_constant": language_constant,
                "limit": limit,
            }
        )
        return self.rows


async def test_read_reference_data_without_client_fails_closed() -> None:
    adapter = GoogleAdsAdapter(_config(), _FakeSearchClient(), FixedClock(_NOW))

    with pytest.raises(PlatformCapabilityNotImplementedError):
        await adapter.read_reference_data(
            AccountRef(PlatformCode.GOOGLE, _CUSTOMER_ID),
            tool="get_google_keyword_ideas",
            arguments={"seed_keywords": ["perros"], "geo_target": "2724", "language": "1003"},
        )


async def test_read_reference_data_rejects_unknown_tool() -> None:
    keyword_client = _FakeKeywordIdeaClient()
    adapter = GoogleAdsAdapter(
        _config(), _FakeSearchClient(), FixedClock(_NOW), keyword_idea_client=keyword_client
    )

    with pytest.raises(PlatformCapabilityNotImplementedError):
        await adapter.read_reference_data(
            AccountRef(PlatformCode.GOOGLE, _CUSTOMER_ID), tool="list_meta_pages", arguments={}
        )
    assert keyword_client.calls == []


async def test_read_reference_data_returns_keyword_ideas() -> None:
    keyword_client = _FakeKeywordIdeaClient(
        rows=[{"text": "pienso perro", "avg_monthly_searches": 1000, "competition": "LOW"}]
    )
    adapter = GoogleAdsAdapter(
        _config(), _FakeSearchClient(), FixedClock(_NOW), keyword_idea_client=keyword_client
    )

    rows = await adapter.read_reference_data(
        AccountRef(PlatformCode.GOOGLE, _CUSTOMER_ID),
        tool="get_google_keyword_ideas",
        arguments={"seed_keywords": ["perros"], "geo_target": "2724", "language": "1003"},
    )

    assert rows == keyword_client.rows
    assert keyword_client.calls[0]["customer_id"] == _CUSTOMER_ID
    assert keyword_client.calls[0]["seed_keywords"] == ("perros",)


# ---------------------------------------------------------------------------
# upload_asset (T035 finding 1, threat-model.md #17): AssetService.mutate_assets
# via `GoogleAssetUploadClient`. Mime/size mirror MetaAdsAdapter; idempotency
# is a GAQL lookup on `asset.name` for a standalone upload and the same
# `WriteAuthorizationPipeline.admit_upload` ledger Meta uses for a package
# step (H1, 0.2.23).
# ---------------------------------------------------------------------------

_UPLOAD_BUSINESS_ID = uuid4()
_UPLOAD_CONNECTION_ID = uuid4()
_UPLOAD_ACCOUNT_REF = AccountRef(
    PlatformCode.GOOGLE, _CUSTOMER_ID, _UPLOAD_BUSINESS_ID, _UPLOAD_CONNECTION_ID
)
_UPLOAD_MEDIA = b"tiny-fake-png-bytes"
_UPLOAD_MIME_TYPE = "image/png"
_UPLOAD_WIDTH = 1080
_UPLOAD_HEIGHT = 1080
_UPLOAD_PUBLICATION_ID = "pub-upload-1"


class _FakeAssetUploadClient:
    def __init__(self, resource_name: str = f"customers/{_CUSTOMER_ID}/assets/999") -> None:
        self.calls: list[dict[str, Any]] = []
        self._resource_name = resource_name

    def mutate_image_asset(
        self,
        customer_id: str,
        *,
        name: str,
        data: bytes,
        mime_type: str,
        width: int,
        height: int,
    ) -> str:
        self.calls.append(
            {
                "customer_id": customer_id,
                "name": name,
                "data": data,
                "mime_type": mime_type,
                "width": width,
                "height": height,
            }
        )
        return self._resource_name


def _upload_request(**overrides: object) -> AssetUploadRequest:
    defaults: dict[str, object] = {
        "account_ref": _UPLOAD_ACCOUNT_REF,
        "file_name": "logo.png",
        "mime_type": _UPLOAD_MIME_TYPE,
        "media": _UPLOAD_MEDIA,
        "width": _UPLOAD_WIDTH,
        "height": _UPLOAD_HEIGHT,
    }
    defaults.update(overrides)
    return AssetUploadRequest(**defaults)  # type: ignore[arg-type]


async def test_upload_asset_without_client_fails_closed() -> None:
    adapter = GoogleAdsAdapter(_config(), _FakeSearchClient(), FixedClock(_NOW))

    with pytest.raises(PlatformCapabilityNotImplementedError):
        await adapter.upload_asset(_upload_request())


async def test_upload_asset_rejects_a_disallowed_mime_type_without_calling_the_service() -> None:
    upload_client = _FakeAssetUploadClient()
    adapter = GoogleAdsAdapter(
        _config(), _FakeSearchClient(), FixedClock(_NOW), asset_upload_client=upload_client
    )

    with pytest.raises(GoogleAdsAdapterError, match="tipo de imagen no admitido"):
        await adapter.upload_asset(_upload_request(mime_type="image/gif"))

    assert upload_client.calls == []


async def test_upload_asset_rejects_media_over_the_size_limit_without_calling_the_service() -> (
    None
):
    upload_client = _FakeAssetUploadClient()
    adapter = GoogleAdsAdapter(
        _config(), _FakeSearchClient(), FixedClock(_NOW), asset_upload_client=upload_client
    )
    oversized = b"x" * (MAX_IMAGE_UPLOAD_BYTES + 1)

    with pytest.raises(GoogleAdsAdapterError, match="limite de subida"):
        await adapter.upload_asset(_upload_request(media=oversized))

    assert upload_client.calls == []


async def test_upload_asset_happy_path_calls_asset_service_once() -> None:
    checksum = hashlib.sha256(_UPLOAD_MEDIA).hexdigest()
    upload_client = _FakeAssetUploadClient(resource_name=f"customers/{_CUSTOMER_ID}/assets/555")
    adapter = GoogleAdsAdapter(
        _config(), _FakeSearchClient(), FixedClock(_NOW), asset_upload_client=upload_client
    )

    handle = await adapter.upload_asset(_upload_request())

    assert handle == PlatformAssetHandle(
        platform_asset_id=f"customers/{_CUSTOMER_ID}/assets/555", preview_url=None
    )
    assert upload_client.calls == [
        {
            "customer_id": _CUSTOMER_ID,
            "name": checksum,
            "data": _UPLOAD_MEDIA,
            "mime_type": _UPLOAD_MIME_TYPE,
            "width": _UPLOAD_WIDTH,
            "height": _UPLOAD_HEIGHT,
        }
    ]


async def test_upload_asset_replays_an_existing_resource_found_by_gaql() -> None:
    checksum = hashlib.sha256(_UPLOAD_MEDIA).hexdigest()
    existing_resource = f"customers/{_CUSTOMER_ID}/assets/111"
    lookup_query = (
        f"SELECT asset.resource_name FROM asset WHERE asset.name = '{checksum}'"  # noqa: S608
    )
    search_client = _FakeSearchClient(
        rows_by_query={lookup_query: [{"asset.resource_name": existing_resource}]}
    )
    upload_client = _FakeAssetUploadClient()
    adapter = GoogleAdsAdapter(
        _config(), search_client, FixedClock(_NOW), asset_upload_client=upload_client
    )

    handle = await adapter.upload_asset(_upload_request())

    assert handle == PlatformAssetHandle(platform_asset_id=existing_resource, preview_url=None)
    assert upload_client.calls == []


def _upload_template(media: bytes = _UPLOAD_MEDIA) -> dict[str, object]:
    return {
        "checksum": hashlib.sha256(media).hexdigest(),
        "mime_type": _UPLOAD_MIME_TYPE,
        "width": _UPLOAD_WIDTH,
        "height": _UPLOAD_HEIGHT,
    }


def _upload_step_plan() -> list[dict[str, object]]:
    template_hash = hashlib.sha256(canonical_json_bytes(_upload_template())).hexdigest()
    return [
        {
            "step_index": 0,
            "step_kind": "UPLOAD_CREATIVE",
            "local_ref": "img#abc",
            "parent_local_ref": None,
            "payload_template_hash": template_hash,
            "expected_done_steps": None,
            "depends_on": [],
        }
    ]


def _upload_envelope() -> dict[str, object]:
    return {
        "envelope_version": 2,
        "package_id": "pkg-upload-1",
        "package_hash": "h" * 64,
        "business_id": str(_UPLOAD_BUSINESS_ID),
        "platform": "google",
        "account_ref": str(_UPLOAD_ACCOUNT_REF),
        "publication_id": _UPLOAD_PUBLICATION_ID,
        "approved_by": "owner-1",
        "approved_at": _NOW.isoformat(),
        "approval_expires_at": (_NOW + timedelta(hours=1)).isoformat(),
        "step_plan": _upload_step_plan(),
    }


def _upload_signed_approval(
    signer: ApprovalSigner, envelope: dict[str, object]
) -> dict[str, object]:
    signature = signer.sign(envelope)
    return {
        "envelope": envelope,
        "authorization_id": "human-auth-1",
        "issued_by": envelope["approved_by"],
        "expires_at": envelope["approval_expires_at"],
        "signature": signature.hex(),
    }


def _upload_binding(envelope: dict[str, object]) -> dict[str, object]:
    step = envelope["step_plan"][0]  # type: ignore[index]
    return {
        "package_id": envelope["package_id"],
        "package_hash": envelope["package_hash"],
        "publication_id": envelope["publication_id"],
        "envelope_hash": hashlib.sha256(canonical_json_bytes(envelope)).hexdigest(),
        "step_index": step["step_index"],
        "step_kind": step["step_kind"],
        "local_ref": step["local_ref"],
        "parent_local_ref": step["parent_local_ref"],
        "parent_step_index": None,
        "parent_entity_ref": None,
        "account_ref": envelope["account_ref"],
        "payload_template_hash": step["payload_template_hash"],
        "creative_sources": [],
        "expected_done_steps": step["expected_done_steps"],
    }


async def test_upload_asset_with_package_binding_denies_a_checksum_mismatch_before_the_service(
    tmp_path: Path,
) -> None:
    signer, verifier = _keypair(b"7")
    envelope = _upload_envelope()
    approval = _upload_signed_approval(signer, envelope)
    binding = _upload_binding(envelope)
    pipeline = _pipeline(tmp_path, verifier)
    upload_client = _FakeAssetUploadClient()
    adapter = GoogleAdsAdapter(
        _config(),
        _FakeSearchClient(),
        FixedClock(_NOW),
        write_pipeline=pipeline,
        asset_upload_client=upload_client,
    )

    with pytest.raises(PackageUploadDeniedError) as excinfo:
        await adapter.upload_asset(
            _upload_request(
                media=b"different-bytes-than-what-was-signed",
                package_binding=binding,
                package_approval=approval,
            )
        )

    assert excinfo.value.error_code == "creative_checksum_mismatch"
    assert upload_client.calls == []


async def test_upload_asset_with_package_binding_replays_the_handle_without_a_second_mutate(
    tmp_path: Path,
) -> None:
    signer, verifier = _keypair(b"7")
    envelope = _upload_envelope()
    approval = _upload_signed_approval(signer, envelope)
    binding = _upload_binding(envelope)
    pipeline = _pipeline(tmp_path, verifier)
    upload_client = _FakeAssetUploadClient(resource_name=f"customers/{_CUSTOMER_ID}/assets/777")
    adapter = GoogleAdsAdapter(
        _config(),
        _FakeSearchClient(),
        FixedClock(_NOW),
        write_pipeline=pipeline,
        asset_upload_client=upload_client,
    )
    request = _upload_request(package_binding=binding, package_approval=approval)

    first = await adapter.upload_asset(request)
    second = await adapter.upload_asset(request)

    assert first == second
    assert first.platform_asset_id == f"customers/{_CUSTOMER_ID}/assets/777"
    assert len(upload_client.calls) == 1

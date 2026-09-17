"""The production composition keeps managed OAuth below real signatures/caps/ledger."""

import json
from dataclasses import replace
from datetime import timedelta

import httpx
import pytest

from safent_ads.accounts.application.ports import IdempotencyKey, WriteIntent, WriteOperation
from safent_ads.broker.application.connection_scope import connection_scope
from safent_ads.broker.domain.write_authorization import authorization_signing_payload
from safent_ads.broker.infrastructure.caps_config import parse_caps_config
from safent_ads.broker.platforms.composio_transport import ComposioAdsTransport
from safent_ads.broker.platforms.write_pipeline import WriteAuthorizationPipeline
from safent_ads.composition import broker as composition
from safent_ads.proposals.domain.diff_hash import compute_diff_hash
from safent_ads.shared.clock import FixedClock
from safent_ads.shared.ids import EntityRef, PlatformCode
from tests.unit.broker.platforms.test_composio_transport import NOW, SCOPE, metadata, setup_store
from tests.unit.broker.platforms.test_write_pipeline import _authorization, _signer_and_verifier
from tests.unit.composition.test_broker import _settings
from tests.unit.execution.test_campaign_creation_budget import creation_payload


@pytest.mark.parametrize("platform", [PlatformCode.GOOGLE, PlatformCode.META])
@pytest.mark.parametrize("denial", [None, "signature", "hard_cap", "revoke", "unknown"])
async def test_managed_native_adapter_uses_real_pipeline(  # noqa: PLR0915 - full composition contract
    tmp_path, monkeypatch, platform, denial
):
    signer, _ = _signer_and_verifier()
    encrypted, _, ref, _ = setup_store(tmp_path / "credentials", platform)
    clock = FixedClock(NOW)
    monkeypatch.setattr(composition, "SystemClock", lambda: clock)

    async def allow(host):
        return None

    monkeypatch.setattr(composition, "assert_egress_allowed", allow)
    writes = []
    calls = []
    account = "123" if platform == PlatformCode.GOOGLE else "act_123"

    def handler(request):
        calls.append(request)
        if request.method == "GET":
            assert request.url.path.startswith("/api/v3.1/connected_accounts/")
            return httpx.Response(200, json=metadata(platform))
        payload = json.loads(request.content)
        if payload["endpoint"].endswith("googleAds:searchStream"):
            data = [{"results": [{"customer": {"currencyCode": "EUR"}}]}]
        elif payload["method"] == "GET" and payload["endpoint"].endswith("/act_123"):
            data = {"currency": "EUR"}
        elif payload["method"] == "POST":
            writes.append(payload)
            if denial == "unknown":
                raise httpx.ReadTimeout("redacted unknown write", request=request)
            if platform == PlatformCode.GOOGLE:
                b, c = payload["body"]["mutateOperations"]
                budget = b["campaignBudgetOperation"]["create"]
                campaign = c["campaignOperation"]["create"]
                assert budget["amountMicros"] == "20000000"
                assert campaign["status"] == "PAUSED"
                data = {
                    "mutateOperationResponses": [
                        {
                            "campaignBudgetResult": {
                                "resourceName": "customers/123/campaignBudgets/111",
                                "campaignBudget": budget,
                            }
                        },
                        {
                            "campaignResult": {
                                "resourceName": "customers/123/campaigns/456",
                                "campaign": {
                                    **campaign,
                                    "campaignBudget": "customers/123/campaignBudgets/111",
                                },
                            }
                        },
                    ]
                }
            else:
                assert payload["body"]["daily_budget"] == 2000
                assert payload["body"]["status"] == "PAUSED"
                data = {"id": "456"}
        else:
            data = {**writes[0]["body"], "account_id": "123"}
        return httpx.Response(200, json={"status": 200, "data": data})

    monkeypatch.setattr(
        composition,
        "ComposioAdsTransport",
        lambda **kwargs: ComposioAdsTransport(
            **kwargs, http_transport=httpx.MockTransport(handler)
        ),
    )
    settings = _settings(
        credential_store_dir=tmp_path / "credentials",
        approval_public_key=signer.public_key_b64(),
        composio_api_key="private-test-key",
        composio_googleads_auth_config_id="ac_bound",
        composio_metaads_auth_config_id="ac_bound",
    )
    caps = parse_caps_config(f"""defaults:
  max_step_pct: 100
  max_changes_per_day: 10
  autonomy_enabled: false
accounts:
  '{account}':
    daily_cap_minor: {1000 if denial == "hard_cap" else 100000}
    monthly_cap_minor: 900000
    floor_minor: 100
    ceiling_minor: 50000
""")
    registry = await composition._build_registry(settings, caps, encrypted)
    runtime = composition._build_runtime(settings, registry, encrypted)
    assert runtime.app_credentials.status(platform).configured
    adapter = registry.get(platform)
    assert isinstance(adapter._write_pipeline, WriteAuthorizationPipeline)
    assert (
        adapter._write_pipeline
        is registry.get(
            PlatformCode.META if platform == PlatformCode.GOOGLE else PlatformCode.GOOGLE
        )._write_pipeline
    )
    entity = EntityRef.parse(
        f"{platform.value}:account:{SCOPE.business_id}:{SCOPE.connection_id}:{account}"
    )
    payload = creation_payload(platform.value)
    diff = compute_diff_hash(entity, "new_campaign:explicit", None, payload)
    intent = WriteIntent(
        entity,
        WriteOperation.CREATE_CAMPAIGN,
        "new_campaign:explicit",
        None,
        payload,
        diff,
        "",
        str(SCOPE.business_id),
    )
    unsigned = replace(_authorization(signer, diff_hash=diff), expires_at=NOW + timedelta(hours=1))
    auth = replace(unsigned, signature=signer.sign(authorization_signing_payload(unsigned)).hex())
    if denial == "signature":
        auth = replace(auth, signature="00" * 64)
    if denial == "revoke":
        encrypted.revoke_credential(ref, at=NOW)
    with connection_scope(SCOPE):
        result = await adapter.execute_write(intent, auth, IdempotencyKey("managed-create"))
        if denial is None or denial == "unknown":
            replay = await adapter.execute_write(intent, auth, IdempotencyKey("managed-create"))
            assert replay == result
    if denial in {"signature", "hard_cap", "revoke"}:
        assert result.outcome in {"DENIED", "BLOCKED_HARD_CAP"}
        assert len(calls) == 0
        assert not writes
    else:
        assert result.outcome == ("UNKNOWN" if denial == "unknown" else "SUCCEEDED")
        assert len(writes) == 1
    adapter._write_pipeline._ledger.close()

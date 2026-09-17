"""Creation receives native account IDs, while signature/EE claims stay numeric."""

from dataclasses import replace

import pytest

from safent_ads.accounts.application.ports import IdempotencyKey, WriteOperation
from safent_ads.broker.infrastructure.caps_config import parse_caps_config
from safent_ads.broker.platforms.campaign_creation import create_paused_campaign
from safent_ads.shared.ids import EntityLevel, EntityRef, PlatformCode
from tests.unit.broker.ledger_scope_fakes import fake_scope
from tests.unit.broker.platforms.test_write_pipeline import _CAPS_YAML, _NOW
from tests.unit.broker.test_managed_fresh_admission import resign, setup
from tests.unit.execution.test_campaign_creation_budget import creation_payload


@pytest.mark.parametrize("platform", [PlatformCode.GOOGLE, PlatformCode.META])
@pytest.mark.parametrize("uncertain", [False, True])
async def test_native_creation_and_replay_keep_exact_numeric_signed_context(
    tmp_path, platform, uncertain
):
    pipeline, intent, auth, ledger, authority, _ = setup(tmp_path)
    bound = replace(
        intent.managed_binding, account=replace(intent.managed_binding.account, platform=platform)
    )
    provider = bound.provider_account
    account = provider.external_account_id
    pipeline._caps = parse_caps_config(_CAPS_YAML.replace('"1234567890"', f'"{account}"'))
    authority.admit_binding.return_value = bound
    ref = EntityRef(
        platform, EntityLevel.ACCOUNT, account, provider.business_id, provider.connection_id
    )
    intent, auth = resign(
        replace(
            intent,
            entity_ref=ref,
            managed_binding=bound,
            operation=WriteOperation.CREATE_CAMPAIGN,
            parametro="new_campaign:fixture",
            valor_actual=None,
            valor_propuesto=creation_payload(platform.value),
            expected_state_hash="",
        ),
        auth,
    )
    signed_hash, signature = intent.diff_hash, auth.signature
    claims = bound.as_claims()
    calls = []

    def native(account_id, plan):
        calls.append((account_id, plan))
        assert account_id == account
        if uncertain:
            raise TimeoutError("synthetic unknown")
        return {
            "campaign_resource": f"{account}/456",
            "status": "PAUSED",
            "daily_budget_minor": 2000,
        }

    kwargs = dict(
        pipeline=pipeline,
        intent=intent,
        authorization=auth,
        idempotency_key=IdempotencyKey("managed-create"),
        now=_NOW,
        currency=lambda _: "EUR",
        create=native,
        consume_rate=lambda _operations: True,
    )
    result = await create_paused_campaign(**kwargs)
    assert result.outcome == ("UNKNOWN" if uncertain else "SUCCEEDED"), result.error_code
    assert await create_paused_campaign(**kwargs) == result
    assert len(calls) == 1
    assert claims["external_account_id"] == "1234567890"
    assert intent.diff_hash == signed_hash and auth.signature == signature
    assert bound.as_claims() == claims
    assert ledger.pending_totals(fake_scope(intent, account))[1] == (2000 if uncertain else 0)
    assert pipeline.read_receipt("managed-create", intent, auth) == result

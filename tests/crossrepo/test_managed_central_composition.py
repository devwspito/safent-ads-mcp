"""Real central REST -> EE cookie/MFA -> central approval -> PG queue -> Unix broker.

Only provider SDK and HTTP transport destination are test doubles. No managed
Runtime policy is enabled by this test; that remains a separate activation gate.
"""

import base64
import os
from datetime import UTC, datetime, timedelta
from uuid import UUID, uuid4

import httpx
import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import async_sessionmaker

pytest.importorskip("safent_control", reason="Explicit Enterprise snapshot required")

from safent_control.api import ads_human_approval as human_api
from safent_control.api import deps
from safent_control.application.auth_service import SESSION_COOKIE
from safent_control.domain import totp
from safent_control.domain.ads_grants import AccountIdentity
from safent_control.domain.entities import ConsoleSession
from safent_control.infrastructure.ads_approval_delivery import AdsApprovalDelivery
from safent_control.infrastructure.config import get_settings
from tests.contracts.sql_fixtures import campaign_ref, seed_entity
from tests.crossrepo.test_managed_broker_admission import enterprise as enterprise  # noqa: PLC0414
from tests.integration.composition.test_write_path_end_to_end import _caps_yaml
from tests.integration.execution.test_physical_controls_sql import clone_connection
from tests.unit.broker.platforms.test_google_ads_adapter import (
    _campaign_client,
    _campaign_state_hash,
    _config,
)
from tests.unit.composition.factories import build_api_settings
from tests.unit.composition.test_broker import _settings

from safent_ads.accounts.domain.refs import AccountRef, CredentialRefId
from safent_ads.broker.application.ports import CredentialRecord
from safent_ads.broker.infrastructure.adapter_registry import PlatformAdapterRegistry
from safent_ads.broker.infrastructure.caps_config import parse_caps_config
from safent_ads.broker.infrastructure.connected_credential_store import ConnectedCredentialStore
from safent_ads.broker.infrastructure.credential_store import EncryptedCredentialStore
from safent_ads.broker.platforms.google_ads_adapter import GoogleAdsAdapter
from safent_ads.broker.presentation.socket_server import serve
from safent_ads.composition import broker as broker_module
from safent_ads.composition import container as container_module
from safent_ads.composition.app import create_app
from safent_ads.composition.broker import _build_runtime, _build_write_pipeline
from safent_ads.execution.domain.execution_attempt import ExecutionStatus
from safent_ads.iam.infrastructure import enterprise_ads_authority as authority_module
from safent_ads.iam.infrastructure.enterprise_human_approval import EnterpriseHumanApproval
from safent_ads.proposals.domain.identifiers import ProposalId
from safent_ads.shared.clock import FixedClock
from safent_ads.shared.crypto.ed25519 import ApprovalSigner
from safent_ads.shared.ids import PlatformCode
from safent_ads.shared.managed_ads import ManagedAdsBinding

pytestmark = pytest.mark.integration


@pytest.mark.parametrize("revoke_before_effect", [False, True])
@pytest.mark.parametrize("through_runtime", [False, True])
async def test_composed_managed_human_execution(  # noqa: PLR0915, PLR0917 - full cross-process sequence
    db_session,
    database_url,
    enterprise,
    tmp_path,
    monkeypatch,
    revoke_before_effect,
    through_runtime,
):
    if through_runtime:
        pytest.importorskip("hermes", reason="Explicit Runtime snapshot required")
    service, actor, initial, enterprise_app, secret = enterprise
    now = datetime.now(UTC)
    clock = FixedClock(now)
    legacy = campaign_ref("customers/1234567890/campaigns/111", "google")
    business = await seed_entity(db_session, legacy)
    await db_session.execute(
        text("UPDATE platform_accounts SET external_account_id='1234567890' WHERE business_id=:b"),
        {"b": business},
    )
    entity, ref = await clone_connection(db_session, legacy)
    account = AccountRef.parse(ref)
    await db_session.execute(
        text("UPDATE ad_entities SET platform_state_hash=:hash WHERE entity_ref=:ref"),
        {"hash": _campaign_state_hash(), "ref": str(entity)},
    )
    await db_session.execute(
        text("""INSERT INTO guardrails
        (scope,business_id,currency,daily_cap_minor,monthly_cap_minor,budget_floor_minor,
         budget_ceiling_minor,max_step_pct,max_changes_per_entity_per_day)
        VALUES('business',:business,'EUR',1000000,30000000,0,1000000,100,10)"""),
        {"business": business},
    )
    await db_session.commit()
    identity = AccountIdentity(
        business_id=str(business),
        platform="google",
        connection_id=str(account.connection_id),
        external_account_id="1234567890",
    )
    service.sync_resource(str(initial.org_id), identity, revision=1, active=True)
    grant = service.create(
        str(initial.org_id),
        actor.user_id,
        operation_id=str(uuid4()),
        user_id=str(initial.user_id),
        employee_id=str(initial.employee_id),
        instance_id=str(initial.instance_id),
        account=identity,
        expected_resource_revision=1,
    )
    binding = ManagedAdsBinding.from_claims(service.repo.get_ads_grant(grant["grant_id"]).claims())
    token = service.issue(
        str(binding.grant_id), service.repo.get_instance(str(binding.instance_id))
    )["grant_token"]
    seed = base64.b64encode(b"m" * 32).decode()
    socket = tmp_path / "managed.sock"
    settings = build_api_settings(
        database_url=database_url,
        broker_socket_path=socket,
        approval_signing_key=seed,
        managed_central=True,
        enterprise_origin="https://enterprise.invalid",
        enterprise_service_secret=secret,
        enterprise_org_ids=frozenset({binding.org_id}),
    )
    monkeypatch.setattr(
        container_module,
        "EnterpriseHumanApproval",
        lambda trust: EnterpriseHumanApproval(trust, transport=httpx.ASGITransport(enterprise_app)),
    )
    central = create_app(settings)
    container = central.state.container
    container.clock = clock
    container.session_factory = async_sessionmaker(
        bind=db_session.bind, expire_on_commit=False, join_transaction_mode="create_savepoint"
    )
    original_delivery = AdsApprovalDelivery
    monkeypatch.setattr(
        human_api,
        "AdsApprovalDelivery",
        lambda origin: original_delivery(origin, transport=httpx.ASGITransport(central)),
    )
    monkeypatch.setenv("SAFENT_ADS_CENTRAL_ORIGIN", "https://central.invalid")
    get_settings.cache_clear()

    key = base64.b64encode(b"0" * 32).decode()
    credentials = EncryptedCredentialStore(tmp_path / "credentials", key)
    credential = CredentialRefId(account.connection_id)
    credentials.save_credential(
        credential,
        CredentialRecord(
            PlatformCode.GOOGLE,
            "synthetic-refresh",
            "refresh_token",
            (),
            now,
            None,
            business_id=str(business),
            connection_id=str(account.connection_id),
        ),
    )
    credentials.bind_account_credential(
        PlatformCode.GOOGLE,
        "1234567890",
        credential,
        business_id=str(business),
        connection_id=str(account.connection_id),
    )
    connected = ConnectedCredentialStore(credentials, clock)
    signer = ApprovalSigner.from_seed_b64(seed)
    broker_settings = _settings(
        broker_socket_path=socket,
        credential_master_key=key,
        credential_store_dir=tmp_path / "credentials",
        approval_public_key=signer.public_key_b64(),
        managed_central=True,
        enterprise_origin="https://enterprise.invalid",
        enterprise_service_secret=secret,
        enterprise_org_ids=frozenset({binding.org_id}),
    )
    original_authority = authority_module.EnterpriseAdsAuthority
    monkeypatch.setattr(
        broker_module,
        "EnterpriseAdsAuthority",
        lambda trust: original_authority(trust, transport=httpx.ASGITransport(enterprise_app)),
    )
    pipeline = _build_write_pipeline(
        broker_settings, parse_caps_config(_caps_yaml("1234567890")), connected
    )
    assert pipeline is not None
    pipeline._clock = clock
    sdk = _campaign_client()
    adapter = GoogleAdsAdapter(_config(), sdk, clock, write_pipeline=pipeline)
    runtime = _build_runtime(
        broker_settings, PlatformAdapterRegistry({PlatformCode.GOOGLE: adapter}), credentials
    )
    server = await serve(socket, runtime, frozenset({os.getuid()}))
    try:
        caller = httpx.AsyncClient(
            transport=httpx.ASGITransport(central),
            base_url="https://central.invalid",
            headers={"Authorization": "Bearer " + token},
        )
        if through_runtime:
            from tests.crossrepo.runtime_ads_driver import runtime_driver  # noqa: PLC0415

            await caller.aclose()
            caller = runtime_driver(service, binding, enterprise_app, central, tmp_path)
        async with caller as client:
            inventory = await client.post("/api/v1/managed/tools/list_platform_accounts", json={})
            assert inventory.status_code == 200, inventory.text
            assert len(inventory.json()["result"]["accounts"]) == 1
            proposed = await client.post(
                "/api/v1/managed/tools/propose_pause",
                json={
                    "business_id": str(business),
                    "entity_ref": str(entity),
                    "cause": {"text": "Revisión humana de pausa"},
                },
            )
            assert proposed.status_code == 200, proposed.text
            proposal_id = proposed.json()["result"]["proposal_id"]
            review = await client.post(
                "/api/v1/managed/tools/get_approval_review", json={"proposal_id": proposal_id}
            )
            assert review.status_code == 200, review.text
            intent = review.json()["result"]["intent_id"]
            assert "grant_token" not in review.text and "assertion" not in review.text
        session = ConsoleSession(
            "synthetic-human-" + str(uuid4()),
            str(binding.user_id),
            str(binding.org_id),
            now + timedelta(hours=1),
        )
        service.repo.save_session(session)
        mfa = deps.get_mfa_service()
        enrolled = mfa.begin_enrollment(user_id=session.user_id, account_name="fixture")
        code = totp._hotp(secret=enrolled["secret"], counter=int(now.timestamp()) // 30)
        assert mfa.confirm_enrollment(user_id=session.user_id, code=code)
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(enterprise_app),
            base_url="https://enterprise.invalid",
            cookies={SESSION_COOKIE: session.session_id},
        ) as browser:
            review = await browser.get(f"/api/ads/human-approval/intents/{intent}")
            assert review.status_code == 200
            confirmed = await browser.post(
                f"/api/ads/human-approval/intents/{intent}/confirm",
                headers={
                    "Origin": "https://enterprise.invalid",
                    "X-Safent-Org": str(binding.org_id),
                },
                json={"diff_hash": review.json()["diff_hash"], "totp": code},
            )
            assert confirmed.status_code == 200, confirmed.text
            assert set(confirmed.json()) == {"intent_id", "receipt"}
            receipt = confirmed.json()["receipt"]
            replay = await browser.post(
                f"/api/ads/human-approval/intents/{intent}/confirm",
                headers={
                    "Origin": "https://enterprise.invalid",
                    "X-Safent-Org": str(binding.org_id),
                },
                json={"diff_hash": review.json()["diff_hash"], "totp": code},
            )
            assert replay.status_code in {
                401,
                409,
            }  # TOTP replay or consumed intent; neither admits.
        clock.advance_to(
            datetime.fromisoformat(receipt["execution_scheduled_at"]) + timedelta(seconds=1)
        )
        if revoke_before_effect:
            service.revoke(
                str(binding.org_id),
                actor.user_id,
                str(binding.grant_id),
                operation_id=str(uuid4()),
                expected_revision=binding.revision,
            )
            if through_runtime:
                from hermes.runtime.managed_ads_policy import ManagedAdsUnavailable  # noqa: PLC0415

                with pytest.raises(ManagedAdsUnavailable):
                    await caller.post(
                        "/api/v1/managed/tools/get_proposal", json={"proposal_id": proposal_id}
                    )
        async with container.session_factory() as session:
            cases = container.build_execution_use_cases(session)
            status = await cases.chokepoint.run_once(proposal_id=ProposalId(UUID(proposal_id)))
            await session.commit()
            diagnostic = (
                (
                    await session.execute(
                        text("SELECT outcome,error_code FROM executions WHERE id=:id"),
                        {"id": UUID(receipt["execution_id"])},
                    )
                )
                .mappings()
                .one()
            )
            assert (
                status is ExecutionStatus.EXECUTED
                if not revoke_before_effect
                else status is not ExecutionStatus.EXECUTED
            ), dict(diagnostic)
        assert len(sdk.status_mutations) == (0 if revoke_before_effect else 1)
    finally:
        server.close()
        await server.wait_closed()
        await pipeline._managed_authority.aclose()
        pipeline._ledger.close()
        await container.aclose()

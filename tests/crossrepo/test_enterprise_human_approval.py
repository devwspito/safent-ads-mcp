"""Real EE cookie+MFA+signature/nonce and Ads PG proposal/approval/queue.

Explicit PYTHONPATH also contains an Enterprise source snapshot. Only Ads SDK
and policy telemetry are test doubles; no production managed composition.
"""

import base64
from dataclasses import asdict, replace
from datetime import UTC, datetime, timedelta
from uuid import uuid4

import httpx
import pytest
from fastapi.encoders import jsonable_encoder
from sqlalchemy import text

pytest.importorskip("safent_control", reason="Explicit Enterprise snapshot required")

from safent_control.api import deps
from safent_control.application.auth_service import SESSION_COOKIE
from safent_control.domain import totp
from safent_control.domain.ads_grants import AccountIdentity, AdsGrantError
from safent_control.domain.entities import ConsoleSession
from safent_control.infrastructure.ads_approval_delivery import AdsApprovalDelivery
from safent_control.infrastructure.config import get_settings
from tests.contracts.sql_fixtures import campaign_ref, seed_entity
from tests.crossrepo.test_managed_broker_admission import enterprise as enterprise  # noqa: PLC0414
from tests.integration.execution.test_physical_controls_sql import clone_connection
from tests.integration.proposals.test_physical_equivalence import command, use_case
from tests.unit.broker.platforms.test_google_ads_adapter import (
    _campaign_client,
    _campaign_state_hash,
    _config,
)
from tests.unit.broker.test_managed_fresh_admission import setup
from tests.unit.execution.conftest import guardrail_set

from safent_ads.accounts.domain.refs import AccountRef, CredentialRefId, IdempotencyKey
from safent_ads.broker.application.connection_scope import ConnectionScope, connection_scope
from safent_ads.broker.application.ports import CredentialRecord
from safent_ads.broker.infrastructure.connected_credential_store import ConnectedCredentialStore
from safent_ads.broker.infrastructure.credential_store import EncryptedCredentialStore
from safent_ads.broker.platforms.google_ads_adapter import GoogleAdsAdapter
from safent_ads.composition.signing import build_approval_key_pair
from safent_ads.execution.application.ports import WriteCommand
from safent_ads.execution.domain.guardrails import GuardrailEvaluator
from safent_ads.execution.infrastructure.broker_platform import _intent, _signed
from safent_ads.execution.infrastructure.sql_execution_queue import SqlExecutionQueue
from safent_ads.execution.testing.fakes import (
    FakeBrakeStatePort,
    FakeGuardrailSetRepository,
    FakeSpendLedger,
)
from safent_ads.iam.infrastructure.enterprise_ads_authority import EnterpriseAdsTrust
from safent_ads.iam.infrastructure.enterprise_human_approval import EnterpriseHumanApproval
from safent_ads.proposals.application.prepare_managed_approval import PrepareManagedApproval
from safent_ads.proposals.application.submit_approval import (
    ProposalApprovalDeniedError,
    SubmitApproval,
    SubmitApprovalCommand,
)
from safent_ads.proposals.domain.authorization import AuthorizationChannel
from safent_ads.proposals.domain.classification import ProposalKind
from safent_ads.proposals.domain.proposal import ProposedDiff
from safent_ads.proposals.infrastructure.sql_authorization_repository import (
    SqlAuthorizationRepository,
)
from safent_ads.proposals.infrastructure.sql_proposal_repository import SqlProposalRepository
from safent_ads.shared.clock import FixedClock
from safent_ads.shared.ids import BusinessId, PlatformCode
from safent_ads.shared.managed_ads import ManagedAdsBinding

pytestmark = pytest.mark.integration


@pytest.mark.parametrize("logout", [False, True])
async def test_real_human_to_signed_queue_and_broker_receipt(  # noqa: PLR0915 - cross-repo sequence
    db_session, enterprise, tmp_path, logout, monkeypatch
):
    service, actor, original_binding, app, secret = enterprise
    now = datetime.now(UTC)
    clock = FixedClock(now)
    original = campaign_ref("customers/1234567890/campaigns/7", "google")
    business = BusinessId(await seed_entity(db_session, original))
    await db_session.execute(
        text("UPDATE platform_accounts SET external_account_id='1234567890' WHERE business_id=:b"),
        {"b": business.value},
    )
    entity, account_ref = await clone_connection(db_session, original)
    account = AccountRef.parse(account_ref)
    identity = AccountIdentity(
        business_id=str(account.business_id),
        connection_id=str(account.connection_id),
        platform="google",
        external_account_id=account.external_account_id,
    )
    service.sync_resource(str(original_binding.org_id), identity, revision=1, active=True)
    grant = service.create(
        str(original_binding.org_id),
        actor.user_id,
        operation_id=str(uuid4()),
        user_id=str(original_binding.user_id),
        employee_id=str(original_binding.employee_id),
        instance_id=str(original_binding.instance_id),
        account=identity,
        expected_resource_revision=1,
    )
    server_binding = ManagedAdsBinding.from_claims(
        service.repo.get_ads_grant(grant["grant_id"]).claims()
    )
    token = service.issue(
        str(server_binding.grant_id), service.repo.get_instance(str(server_binding.instance_id))
    )
    authority = EnterpriseHumanApproval(
        EnterpriseAdsTrust(
            "https://enterprise.invalid", secret, frozenset({server_binding.org_id})
        ),
        transport=httpx.ASGITransport(app),
    )
    try:
        admission = await authority.introspect(
            token["grant_token"],
            expected_instance_id=server_binding.instance_id,
            operation="approve",
            account=account,
        )
        proposer = use_case(db_session)
        proposer._clock = clock
        cmd = replace(
            command(business, entity),
            kind=ProposalKind.PAUSE,
            diff=ProposedDiff.build(
                entity, "status", "ENABLED", "PAUSED", managed_binding=admission.binding
            ),
            expected_state_hash=_campaign_state_hash(),
        )
        proposed = await proposer.execute(cmd)
        proposals = SqlProposalRepository(db_session)
        preparation = await PrepareManagedApproval(proposals, authority, clock).execute(
            proposed.proposal_id, admission
        )
        session = ConsoleSession(
            "synthetic-human-session-" + str(uuid4()),
            str(server_binding.user_id),
            str(server_binding.org_id),
            now + timedelta(hours=1),
        )
        service.repo.save_session(session)
        mfa = deps.get_mfa_service()
        enrolled = mfa.begin_enrollment(user_id=session.user_id, account_name="human")
        counter = int(now.timestamp()) // 30
        code = totp._hotp(secret=enrolled["secret"], counter=counter)
        assert mfa.confirm_enrollment(user_id=session.user_id, code=code)
        approvals, queue = (
            SqlAuthorizationRepository(db_session),
            SqlExecutionQueue(db_session, clock),
        )
        submit = SubmitApproval(
            proposals=proposals,
            authorizations=approvals,
            execution_queue=queue,
            brakes=FakeBrakeStatePort(),
            guardrail_evaluator=GuardrailEvaluator(),
            guardrail_sets=FakeGuardrailSetRepository({str(entity): guardrail_set()}),
            spend_ledger=FakeSpendLedger(),
            signer=build_approval_key_pair(base64.b64encode(b"m" * 32).decode()).signer,
            clock=clock,
            human_authority=authority,
        )
        delivery_result = {}

        async def deliver(_self, assertion, *, proposal_id):
            if logout:
                service.repo.delete_session(session.session_id)
                service.repo.save_session(session)
            command = SubmitApprovalCommand(
                proposed.proposal_id,
                proposed.diff_hash,
                "not-enterprise-user",
                AuthorizationChannel.PANEL,
                human_assertion=assertion,
            )
            delivery_result["command"] = command
            try:
                outcome = await submit.execute(command)
            except ProposalApprovalDeniedError:
                raise AdsGrantError("ads_human_delivery_uncertain", 503) from None
            delivery_result["result"] = outcome
            return {
                "status": "scheduled",
                "proposal_id": proposal_id,
                **jsonable_encoder(asdict(outcome)),
            }

        monkeypatch.setattr(AdsApprovalDelivery, "submit", deliver)
        monkeypatch.setenv("SAFENT_ADS_CENTRAL_ORIGIN", "https://central.invalid")
        get_settings.cache_clear()
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app),
            base_url="https://enterprise.invalid",
            cookies={SESSION_COOKIE: session.session_id},
        ) as browser:
            preview = await browser.get(f"/api/ads/human-approval/intents/{preparation.intent_id}")
            assert preview.status_code == 200, preview.text
            assert preview.json()["diff_hash"] == proposed.diff_hash
            assert preview.json()["diff"]["managed_binding"] == server_binding.as_claims()
            confirmed = await browser.post(
                f"/api/ads/human-approval/intents/{preparation.intent_id}/confirm",
                headers={
                    "Origin": "https://enterprise.invalid",
                    "X-Safent-Org": session.active_org_id,
                },
                json={"diff_hash": proposed.diff_hash, "totp": code},
            )
            assert confirmed.status_code == (503 if logout else 200), confirmed.text
            assert "assertion" not in confirmed.json()
        if logout:
            assert await approvals.get_active_for_proposal(proposed.proposal_id) is None
            assert await queue.get_for_proposal(proposed.proposal_id) is None
            return
        result = delivery_result["result"]
        stored = await proposals.get(proposed.proposal_id)
        auth = await approvals.get_active_for_proposal(proposed.proposal_id)
        assert auth.issued_by == session.user_id and auth.managed_binding == server_binding
        attempt = await queue.get_for_proposal(proposed.proposal_id)
        assert str(attempt.execution_id) == result.execution_id
        assert stored.diff.diff_hash == proposed.diff_hash
        with pytest.raises(ProposalApprovalDeniedError):
            await submit.execute(delivery_result["command"])
        # Advance beyond the normal human undo window; do not renew the EE proof.
        clock.advance_to(result.execution_scheduled_at + timedelta(seconds=1))
        pipeline, _, _, ledger, _, _ = setup(tmp_path)
        pipeline._clock = clock
        pipeline._managed_authority = authority
        credentials = EncryptedCredentialStore(
            tmp_path / "credentials", base64.b64encode(b"0" * 32).decode()
        )
        credential = CredentialRefId(account.connection_id)
        credentials.save_credential(
            credential,
            CredentialRecord(
                PlatformCode.GOOGLE,
                "synthetic-unused-refresh",
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
            account.external_account_id,
            credential,
            business_id=str(business),
            connection_id=str(account.connection_id),
        )
        connected = ConnectedCredentialStore(credentials, clock)
        pipeline._scope_resolver = connected.resolve_write_scope
        sdk = _campaign_client()
        adapter = GoogleAdsAdapter(_config(), sdk, clock, write_pipeline=pipeline)
        intent = _intent(
            WriteCommand(entity, "status", "ENABLED", "PAUSED", server_binding), stored, auth
        )
        try:
            with connection_scope(ConnectionScope(business.value, account.connection_id)):
                outcome = await adapter.execute_write(
                    intent, _signed(auth), IdempotencyKey(attempt.idempotency_key)
                )
                assert outcome.outcome == "SUCCEEDED", outcome.error_code
                receipt = pipeline.read_receipt(attempt.idempotency_key, intent, _signed(auth))
                assert receipt.outcome == "SUCCEEDED"
            assert len(sdk.status_mutations) == 1
        finally:
            ledger.close()
    finally:
        await authority.aclose()

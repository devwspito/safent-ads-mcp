"""Explicit crossrepo run: PYTHONPATH=src:<Enterprise>/src.

Real EE registry/router + Ads HTTP admission + encrypted credential resolver +
SQLite reservation + Google adapter. Only provider SDK and human signing are
synthetic. This does NOT implement or enable an Enterprise human login flow.
"""

import base64
from dataclasses import replace
from uuid import uuid4

import httpx
import pytest

pytest.importorskip("safent_control", reason="Explicit Enterprise source snapshot required")

from fastapi import FastAPI
from safent_control.api import ads, deps
from safent_control.application.ads_grants import AdsGrants
from safent_control.application.provisioning import ProvisioningService
from safent_control.domain.ads_grants import AccountIdentity, AdsGrantError
from safent_control.domain.entities import (
    Instance,
    InstanceState,
    LicenseState,
    Membership,
    MembershipRole,
    User,
)
from safent_control.infrastructure.config import get_settings
from safent_control.infrastructure.keystore import TenantKeystore
from safent_control.infrastructure.repository import ControlPlaneRepository, hash_secret
from tests.unit.broker.platforms.test_google_ads_adapter import (
    _campaign_client,
    _campaign_state_hash,
    _config,
)
from tests.unit.broker.platforms.test_write_pipeline import _NOW
from tests.unit.broker.test_managed_fresh_admission import setup

from safent_ads.accounts.domain.refs import CredentialRefId, IdempotencyKey
from safent_ads.broker.application.connection_scope import ConnectionScope, connection_scope
from safent_ads.broker.application.ports import CredentialRecord
from safent_ads.broker.domain.write_authorization import (
    authorization_signing_payload,
    recompute_diff_hash,
)
from safent_ads.broker.infrastructure.connected_credential_store import ConnectedCredentialStore
from safent_ads.broker.infrastructure.credential_store import EncryptedCredentialStore
from safent_ads.broker.platforms.google_ads_adapter import GoogleAdsAdapter
from safent_ads.iam.infrastructure.enterprise_ads_authority import (
    EnterpriseAdsAuthority,
    EnterpriseAdsTrust,
)
from safent_ads.shared.crypto.ed25519 import ApprovalSigner
from safent_ads.shared.ids import EntityLevel, EntityRef, PlatformCode
from safent_ads.shared.managed_ads import ManagedAdsBinding


@pytest.fixture
def enterprise(tmp_path, monkeypatch):
    repo = ControlPlaneRepository(db_path=tmp_path / "ee.sqlite")
    keys = TenantKeystore(db_path=tmp_path / "keys.sqlite", data_dir=tmp_path)
    provision = ProvisioningService(repo=repo, keystore=keys)
    org = provision.create_org(name="Crossrepo fixture", seat_limit=2)
    employee = provision.create_employee(
        org_id=org.org_id, name="Human", email="fixture@example.test"
    )
    template = provision.create_agent_template(employee_id=employee.employee_id, name="Fixture")
    instance = Instance(
        str(uuid4()),
        org.org_id,
        template.agent_template_id,
        "synthetic-device",
        state=InstanceState.ACTIVE,
    )
    repo.save_instance(instance, instance_secret_hash=hash_secret("synthetic-pairing-unused"))
    license = provision.create_license(
        org_id=org.org_id, plan="fixture", max_agents=1, expires_at=""
    )
    repo.save_license(
        replace(license, state=LicenseState.ASSIGNED, assigned_instance_id=instance.instance_id)
    )
    actor, user = (
        User(str(uuid4()), f"{uuid4()}@example.test", name) for name in ("Admin", "Human")
    )
    for person, role in ((actor, MembershipRole.ADMIN), (user, MembershipRole.VIEWER)):
        repo.save_user(person)
        repo.save_membership(Membership(str(uuid4()), person.user_id, org.org_id, role))
    service = AdsGrants(repo, keys)
    account = AccountIdentity(
        business_id=str(uuid4()),
        platform="google",
        connection_id=str(uuid4()),
        external_account_id="1234567890",
    )
    service.sync_resource(org.org_id, account, revision=1, active=True)
    grant = service.create(
        org.org_id,
        actor.user_id,
        operation_id=str(uuid4()),
        user_id=user.user_id,
        employee_id=employee.employee_id,
        instance_id=instance.instance_id,
        account=account,
        expected_resource_revision=1,
    )
    binding = ManagedAdsBinding.from_claims(repo.get_ads_grant(grant["grant_id"]).claims())
    secret = "0123456789abcdef" * 4  # Synthetic test-only central identity. gitleaks:allow
    monkeypatch.setenv("SAFENT_ADS_SERVICE_SECRET", secret)
    monkeypatch.setenv("SAFENT_ADS_SERVICE_ORGS", org.org_id)
    monkeypatch.setenv("PUBLIC_BASE_URL", "https://enterprise.invalid")
    monkeypatch.setattr(deps, "get_repo", lambda: repo)
    monkeypatch.setattr(deps, "get_keystore", lambda: keys)
    get_settings.cache_clear()
    app = FastAPI()
    app.include_router(ads.router)
    app.add_exception_handler(AdsGrantError, ads.ads_error_handler)
    yield service, actor, binding, app, secret
    get_settings.cache_clear()


@pytest.mark.parametrize("terminal", ["SUCCEEDED", "FAILED"])
async def test_real_ee_revoke_blocks_next_effect_and_preserves_original_receipt(
    tmp_path, enterprise, terminal
):
    service, actor, binding, app, secret = enterprise
    pipeline, intent, auth, ledger, _, clock = setup(tmp_path)
    authority = EnterpriseAdsAuthority(
        EnterpriseAdsTrust("https://enterprise.invalid", secret, frozenset({binding.org_id})),
        transport=httpx.ASGITransport(app),
    )
    credentials = EncryptedCredentialStore(
        tmp_path / "credentials", base64.b64encode(b"0" * 32).decode()
    )
    ref = CredentialRefId(binding.account.connection_id)
    credentials.save_credential(
        ref,
        CredentialRecord(
            PlatformCode.GOOGLE,
            "synthetic-unused-refresh",
            "refresh_token",
            (),
            _NOW,
            None,
            business_id=str(binding.account.business_id),
            connection_id=str(binding.account.connection_id),
        ),
    )
    credentials.bind_account_credential(
        PlatformCode.GOOGLE,
        binding.account.external_account_id,
        ref,
        business_id=str(binding.account.business_id),
        connection_id=str(binding.account.connection_id),
    )
    connected = ConnectedCredentialStore(credentials, clock)
    pipeline._managed_authority = (
        authority  # Harness-only injection; production composition is closed.
    )
    pipeline._scope_resolver = connected.resolve_write_scope
    entity = EntityRef(
        PlatformCode.GOOGLE,
        EntityLevel.CAMPAIGN,
        "customers/1234567890/campaigns/7",
        binding.account.business_id,
        binding.account.connection_id,
    )
    intent = replace(
        intent,
        entity_ref=entity,
        managed_binding=binding,
        business_id=str(binding.account.business_id),
        valor_actual="ENABLED",
        expected_state_hash=_campaign_state_hash(),
    )
    intent = replace(intent, diff_hash=recompute_diff_hash(intent))
    auth = replace(
        auth, managed_binding=binding, issued_by=str(binding.user_id), diff_hash=intent.diff_hash
    )
    signer = ApprovalSigner.from_seed_b64(base64.b64encode(b"m" * 32).decode())
    auth = replace(auth, signature=signer.sign(authorization_signing_payload(auth)).hex())
    client = (
        _campaign_client(mutate_error=RuntimeError("synthetic-unknown"))
        if terminal == "FAILED"
        else _campaign_client()
    )
    adapter = GoogleAdsAdapter(_config(), client, clock, write_pipeline=pipeline)
    scope = ConnectionScope(binding.account.business_id, binding.account.connection_id)
    try:
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app), base_url="https://enterprise.invalid"
        ) as probe:
            check = await probe.post(
                "/internal/ads/admit-binding",
                json={"binding": binding.as_claims(), "operation": "execute"},
                headers={"Authorization": f"Bearer {secret}"},
            )
            assert check.status_code == 200, check.json()
        with connection_scope(scope):
            result = await adapter.execute_write(intent, auth, IdempotencyKey("original"))
            assert result.outcome == terminal, result.error_code
            service.revoke(
                str(binding.org_id),
                actor.user_id,
                str(binding.grant_id),
                operation_id=str(uuid4()),
                expected_revision=1,
            )
            denied = await adapter.execute_write(intent, auth, IdempotencyKey("new-attempt"))
            assert denied.error_code == "managed_admission_denied"
        assert ledger.get_outcome("new-attempt") is None
        assert len(client.status_mutations) == (1 if terminal == "SUCCEEDED" else 0)
        # Original receipt remains readable after EE revocation and local OAuth revocation.
        credentials.revoke_credential(ref, at=_NOW)
        assert pipeline.read_receipt("original", intent, auth).outcome == terminal
    finally:
        await authority.aclose()
        ledger.close()

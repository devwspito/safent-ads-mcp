"""Admission is request-local; no network call owns the SQLite write lock."""

import asyncio
import base64
from dataclasses import replace
from datetime import timedelta
from unittest.mock import AsyncMock
from uuid import uuid4

import pytest

from safent_ads.accounts.application.ports import WriteOperation, WriteOutcome
from safent_ads.broker.domain.ledger_scope import LedgerScopeError
from safent_ads.broker.domain.write_authorization import (
    authorization_signing_payload,
    recompute_diff_hash,
)
from safent_ads.broker.infrastructure.caps_config import parse_caps_config
from safent_ads.broker.infrastructure.write_ledger_store import WriteLedgerStore
from safent_ads.broker.platforms.write_pipeline import WriteAuthorizationPipeline
from safent_ads.execution.infrastructure.broker_platform import _signed
from safent_ads.iam.application.managed_ads_authority import ManagedAdsDenied, ManagedAdsUnavailable
from safent_ads.shared.clock import FixedClock
from safent_ads.shared.crypto.ed25519 import ApprovalSigner
from tests.unit.broker.ledger_scope_fakes import fake_scope
from tests.unit.broker.platforms.test_write_pipeline import _ACCOUNT, _CAPS_YAML, _NOW
from tests.unit.broker.test_managed_signed_context import signed


def setup(tmp_path, *, authority=True, resolver=fake_scope):
    intent, auth, verifier = signed()
    service = AsyncMock()
    service.admit_binding.return_value = intent.managed_binding
    ledger = WriteLedgerStore(tmp_path / "ledger.sqlite3")
    clock = FixedClock(_NOW)
    pipeline = WriteAuthorizationPipeline(
        verifier,
        parse_caps_config(_CAPS_YAML),
        ledger,
        scope_resolver=resolver,
        managed_authority=service if authority else None,
        clock=clock,
    )
    wire = replace(_signed(auth), issued_by=str(intent.managed_binding.user_id))
    signer = ApprovalSigner.from_seed_b64(base64.b64encode(b"m" * 32).decode())
    wire = replace(wire, signature=signer.sign(authorization_signing_payload(wire)).hex())
    return pipeline, intent, wire, ledger, service, clock


async def test_managed_requires_async_fresh_gate_even_if_service_configured(tmp_path):
    pipeline, intent, auth, ledger, service, _ = setup(tmp_path)
    assert (
        pipeline.begin_write("k", intent, auth, _ACCOUNT, _NOW).error_code
        == "managed_admission_unavailable"
    )
    service.admit_binding.assert_not_awaited()
    assert await pipeline.begin_admitted_write("k", intent, auth, _ACCOUNT, _NOW) is None
    service.admit_binding.assert_awaited_once_with(intent.managed_binding, operation="execute")
    assert ledger.pending_totals(fake_scope(intent, _ACCOUNT)) == (1, 0)


async def test_default_composition_gate_stays_closed(tmp_path):
    pipeline, intent, auth, ledger, service, _ = setup(tmp_path, authority=False)
    result = await pipeline.begin_admitted_write("k", intent, auth, _ACCOUNT, _NOW)
    assert result.error_code == "managed_admission_unavailable"
    assert ledger.get_outcome("k") is None
    service.admit_binding.assert_not_awaited()


async def test_central_profile_rejects_even_valid_local_owner_signature(tmp_path):
    pipeline, intent, auth, ledger, service, _ = setup(tmp_path)
    pipeline._require_managed_binding = True
    unmanaged = replace(intent, managed_binding=None)
    unmanaged = replace(unmanaged, diff_hash=recompute_diff_hash(unmanaged))
    local = replace(auth, managed_binding=None, diff_hash=unmanaged.diff_hash)
    signer = ApprovalSigner.from_seed_b64(base64.b64encode(b"m" * 32).decode())
    local = replace(local, signature=signer.sign(authorization_signing_payload(local)).hex())
    result = await pipeline.begin_admitted_write("local-owner", unmanaged, local, _ACCOUNT, _NOW)
    assert result.error_code == "managed_binding_mismatch"
    assert ledger.get_outcome("local-owner") is None
    service.admit_binding.assert_not_awaited()


@pytest.mark.parametrize("failure", [ManagedAdsDenied, ManagedAdsUnavailable])
async def test_rejected_or_unavailable_authority_cannot_reserve(tmp_path, failure):
    pipeline, intent, auth, ledger, service, _ = setup(tmp_path)
    service.admit_binding.side_effect = failure()
    result = await pipeline.begin_admitted_write("k", intent, auth, _ACCOUNT, _NOW)
    expected = (
        "managed_admission_denied"
        if failure is ManagedAdsDenied
        else "managed_admission_unavailable"
    )
    assert result.error_code == expected
    assert ledger.get_outcome("k") is None


async def test_admission_network_holds_no_sqlite_transaction(tmp_path):
    pipeline, intent, auth, _, service, _ = setup(tmp_path)
    independent = WriteLedgerStore(tmp_path / "ledger.sqlite3")

    async def admit(*args, **kwargs):
        independent.begin_transaction()
        independent.rollback()
        return intent.managed_binding

    service.admit_binding.side_effect = admit
    assert await pipeline.begin_admitted_write("k", intent, auth, _ACCOUNT, _NOW) is None


async def test_resolved_account_must_match_not_only_oauth_connection(tmp_path):
    def other_account(intent, account):
        return replace(fake_scope(intent, account), external_account_id="999")

    pipeline, intent, auth, ledger, service, _ = setup(tmp_path, resolver=other_account)
    result = await pipeline.begin_admitted_write("k", intent, auth, _ACCOUNT, _NOW)
    assert result.error_code == "managed_binding_mismatch"
    service.admit_binding.assert_not_awaited()
    assert ledger.get_outcome("k") is None


async def test_expiry_after_network_denies_before_receipt(tmp_path):
    pipeline, intent, auth, ledger, service, clock = setup(tmp_path)

    async def admit(*args, **kwargs):
        clock.advance_to(_NOW + timedelta(hours=1))
        return intent.managed_binding

    service.admit_binding.side_effect = admit
    result = await pipeline.begin_admitted_write("k", intent, auth, _ACCOUNT, _NOW)
    assert result.error_code == "authorization_expired"
    assert ledger.get_outcome("k") is None


async def test_binding_response_cannot_substitute_revision(tmp_path):
    pipeline, intent, auth, ledger, service, _ = setup(tmp_path)
    service.admit_binding.return_value = replace(intent.managed_binding, revision=2)
    result = await pipeline.begin_admitted_write("k", intent, auth, _ACCOUNT, _NOW)
    assert result.error_code == "managed_binding_mismatch"
    assert ledger.get_outcome("k") is None


@pytest.mark.parametrize("change", ["signature", "hash", "human_identity"])
async def test_invalid_approval_never_contacts_enterprise(tmp_path, change):
    pipeline, intent, auth, ledger, service, _ = setup(tmp_path)
    if change == "signature":
        auth = replace(auth, signature="00" * 64)
    elif change == "hash":
        intent = replace(intent, valor_propuesto="ACTIVE")
    else:
        signer = ApprovalSigner.from_seed_b64(base64.b64encode(b"m" * 32).decode())
        auth = replace(auth, issued_by="local-owner")
        auth = replace(auth, signature=signer.sign(authorization_signing_payload(auth)).hex())
    result = await pipeline.begin_admitted_write("k", intent, auth, _ACCOUNT, _NOW)
    assert result is not None
    assert ledger.get_outcome("k") is None
    service.admit_binding.assert_not_awaited()


async def test_credentials_revoked_during_admission_are_not_cached(tmp_path):
    revoked = False

    def resolver(intent, account):
        if revoked:
            raise LedgerScopeError("ledger_scope_unverified")
        return fake_scope(intent, account)

    pipeline, intent, auth, ledger, service, _ = setup(tmp_path, resolver=resolver)

    async def admit(*args, **kwargs):
        nonlocal revoked
        revoked = True
        return intent.managed_binding

    service.admit_binding.side_effect = admit
    assert (
        await pipeline.begin_admitted_write("k", intent, auth, _ACCOUNT, _NOW)
    ).error_code == "ledger_scope_unverified"
    assert ledger.get_outcome("k") is None


async def test_cancellation_leaves_no_receipt_or_sqlite_lock(tmp_path):
    pipeline, intent, auth, ledger, service, _ = setup(tmp_path)
    entered = asyncio.Event()

    async def admit(*args, **kwargs):
        entered.set()
        await asyncio.Event().wait()

    service.admit_binding.side_effect = admit
    task = asyncio.create_task(pipeline.begin_admitted_write("k", intent, auth, _ACCOUNT, _NOW))
    await entered.wait()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert ledger.get_outcome("k") is None
    ledger.begin_transaction()
    ledger.rollback()


async def test_two_concurrent_admissions_same_key_only_reserve_once(tmp_path):
    pipeline, intent, auth, ledger, service, _ = setup(tmp_path)
    entered = 0
    both = asyncio.Event()

    async def admit(*args, **kwargs):
        nonlocal entered
        entered += 1
        if entered == 2:
            both.set()
        await both.wait()
        return intent.managed_binding

    service.admit_binding.side_effect = admit
    outcomes = await asyncio.gather(
        *(pipeline.begin_admitted_write("k", intent, auth, _ACCOUNT, _NOW) for _ in range(2))
    )
    assert sum(result is None for result in outcomes) == 1
    assert [result.outcome for result in outcomes if result] == ["UNKNOWN"]
    assert ledger.pending_totals(fake_scope(intent, _ACCOUNT)) == (1, 0)


async def test_expiry_checked_after_wait_for_sqlite_lock(tmp_path, monkeypatch):
    pipeline, intent, auth, ledger, _, clock = setup(tmp_path)
    begin = ledger.begin_transaction

    def delayed_begin():
        begin()
        clock.advance_to(_NOW + timedelta(hours=1))

    monkeypatch.setattr(ledger, "begin_transaction", delayed_begin)
    result = await pipeline.begin_admitted_write("k", intent, auth, _ACCOUNT, _NOW)
    assert result.error_code == "authorization_expired"
    assert ledger.get_outcome("k") is None


async def test_fresh_expiry_clock_does_not_silently_move_accounting_day(tmp_path, monkeypatch):
    pipeline, intent, auth, _, service, clock = setup(tmp_path)
    # A long-valid synthetic signature lets a clock jump across midnight;
    # accounting must retain the timestamp that finalize receives from adapter.
    auth = replace(auth, expires_at=_NOW + timedelta(days=2))
    intent, auth = resign(intent, auth)
    seen = []
    original = pipeline._check_caps

    def check(intent, caps, scope, now):
        seen.append(now)
        return original(intent, caps, scope, now)

    monkeypatch.setattr(pipeline, "_check_caps", check)

    async def admit(*args, **kwargs):
        clock.advance_to(_NOW + timedelta(days=1))
        return intent.managed_binding

    service.admit_binding.side_effect = admit
    assert await pipeline.begin_admitted_write("k", intent, auth, _ACCOUNT, _NOW) is None
    assert seen == [_NOW]


def resign(intent, auth):
    intent = replace(intent, diff_hash=recompute_diff_hash(intent))
    auth = replace(auth, diff_hash=intent.diff_hash, managed_binding=intent.managed_binding)
    signer = ApprovalSigner.from_seed_b64(base64.b64encode(b"m" * 32).decode())
    return intent, replace(auth, signature=signer.sign(authorization_signing_payload(auth)).hex())


async def test_mutable_json_changed_during_network_fails_hash_before_effect(tmp_path):
    pipeline, intent, auth, ledger, service, _ = setup(tmp_path)
    after = {"amount": "60", "currency": "EUR"}
    intent, auth = resign(
        replace(
            intent,
            operation=WriteOperation.RAISE_BUDGET,
            parametro="daily_budget",
            valor_actual={"amount": "50", "currency": "EUR"},
            valor_propuesto=after,
        ),
        auth,
    )

    async def admit(*args, **kwargs):
        after["amount"] = "70"
        return intent.managed_binding

    service.admit_binding.side_effect = admit
    result = await pipeline.begin_admitted_write("k", intent, auth, _ACCOUNT, _NOW)
    assert result.error_code == "diff_hash_mismatch"
    assert ledger.get_outcome("k") is None


@pytest.mark.parametrize("same_business", [True, False])
async def test_multiple_grants_oauth_connections_keep_one_physical_budget(tmp_path, same_business):
    pipeline, intent, auth, ledger, service, _ = setup(tmp_path)
    pipeline._caps = parse_caps_config(
        _CAPS_YAML.replace("daily_cap_minor: 100000", "daily_cap_minor: 1000")
    )
    intent, auth = resign(
        replace(
            intent,
            operation=WriteOperation.RAISE_BUDGET,
            parametro="daily_budget",
            valor_actual={"amount": "50", "currency": "EUR"},
            valor_propuesto={"amount": "60", "currency": "EUR"},
        ),
        auth,
    )
    business = intent.managed_binding.account.business_id if same_business else uuid4()
    connection = uuid4()
    binding = replace(
        intent.managed_binding,
        grant_id=uuid4(),
        account=replace(
            intent.managed_binding.account, business_id=business, connection_id=connection
        ),
    )
    other, other_auth = resign(
        replace(
            intent,
            business_id=str(business),
            managed_binding=binding,
            entity_ref=replace(intent.entity_ref, business_id=business, connection_id=connection),
        ),
        auth,
    )
    entered = 0
    both = asyncio.Event()

    async def admit(binding, **kwargs):
        nonlocal entered
        entered += 1
        if entered == 2:
            both.set()
        await both.wait()
        return binding

    service.admit_binding.side_effect = admit
    results = await asyncio.gather(
        pipeline.begin_admitted_write("one", intent, auth, _ACCOUNT, _NOW),
        pipeline.begin_admitted_write("two", other, other_auth, _ACCOUNT, _NOW),
    )
    assert sum(result is None for result in results) == (1 if same_business else 2)
    assert all(result.error_code == "daily_cap_exceeded" for result in results if result)
    admitted_key, admitted_intent, admitted_auth = (
        ("one", intent, auth) if results[0] is None else ("two", other, other_auth)
    )
    pipeline.finalize(
        admitted_key,
        _ACCOUNT,
        admitted_intent,
        WriteOutcome("UNKNOWN", None, None, "synthetic-timeout", None),
        now=_NOW,
    )
    restarted = WriteLedgerStore(tmp_path / "ledger.sqlite3")
    assert restarted.pending_totals(fake_scope(admitted_intent, _ACCOUNT)) == (1, 1000)
    service.admit_binding.side_effect = ManagedAdsDenied()
    assert pipeline.read_receipt(admitted_key, admitted_intent, admitted_auth).outcome == "UNKNOWN"
    assert restarted.pending_totals(fake_scope(admitted_intent, _ACCOUNT)) == (1, 1000)
    restarted.close()

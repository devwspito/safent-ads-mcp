"""Broker hard caps must not share money between tenants or providers."""

import base64
import json
import sqlite3
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from datetime import timedelta
from pathlib import Path
from threading import Barrier
from uuid import UUID

import pytest

from safent_ads.accounts.application.ports import WriteIntent, WriteOperation, WriteOutcome
from safent_ads.accounts.domain.refs import CredentialRefId
from safent_ads.broker.application.connection_scope import ConnectionScope, connection_scope
from safent_ads.broker.application.ports import CredentialRecord
from safent_ads.broker.domain.ledger_scope import LedgerScopeError
from safent_ads.broker.domain.write_authorization import authorization_signing_payload
from safent_ads.broker.infrastructure.caps_config import parse_caps_config
from safent_ads.broker.infrastructure.connected_credential_store import ConnectedCredentialStore
from safent_ads.broker.infrastructure.credential_store import EncryptedCredentialStore
from safent_ads.broker.infrastructure.write_ledger_store import (
    _SCHEMA,
    WriteLedgerStore,
    _fingerprint,
)
from safent_ads.broker.platforms.write_pipeline import WriteAuthorizationPipeline
from safent_ads.proposals.domain.diff_hash import compute_diff_hash
from safent_ads.shared.clock import FixedClock
from safent_ads.shared.ids import PlatformCode
from tests.unit.broker.ledger_scope_fakes import fake_scope
from tests.unit.broker.platforms.test_write_pipeline import (
    _ACCOUNT,
    _CAPS_YAML,
    _NOW,
    _authorization,
    _intent,
    _signer_and_verifier,
)

_BUSINESS_A = UUID("aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa")
_BUSINESS_B = UUID("bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb")
_CONNECTION_A = UUID("11111111-1111-4111-8111-111111111111")
_CONNECTION_B = UUID("22222222-2222-4222-8222-222222222222")


def scoped_intent(
    *,
    business: UUID = _BUSINESS_A,
    platform: PlatformCode = PlatformCode.GOOGLE,
    connection: UUID = _CONNECTION_A,
) -> WriteIntent:
    intent = _intent(
        before={"amount": "70", "currency": "EUR"},
        after={"amount": "90", "currency": "EUR"},
        parametro="daily_budget",
        operation=WriteOperation.RAISE_BUDGET,
    )
    ref = replace(
        intent.entity_ref, platform=platform, business_id=business, connection_id=connection
    )
    return replace(
        intent,
        entity_ref=ref,
        business_id=str(business),
        diff_hash=compute_diff_hash(
            ref, intent.parametro, intent.valor_actual, intent.valor_propuesto
        ),
    )


def limited_pipeline(path: Path):
    signer, verifier = _signer_and_verifier()
    store = WriteLedgerStore(path)
    caps = parse_caps_config(_CAPS_YAML.replace("daily_cap_minor: 100000", "daily_cap_minor: 2000"))
    return (
        signer,
        store,
        WriteAuthorizationPipeline(verifier, caps, store, scope_resolver=fake_scope),
    )


@pytest.mark.parametrize("other_scope", ["business", "platform"])
def test_same_remote_account_does_not_mix_businesses_or_platforms(
    tmp_path: Path,
    other_scope: str,
) -> None:
    signer, store, pipeline = limited_pipeline(tmp_path / "ledger.db")
    first = scoped_intent()
    second = (
        scoped_intent(business=_BUSINESS_B)
        if other_scope == "business"
        else scoped_intent(platform=PlatformCode.META)
    )
    try:
        assert (
            pipeline.begin_write(
                "first", first, _authorization(signer, diff_hash=first.diff_hash), _ACCOUNT, _NOW
            )
            is None
        )
        assert (
            pipeline.begin_write(
                "second", second, _authorization(signer, diff_hash=second.diff_hash), _ACCOUNT, _NOW
            )
            is None
        )
    finally:
        store.close()


def test_two_connections_share_one_physical_cap_under_concurrency(tmp_path: Path) -> None:
    path = tmp_path / "ledger.db"
    WriteLedgerStore(path).close()
    barrier = Barrier(2)

    def reserve(connection: UUID):
        signer, store, pipeline = limited_pipeline(path)
        intent = scoped_intent(connection=connection)
        try:
            barrier.wait(timeout=5)
            return pipeline.begin_write(
                str(connection),
                intent,
                _authorization(signer, diff_hash=intent.diff_hash),
                _ACCOUNT,
                _NOW,
            )
        finally:
            store.close()

    with ThreadPoolExecutor(max_workers=2) as workers:
        outcomes = list(workers.map(reserve, (_CONNECTION_A, _CONNECTION_B)))
    assert sum(outcome is None for outcome in outcomes) == 1
    assert {outcome.error_code for outcome in outcomes if outcome} == {"daily_cap_exceeded"}
    store = WriteLedgerStore(path)
    try:
        assert store.pending_totals(fake_scope(scoped_intent(), _ACCOUNT)) == (1, 2000)
    finally:
        store.close()


@pytest.mark.parametrize("outcome", [None, "UNKNOWN", "FAILED"])
def test_unknown_reservation_survives_restart_and_never_moves_to_another_connection(
    tmp_path: Path,
    outcome: str | None,
) -> None:
    path = tmp_path / "ledger.db"
    signer, store, pipeline = limited_pipeline(path)
    first = scoped_intent()
    auth = _authorization(signer, diff_hash=first.diff_hash)
    assert pipeline.begin_write("first", first, auth, _ACCOUNT, _NOW) is None
    if outcome:
        pipeline.finalize(
            "first",
            _ACCOUNT,
            first,
            WriteOutcome(outcome, None, None, "remote_outcome_unknown", None),
            now=_NOW,
        )
    store.close()
    signer, store, pipeline = limited_pipeline(path)
    second = scoped_intent(connection=_CONNECTION_B)
    later = _NOW + timedelta(days=70)
    # A fresh authorization still cannot erase the old reservation by aging it.
    unsigned = replace(
        _authorization(signer, diff_hash=second.diff_hash), expires_at=later + timedelta(hours=1)
    )
    fresh = replace(unsigned, signature=signer.sign(authorization_signing_payload(unsigned)).hex())
    try:
        denial = pipeline.begin_write("second", second, fresh, _ACCOUNT, later)
        assert denial is not None and denial.error_code == "daily_cap_exceeded"
        assert store.pending_totals(fake_scope(first, _ACCOUNT)) == (1, 2000)
        assert pipeline.read_receipt("first", first, auth) == (
            None
            if outcome is None
            else WriteOutcome(outcome, None, None, "remote_outcome_unknown", None)
        )
    finally:
        store.close()


@pytest.mark.parametrize("tamper", ["business", "connection", "account", "payload"])
def test_idempotency_key_cannot_move_or_settle_another_scope(tmp_path: Path, tamper: str) -> None:
    signer, store, pipeline = limited_pipeline(tmp_path / "ledger.db")
    first = scoped_intent()
    auth = _authorization(signer, diff_hash=first.diff_hash)
    changed = {
        "business": scoped_intent(business=_BUSINESS_B),
        "connection": scoped_intent(connection=_CONNECTION_B),
        "account": first,
        "payload": replace(first, expected_state_hash="tampered"),
    }[tamper]
    account = "999" if tamper == "account" else _ACCOUNT
    try:
        assert pipeline.begin_write("key", first, auth, _ACCOUNT, _NOW) is None
        denied = pipeline.begin_write(
            "key", changed, _authorization(signer, diff_hash=changed.diff_hash), account, _NOW
        )
        assert denied is not None and denied.outcome == "DENIED"
        with pytest.raises(LedgerScopeError, match="receipt_(scope|payload)_mismatch"):
            pipeline.finalize(
                "key",
                account,
                changed,
                WriteOutcome("SUCCEEDED", "90", "state", None, "request"),
                now=_NOW,
            )
        assert store.pending_totals(fake_scope(first, _ACCOUNT)) == (1, 2000)
        assert pipeline.read_receipt("key", first, auth) is None
    finally:
        store.close()


def test_finalize_retry_on_another_day_does_not_duplicate_money(tmp_path: Path) -> None:
    signer, store, pipeline = limited_pipeline(tmp_path / "ledger.db")
    intent = scoped_intent()
    auth = _authorization(signer, diff_hash=intent.diff_hash)
    outcome = WriteOutcome("SUCCEEDED", "90", "state", None, "request")
    try:
        assert pipeline.begin_write("key", intent, auth, _ACCOUNT, _NOW) is None
        assert pipeline.finalize("key", _ACCOUNT, intent, outcome, now=_NOW) == outcome
        assert (
            pipeline.finalize("key", _ACCOUNT, intent, outcome, now=_NOW + timedelta(days=1))
            == outcome
        )
        scope = fake_scope(intent, _ACCOUNT)
        assert store.month_to_date_delta(scope, _NOW.date()) == 2000
        assert store.snapshot_today(scope, (_NOW + timedelta(days=1)).date()).changes_count == 0
        assert store.pending_totals(scope) == (0, 0)
    finally:
        store.close()


@pytest.mark.parametrize(
    "legacy_account,blocked",
    [(_ACCOUNT, True), ("act_" + _ACCOUNT, True), ("999999999", False), ("unparseable", True)],
)
def test_additive_upgrade_does_not_guess_legacy_tenant_or_block_provably_other_accounts(
    tmp_path: Path,
    legacy_account: str,
    blocked: bool,
) -> None:
    path = tmp_path / "ledger.db"
    with sqlite3.connect(path) as old:
        old.execute(
            "CREATE TABLE applied_changes (platform_account_id TEXT NOT NULL, "
            "change_date TEXT NOT NULL, delta_minor_units INTEGER NOT NULL, "
            "idempotency_key TEXT NOT NULL, "
            "PRIMARY KEY(platform_account_id,change_date,idempotency_key))"
        )
        old.execute(
            "INSERT INTO applied_changes VALUES (?,?,?,?)",
            (legacy_account, "2020-01-01", 0, "legacy"),
        )
        original = old.execute("SELECT * FROM applied_changes").fetchall()
    signer, store, pipeline = limited_pipeline(path)
    intent = scoped_intent()
    try:
        denial = pipeline.begin_write(
            "key", intent, _authorization(signer, diff_hash=intent.diff_hash), _ACCOUNT, _NOW
        )
        if blocked:
            assert denial is not None and denial.error_code == "legacy_ledger_scope_unresolved"
        else:
            assert denial is None
            # Other accounts stay usable, but the old idempotency key itself
            # cannot be recycled where its original fingerprint is unavailable.
            reused = pipeline.begin_write(
                "legacy", intent, _authorization(signer, diff_hash=intent.diff_hash), _ACCOUNT, _NOW
            )
            assert reused is not None and reused.error_code == "legacy_ledger_scope_unresolved"
    finally:
        store.close()
    with sqlite3.connect(path) as upgraded:
        assert (
            upgraded.execute(
                "SELECT platform_account_id,change_date,delta_minor_units,"
                "idempotency_key FROM applied_changes"
            ).fetchall()
            == original
        )
        assert upgraded.execute("SELECT scope_key FROM applied_changes").fetchall() == [(None,)]


@pytest.mark.parametrize("outcome", ["SUCCEEDED", "UNKNOWN", "FAILED"])
def test_orphan_legacy_result_without_any_account_blocks_all_new_writes(
    tmp_path: Path,
    outcome: str,
) -> None:
    signer, store, pipeline = limited_pipeline(tmp_path / "ledger.db")
    store.record_outcome_if_absent("orphan", WriteOutcome(outcome, None, None, None, None))
    intent = scoped_intent(business=_BUSINESS_B)
    try:
        denial = pipeline.begin_write(
            "key", intent, _authorization(signer, diff_hash=intent.diff_hash), _ACCOUNT, _NOW
        )
        assert denial is not None and denial.error_code == "legacy_ledger_scope_unresolved"
    finally:
        store.close()


def test_legacy_receipt_keeps_original_signature_and_payload_after_upgrade(tmp_path: Path) -> None:
    path = tmp_path / "ledger.db"
    signer, _ = _signer_and_verifier()
    intent = scoped_intent()
    auth = _authorization(signer, diff_hash=intent.diff_hash)
    raw = json.dumps(
        {
            "outcome": "UNKNOWN",
            "applied_value": None,
            "state_hash_after": None,
            "error_code": "timeout",
            "platform_request_id": None,
        }
    )
    with sqlite3.connect(path) as old:
        old.executescript(_SCHEMA.replace("    scope_key TEXT,\n", ""))
        old.execute(
            "INSERT INTO write_receipts VALUES (?,?,?,?,?,?,?,?,?,?)",
            (
                "legacy",
                intent.business_id,
                str(intent.entity_ref),
                intent.diff_hash,
                _fingerprint(intent, auth.authorization_id),
                auth.authorization_id,
                _ACCOUNT,
                2000,
                raw,
                1,
            ),
        )
        original = old.execute("SELECT * FROM write_receipts").fetchone()
    _, store, pipeline = limited_pipeline(path)
    try:
        assert pipeline.read_receipt("legacy", intent, auth).outcome == "UNKNOWN"
        assert pipeline.begin_write("legacy", intent, auth, _ACCOUNT, _NOW).error_code == (
            "legacy_ledger_scope_unresolved"
        )
        denied = pipeline.begin_write("new", intent, auth, _ACCOUNT, _NOW)
        assert denied.error_code == "legacy_ledger_scope_unresolved"
    finally:
        store.close()
    with sqlite3.connect(path) as upgraded:
        assert upgraded.execute("SELECT * FROM write_receipts").fetchone() == (*original, None)


@pytest.mark.parametrize(
    "failure", ["missing", "business", "connection", "unbound", "revoked", "expired"]
)
def test_production_resolver_requires_signed_scope_and_live_encrypted_binding(
    tmp_path: Path,
    failure: str,
) -> None:
    credentials = EncryptedCredentialStore(
        tmp_path / "credentials", base64.b64encode(b"0" * 32).decode()
    )
    ref = CredentialRefId(_CONNECTION_A)
    intent = scoped_intent()
    record = CredentialRecord(
        PlatformCode.GOOGLE,
        "test-only-canary",
        "refresh_token",
        (),
        _NOW,
        _NOW if failure == "expired" else None,
        business_id=str(_BUSINESS_A),
        connection_id=str(_CONNECTION_A),
    )
    credentials.save_credential(ref, record)
    if failure != "unbound":
        credentials.bind_account_credential(
            PlatformCode.GOOGLE,
            _ACCOUNT,
            ref,
            business_id=str(_BUSINESS_A),
            connection_id=str(_CONNECTION_A),
        )
    if failure == "revoked":
        credentials.revoke_credential(ref, at=_NOW)
    connected = ConnectedCredentialStore(credentials, FixedClock(_NOW))
    signer, verifier = _signer_and_verifier()
    store = WriteLedgerStore(tmp_path / "ledger.db")
    pipeline = WriteAuthorizationPipeline(
        verifier, parse_caps_config(_CAPS_YAML), store, scope_resolver=connected.resolve_write_scope
    )
    context = (
        None
        if failure == "missing"
        else ConnectionScope(
            _BUSINESS_B if failure == "business" else _BUSINESS_A,
            _CONNECTION_B if failure == "connection" else _CONNECTION_A,
        )
    )
    try:
        with connection_scope(context):
            denial = pipeline.begin_write(
                "key", intent, _authorization(signer, diff_hash=intent.diff_hash), _ACCOUNT, _NOW
            )
        assert denial is not None and denial.error_code == "ledger_scope_unverified"
        assert store.pending_totals(fake_scope(intent, _ACCOUNT)) == (0, 0)
    finally:
        store.close()


def test_revocation_between_authorize_and_reserve_is_rechecked_but_finalization_is_durable(
    tmp_path: Path,
) -> None:
    credentials = EncryptedCredentialStore(
        tmp_path / "credentials", base64.b64encode(b"0" * 32).decode()
    )
    ref = CredentialRefId(_CONNECTION_A)
    credentials.save_credential(
        ref,
        CredentialRecord(
            PlatformCode.GOOGLE,
            "test-only-canary",
            "refresh_token",
            (),
            _NOW,
            None,
            business_id=str(_BUSINESS_A),
            connection_id=str(_CONNECTION_A),
        ),
    )
    credentials.bind_account_credential(
        PlatformCode.GOOGLE,
        _ACCOUNT,
        ref,
        business_id=str(_BUSINESS_A),
        connection_id=str(_CONNECTION_A),
    )
    connected = ConnectedCredentialStore(credentials, FixedClock(_NOW))
    signer, verifier = _signer_and_verifier()
    store = WriteLedgerStore(tmp_path / "ledger.db")
    pipeline = WriteAuthorizationPipeline(
        verifier, parse_caps_config(_CAPS_YAML), store, scope_resolver=connected.resolve_write_scope
    )
    intent = scoped_intent()
    auth = _authorization(signer, diff_hash=intent.diff_hash)
    try:
        with connection_scope(ConnectionScope(_BUSINESS_A, _CONNECTION_A)):
            assert (
                pipeline.authorize(
                    intent,
                    auth,
                    platform_account_id=_ACCOUNT,
                    remote_state_hash=intent.expected_state_hash,
                    now=_NOW,
                )
                is None
            )
            assert pipeline.begin_write("dispatched", intent, auth, _ACCOUNT, _NOW) is None
            credentials.revoke_credential(ref, at=_NOW)
            assert (
                pipeline.begin_write("new", intent, auth, _ACCOUNT, _NOW).error_code
                == "ledger_scope_unverified"
            )
        outcome = WriteOutcome("SUCCEEDED", "90", "state", None, "request")
        assert pipeline.finalize("dispatched", _ACCOUNT, intent, outcome, now=_NOW) == outcome
        assert pipeline.read_receipt("dispatched", intent, auth) == outcome
        assert (
            store.snapshot_today(
                fake_scope(intent, _ACCOUNT), _NOW.date()
            ).applied_delta_minor_units
            == 2000
        )
    finally:
        store.close()


def test_distinct_tenants_can_reserve_concurrently_without_mixing_caps(tmp_path: Path) -> None:
    path = tmp_path / "ledger.db"
    WriteLedgerStore(path).close()
    barrier = Barrier(2)

    def reserve(business: UUID):
        signer, store, pipeline = limited_pipeline(path)
        intent = scoped_intent(business=business)
        try:
            barrier.wait(timeout=5)
            return pipeline.begin_write(
                str(business),
                intent,
                _authorization(signer, diff_hash=intent.diff_hash),
                _ACCOUNT,
                _NOW,
            )
        finally:
            store.close()

    with ThreadPoolExecutor(max_workers=2) as workers:
        outcomes = list(workers.map(reserve, (_BUSINESS_A, _BUSINESS_B)))
    assert outcomes == [None, None]


def test_legacy_unattributed_change_is_not_assigned_to_first_new_tenant(tmp_path: Path) -> None:
    path = tmp_path / "ledger.db"
    # This is the pre-scope store API/data shape, not an authorized new write.
    legacy = WriteLedgerStore(path)
    legacy.close()
    with sqlite3.connect(path) as connection:
        connection.execute(
            "INSERT INTO applied_changes "
            "(platform_account_id,change_date,idempotency_key,delta_minor_units) "
            "VALUES (?,?,?,?)",
            (_ACCOUNT, _NOW.date().isoformat(), "old-key", 0),
        )
    signer, store, pipeline = limited_pipeline(path)
    intent = scoped_intent()
    try:
        outcome = pipeline.begin_write(
            "new", intent, _authorization(signer, diff_hash=intent.diff_hash), _ACCOUNT, _NOW
        )
        assert outcome is not None and outcome.outcome == "DENIED"
        assert outcome.error_code == "legacy_ledger_scope_unresolved"
    finally:
        store.close()

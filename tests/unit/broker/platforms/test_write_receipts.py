"""Persistent read-only receipts, never a replay that grants new authority."""

from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from datetime import timedelta
from pathlib import Path
from threading import Barrier

import pytest

from safent_ads.accounts.application.ports import WriteOperation, WriteOutcome
from safent_ads.broker.infrastructure.caps_config import parse_caps_config
from safent_ads.broker.infrastructure.write_ledger_store import WriteLedgerStore
from safent_ads.broker.platforms.write_pipeline import WriteAuthorizationPipeline
from tests.unit.broker.ledger_scope_fakes import fake_scope
from tests.unit.broker.platforms.test_write_pipeline import (
    _ACCOUNT,
    _CAPS_YAML,
    _NOW,
    _STATE_HASH,
    _authorization,
    _intent,
    _signer_and_verifier,
)


def test_receipt_survives_restart_and_expired_authorization_is_read_only(tmp_path: Path) -> None:
    signer, verifier = _signer_and_verifier()
    intent = replace(_intent(), business_id="aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa")
    auth = _authorization(signer, diff_hash=intent.diff_hash)
    path = tmp_path / "ledger.db"
    ledger = WriteLedgerStore(path)
    pipeline = WriteAuthorizationPipeline(
        verifier, parse_caps_config(_CAPS_YAML), ledger, scope_resolver=fake_scope
    )
    assert pipeline.begin_write("key", intent, auth, _ACCOUNT, _NOW) is None
    assert pipeline.read_receipt("key", intent, auth) is None
    expected = WriteOutcome("SUCCEEDED", "PAUSED", "a" * 64, None, "request-1")
    pipeline.finalize("key", _ACCOUNT, intent, expected, now=_NOW)
    ledger.close()
    ledger = WriteLedgerStore(path)
    pipeline = WriteAuthorizationPipeline(
        verifier, parse_caps_config(_CAPS_YAML), ledger, scope_resolver=fake_scope
    )
    try:
        assert (
            pipeline.authorize(
                intent,
                auth,
                platform_account_id=_ACCOUNT,
                remote_state_hash=_STATE_HASH,
                now=_NOW + timedelta(days=40),
            ).outcome
            == "DENIED"
        )
        assert pipeline.read_receipt("key", intent, auth) == expected
        assert pipeline.read_receipt("key", replace(intent, business_id="other"), auth) is None
        assert (
            pipeline.read_receipt("key", replace(intent, expected_state_hash="other"), auth) is None
        )
        with pytest.raises(ValueError, match="receipt_payload_mismatch"):
            pipeline.read_receipt("key", replace(intent, valor_propuesto="ACTIVE"), auth)
        with pytest.raises(ValueError, match="receipt_signature_invalid"):
            pipeline.read_receipt("key", intent, replace(auth, signature="00" * 64))
        assert ledger.snapshot_today(fake_scope(intent, _ACCOUNT), _NOW.date()).changes_count == 1
    finally:
        ledger.close()


def test_broker_reservation_is_atomic_across_connections(tmp_path: Path) -> None:
    signer, verifier = _signer_and_verifier()
    intent = replace(
        _intent(
            before={"amount": "70", "currency": "EUR"},
            after={"amount": "90", "currency": "EUR"},
            parametro="daily_budget",
            operation=WriteOperation.RAISE_BUDGET,
        ),
        business_id="aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa",
    )
    auth = _authorization(signer, diff_hash=intent.diff_hash)
    path = tmp_path / "ledger.db"
    WriteLedgerStore(path).close()
    barrier = Barrier(2)

    def reserve(key: str) -> WriteOutcome | None:
        ledger = WriteLedgerStore(path)
        try:
            caps = parse_caps_config(
                _CAPS_YAML.replace("daily_cap_minor: 100000", "daily_cap_minor: 2000")
            )
            pipeline = WriteAuthorizationPipeline(verifier, caps, ledger, scope_resolver=fake_scope)
            barrier.wait(timeout=5)
            return pipeline.begin_write(key, intent, auth, _ACCOUNT, _NOW)
        finally:
            ledger.close()

    with ThreadPoolExecutor(max_workers=2) as workers:
        outcomes = list(workers.map(reserve, ["key-1", "key-2"]))
    assert sum(outcome is None for outcome in outcomes) == 1
    assert {outcome.outcome for outcome in outcomes if outcome is not None} == {"BLOCKED_HARD_CAP"}
    ledger = WriteLedgerStore(path)
    try:
        assert ledger.pending_totals(fake_scope(intent, _ACCOUNT)) == (1, 2000)
    finally:
        ledger.close()


def test_pending_or_failed_receipt_never_releases_broker_reservation(tmp_path: Path) -> None:
    signer, verifier = _signer_and_verifier()
    intent = replace(_intent(), business_id="aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa")
    auth = _authorization(signer, diff_hash=intent.diff_hash)
    ledger = WriteLedgerStore(tmp_path / "ledger.db")
    pipeline = WriteAuthorizationPipeline(
        verifier, parse_caps_config(_CAPS_YAML), ledger, scope_resolver=fake_scope
    )
    try:
        assert pipeline.begin_write("key", intent, auth, _ACCOUNT, _NOW) is None
        assert pipeline.begin_write("key", intent, auth, _ACCOUNT, _NOW).outcome == "UNKNOWN"
        assert ledger.pending_totals(fake_scope(intent, _ACCOUNT)) == (1, 0)
        pipeline.finalize(
            "key", _ACCOUNT, intent, WriteOutcome("FAILED", None, None, "timeout", None), now=_NOW
        )
        assert ledger.pending_totals(fake_scope(intent, _ACCOUNT)) == (1, 0)
        assert pipeline.read_receipt("key", intent, auth).outcome == "FAILED"
        assert ledger.snapshot_today(fake_scope(intent, _ACCOUNT), _NOW.date()).changes_count == 0
    finally:
        ledger.close()

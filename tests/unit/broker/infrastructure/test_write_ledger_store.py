"""`WriteLedgerStore`: idempotencia (C-8) y contador propio de cambios
aplicados por cuenta y dia (C-17), sobre un SQLite propio del broker."""

from __future__ import annotations

from datetime import date
from pathlib import Path

from safent_ads.accounts.application.ports import WriteIntent, WriteOperation, WriteOutcome
from safent_ads.broker.domain.ledger_scope import LedgerScope
from safent_ads.broker.infrastructure.write_ledger_store import WriteLedgerStore
from safent_ads.shared.ids import EntityLevel, EntityRef, PlatformCode
from tests.unit.broker.ledger_scope_fakes import BUSINESS

_TODAY = date(2026, 9, 9)


def _store(tmp_path: Path) -> WriteLedgerStore:
    return WriteLedgerStore(tmp_path / "broker" / "write_ledger.sqlite3")


def _outcome(**overrides: object) -> WriteOutcome:
    defaults: dict[str, object] = {
        "outcome": "SUCCEEDED",
        "applied_value": {"amount": "70.00", "currency": "EUR"},
        "state_hash_after": "a" * 64,
        "error_code": None,
        "platform_request_id": "req-1",
    }
    defaults.update(overrides)
    return WriteOutcome(**defaults)  # type: ignore[arg-type]


def test_creates_the_db_file_with_owner_only_permissions(tmp_path: Path) -> None:
    db_path = tmp_path / "broker" / "write_ledger.sqlite3"

    WriteLedgerStore(db_path)

    assert db_path.is_file()
    assert oct(db_path.stat().st_mode)[-3:] == "600"


def test_unknown_idempotency_key_has_no_outcome(tmp_path: Path) -> None:
    store = _store(tmp_path)

    assert store.get_outcome("unknown") is None


def test_record_outcome_if_absent_persists_it(tmp_path: Path) -> None:
    store = _store(tmp_path)
    outcome = _outcome()

    persisted = store.record_outcome_if_absent("key-1", outcome)

    assert persisted == outcome
    assert store.get_outcome("key-1") == outcome


def test_replay_returns_original_outcome(tmp_path: Path) -> None:
    """Comprobacion 8: una clave ya vista no se vuelve a registrar con un
    desenlace distinto -- gana el primero, sin una segunda mutacion."""
    store = _store(tmp_path)
    original = _outcome(state_hash_after="a" * 64)
    replayed_calculation = _outcome(state_hash_after="b" * 64)

    store.record_outcome_if_absent("key-1", original)
    result = store.record_outcome_if_absent("key-1", replayed_calculation)

    assert result == original
    assert store.get_outcome("key-1") == original


def test_recibo_de_paso_casa_tras_reacunar_la_autorizacion(tmp_path: Path) -> None:
    """`003-paquete-de-campana` data-model.md Revision 2 SS R2.6 (BL-5,
    T110): cada reanudacion de un paso de paquete re-acuna una
    `AuthorizationId` nueva; la huella del recibo no debe llevarla, o el
    paso queda atascado para siempre en `DIFF_HASH_MISMATCH`."""
    store = _store(tmp_path)
    ref = EntityRef(PlatformCode.GOOGLE, EntityLevel.AD, "customers/123/adGroupAds/1")
    intent = WriteIntent(
        entity_ref=ref,
        operation=WriteOperation.CREATE_AD,
        parametro="new_ad:pmax",
        valor_actual=None,
        valor_propuesto={"headline": "Ejemplo"},
        diff_hash="d" * 64,
        expected_state_hash="",
        business_id=str(BUSINESS),
        package_binding={
            "package_hash": "p" * 64,
            "publication_id": "pub-1",
            "step_index": 2,
        },
    )
    scope = LedgerScope(BUSINESS, PlatformCode.GOOGLE, "123")

    assert store.begin_receipt("step-key", intent, "auth-original", scope, 0)

    assert store.receipt_matches("step-key", intent, "auth-reacunada-tras-continuar")


def test_recibo_de_una_escritura_normal_no_casa_si_cambia_la_autorizacion(
    tmp_path: Path,
) -> None:
    """Fuera de un paso de paquete la huella SI incluye `authorization_id`:
    T110 es una excepcion declarada para `package_step`, no una relajacion
    general del control de idempotencia (comprobacion 8)."""
    store = _store(tmp_path)
    ref = EntityRef(PlatformCode.GOOGLE, EntityLevel.CAMPAIGN, "customers/123/campaigns/1")
    intent = WriteIntent(
        entity_ref=ref,
        operation=WriteOperation.PAUSE,
        parametro="status",
        valor_actual="ACTIVE",
        valor_propuesto="PAUSED",
        diff_hash="d" * 64,
        expected_state_hash="s" * 64,
        business_id=str(BUSINESS),
    )
    scope = LedgerScope(BUSINESS, PlatformCode.GOOGLE, "123")

    assert store.begin_receipt("normal-key", intent, "auth-original", scope, 0)

    assert not store.receipt_matches("normal-key", intent, "auth-distinta")


def test_snapshot_today_is_zero_before_any_applied_change(tmp_path: Path) -> None:
    snapshot = _store(tmp_path).snapshot_today(
        LedgerScope(BUSINESS, PlatformCode.GOOGLE, "123"), _TODAY
    )

    assert snapshot.changes_count == 0
    assert snapshot.applied_delta_minor_units == 0


def test_snapshot_today_counts_applied_changes_for_that_account_and_day(tmp_path: Path) -> None:
    store = _store(tmp_path)
    store.record_applied_change(
        LedgerScope(BUSINESS, PlatformCode.GOOGLE, "123"), _TODAY, "key-1", -3_000
    )
    store.record_applied_change(
        LedgerScope(BUSINESS, PlatformCode.GOOGLE, "123"), _TODAY, "key-2", 1_000
    )
    store.record_applied_change(
        LedgerScope(BUSINESS, PlatformCode.GOOGLE, "999"), _TODAY, "key-3", 5_000
    )
    store.record_applied_change(
        LedgerScope(BUSINESS, PlatformCode.GOOGLE, "123"), date(2026, 9, 8), "key-4", 9_000
    )

    snapshot = store.snapshot_today(LedgerScope(BUSINESS, PlatformCode.GOOGLE, "123"), _TODAY)

    assert snapshot.changes_count == 2
    assert snapshot.applied_delta_minor_units == -2_000


def test_record_applied_change_is_idempotent_per_key(tmp_path: Path) -> None:
    store = _store(tmp_path)

    store.record_applied_change(
        LedgerScope(BUSINESS, PlatformCode.GOOGLE, "123"), _TODAY, "key-1", 1_000
    )
    store.record_applied_change(
        LedgerScope(BUSINESS, PlatformCode.GOOGLE, "123"), _TODAY, "key-1", 1_000
    )

    snapshot = store.snapshot_today(LedgerScope(BUSINESS, PlatformCode.GOOGLE, "123"), _TODAY)
    assert snapshot.changes_count == 1
    assert snapshot.applied_delta_minor_units == 1_000


def test_month_to_date_delta_sums_the_whole_month_not_just_today(tmp_path: Path) -> None:
    store = _store(tmp_path)
    store.record_applied_change(
        LedgerScope(BUSINESS, PlatformCode.GOOGLE, "123"), date(2026, 9, 1), "key-1", 2_000
    )
    store.record_applied_change(
        LedgerScope(BUSINESS, PlatformCode.GOOGLE, "123"), _TODAY, "key-2", 3_000
    )
    store.record_applied_change(
        LedgerScope(BUSINESS, PlatformCode.GOOGLE, "123"), date(2026, 8, 31), "key-3", 9_000
    )

    assert (
        store.month_to_date_delta(LedgerScope(BUSINESS, PlatformCode.GOOGLE, "123"), _TODAY)
        == 5_000
    )


def test_ledger_survives_reopening_the_same_file(tmp_path: Path) -> None:
    db_path = tmp_path / "broker" / "write_ledger.sqlite3"
    WriteLedgerStore(db_path).record_applied_change(
        LedgerScope(BUSINESS, PlatformCode.GOOGLE, "123"), _TODAY, "key-1", 4_200
    )

    reopened = WriteLedgerStore(db_path)

    assert (
        reopened.snapshot_today(
            LedgerScope(BUSINESS, PlatformCode.GOOGLE, "123"), _TODAY
        ).applied_delta_minor_units
        == 4_200
    )

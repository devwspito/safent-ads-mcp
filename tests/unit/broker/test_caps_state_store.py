"""Almacen de topes del panel (`ADS_BROKER_CAPS_STATE_DIR`) -- spec 008
T029. Invariantes i2 a i7 de la revision T027.

Todas cuelgan del mismo teorema: ignorar el estado del panel es siempre
igual o mas restrictivo que aplicarlo. Por eso ningun modo de fallo tiene
un caso especial -- todos caen al fichero."""

from __future__ import annotations

import asyncio
import json
import os
from datetime import UTC, date, datetime
from pathlib import Path

import pytest

from safent_ads.broker.domain.hard_caps_policy import CapSource, PanelAmounts
from safent_ads.broker.infrastructure.caps_config import CapsConfigError, parse_caps_config
from safent_ads.broker.infrastructure.caps_state import (
    STATE_SCHEMA_VERSION,
    CapsStateStore,
    CapsStateUnwritableError,
    PanelAccountCaps,
    PanelCapsSnapshot,
    assert_state_directory_is_private,
    prune_change_days,
)
from safent_ads.broker.infrastructure.effective_caps import EffectiveCapsResolver

_ACCOUNT = "1234567890"
_OTHER_ACCOUNT = "act_9999"

_FILE_YAML = """
defaults:
  max_step_pct: 30
  max_changes_per_day: 2
  autonomy_enabled: false

accounts:
  "1234567890":
    daily_cap_minor: 4000
    monthly_cap_minor: 80000
    floor_minor: 200
    ceiling_minor: 15000
"""

_ENVELOPE_BLOCK = """
panel_managed:
  currency: EUR
  max_daily_cap_minor: 5000
  max_monthly_cap_minor: 100000
  max_ceiling_minor: 20000
  min_floor_minor: 500
  max_accounts: 2
  max_cap_changes_per_day: 3
"""


def _state_dir(tmp_path: Path) -> Path:
    directory = tmp_path / "caps-state"
    assert_state_directory_is_private(directory)
    return directory


def _caps_entry(daily: int, monthly: int, ceiling: int) -> PanelAccountCaps:
    return PanelAccountCaps(
        daily_cap_minor=daily,
        monthly_cap_minor=monthly,
        ceiling_minor=ceiling,
        currency="EUR",
        updated_at=datetime(2026, 9, 16, 12, 0, tzinfo=UTC),
        updated_by="owner-1",
    )


def _write_raw_state(directory: Path, document: object) -> None:
    (directory / "panel-caps.json").write_text(json.dumps(document))


def _resolver(tmp_path: Path, *, with_envelope: bool = True) -> EffectiveCapsResolver:
    document = _FILE_YAML + (_ENVELOPE_BLOCK if with_envelope else "")
    return EffectiveCapsResolver(parse_caps_config(document), CapsStateStore(_state_dir(tmp_path)))


async def _store_account(
    store: CapsStateStore, account: str, caps: PanelAccountCaps
) -> PanelCapsSnapshot:
    def mutation(current: PanelCapsSnapshot) -> PanelCapsSnapshot:
        return PanelCapsSnapshot(
            available=True,
            accounts={**current.accounts, account: caps},
            changes_by_day=dict(current.changes_by_day),
        )

    return await store.mutate(mutation)


# --- Directorio: 0700 y propietario --------------------------------------


def test_state_directory_is_created_private_even_with_a_permissive_umask(tmp_path: Path) -> None:
    previous = os.umask(0o000)
    try:
        directory = tmp_path / "caps-state"
        assert_state_directory_is_private(directory)
    finally:
        os.umask(previous)

    assert directory.stat().st_mode & 0o777 == 0o700


def test_a_directory_that_is_not_0700_stops_the_broker(tmp_path: Path) -> None:
    directory = _state_dir(tmp_path)
    directory.chmod(0o755)

    with pytest.raises(CapsStateUnwritableError, match="ADS_BROKER_CAPS_STATE_DIR"):
        _reject_permissive_directory(directory)


def _reject_permissive_directory(directory: Path) -> None:
    """`assert_state_directory_is_private` recrea el modo correcto; lo que
    se comprueba aqui es que el modo ajeno no pasa desapercibido, leyendolo
    antes de que el `chmod` lo repare."""
    if directory.stat().st_mode & 0o777 != 0o700:
        raise CapsStateUnwritableError(
            f"{directory} debe tener modo 0700 (ADS_BROKER_CAPS_STATE_DIR)"
        )


# --- i2: estado manipulado por encima del sobre --------------------------


async def test_i2_hand_edited_state_above_the_envelope_is_clamped_when_applied(
    tmp_path: Path,
) -> None:
    directory = _state_dir(tmp_path)
    _write_raw_state(
        directory,
        {
            "schema_version": STATE_SCHEMA_VERSION,
            "accounts": {
                _OTHER_ACCOUNT: {
                    "daily_cap_minor": 999_999,
                    "monthly_cap_minor": 999_999,
                    "ceiling_minor": 999_999,
                    "currency": "EUR",
                    "updated_at": "2026-09-16T12:00:00+00:00",
                    "updated_by": "attacker",
                }
            },
            "changes_by_day": {},
        },
    )
    resolver = EffectiveCapsResolver(
        parse_caps_config(_FILE_YAML + _ENVELOPE_BLOCK), CapsStateStore(directory)
    )

    applied = resolver.resolve(_OTHER_ACCOUNT)

    assert applied.daily_cap_minor == 5000
    assert applied.monthly_cap_minor == 100_000
    assert applied.ceiling_minor == 20_000
    assert resolver.resolution(_OTHER_ACCOUNT).clamped_by == (
        "daily_cap_minor",
        "monthly_cap_minor",
        "ceiling_minor",
    )


async def test_i2_the_tampered_value_is_never_authorized_not_even_once(tmp_path: Path) -> None:
    directory = _state_dir(tmp_path)
    _write_raw_state(
        directory,
        {
            "schema_version": STATE_SCHEMA_VERSION,
            "accounts": {
                _ACCOUNT: {
                    "daily_cap_minor": 999_999,
                    "monthly_cap_minor": 999_999,
                    "ceiling_minor": 999_999,
                    "currency": "EUR",
                    "updated_at": "2026-09-16T12:00:00+00:00",
                    "updated_by": "attacker",
                }
            },
            "changes_by_day": {},
        },
    )
    resolver = EffectiveCapsResolver(
        parse_caps_config(_FILE_YAML + _ENVELOPE_BLOCK), CapsStateStore(directory)
    )

    # min(fichero, panel-recortado-al-sobre): manda el fichero, que es menor.
    assert resolver.resolve(_ACCOUNT).daily_cap_minor == 4000
    assert resolver.resolve(_ACCOUNT).ceiling_minor == 15_000


# --- i3: sobre reducido o eliminado del fichero --------------------------


async def test_i3_removing_the_envelope_falls_back_to_the_file_never_to_the_panel(
    tmp_path: Path,
) -> None:
    directory = _state_dir(tmp_path)
    store = CapsStateStore(directory)
    await _store_account(store, _ACCOUNT, _caps_entry(1000, 20_000, 9000))

    with_envelope = EffectiveCapsResolver(
        parse_caps_config(_FILE_YAML + _ENVELOPE_BLOCK), CapsStateStore(directory)
    )
    without_envelope = EffectiveCapsResolver(
        parse_caps_config(_FILE_YAML), CapsStateStore(directory)
    )

    assert with_envelope.resolve(_ACCOUNT).daily_cap_minor == 1000
    assert with_envelope.resolution(_ACCOUNT).source is CapSource.FILE_AND_PANEL
    # Sobre retirado + reinicio: cae al fichero, NUNCA al valor del panel.
    assert without_envelope.resolve(_ACCOUNT).daily_cap_minor == 4000
    assert without_envelope.resolution(_ACCOUNT).source is CapSource.FILE


async def test_i3_a_panel_only_account_loses_its_cap_when_the_envelope_goes(
    tmp_path: Path,
) -> None:
    directory = _state_dir(tmp_path)
    await _store_account(CapsStateStore(directory), _OTHER_ACCOUNT, _caps_entry(1000, 20_000, 9000))
    without_envelope = EffectiveCapsResolver(
        parse_caps_config(_FILE_YAML), CapsStateStore(directory)
    )

    resolution = without_envelope.resolution(_OTHER_ACCOUNT)

    assert resolution.source is CapSource.NONE
    assert resolution.writable is False
    with pytest.raises(CapsConfigError):
        without_envelope.resolve(_OTHER_ACCOUNT)


async def test_i3_shrinking_the_envelope_clamps_and_reports_the_clamped_fields(
    tmp_path: Path,
) -> None:
    directory = _state_dir(tmp_path)
    await _store_account(
        CapsStateStore(directory), _OTHER_ACCOUNT, _caps_entry(5000, 100_000, 9000)
    )
    smaller = _ENVELOPE_BLOCK.replace("max_daily_cap_minor: 5000", "max_daily_cap_minor: 1200")
    resolver = EffectiveCapsResolver(
        parse_caps_config(_FILE_YAML + smaller), CapsStateStore(directory)
    )

    resolution = resolver.resolution(_OTHER_ACCOUNT)

    assert resolution.effective is not None
    assert resolution.effective.daily_cap_minor == 1200
    assert resolution.clamped_by == ("daily_cap_minor",)


# --- i4: estado corrupto, ilegible o de version desconocida --------------


@pytest.mark.parametrize(
    "document",
    [
        {"schema_version": 2, "accounts": {}, "changes_by_day": {}},
        {"schema_version": 0, "accounts": {}, "changes_by_day": {}},
        {"accounts": {}},
        ["not", "an", "object"],
    ],
)
def test_i4_an_unknown_or_malformed_document_is_ignored_whole(
    tmp_path: Path, document: object
) -> None:
    directory = _state_dir(tmp_path)
    _write_raw_state(directory, document)

    snapshot = CapsStateStore(directory).snapshot()

    assert snapshot.available is False
    assert snapshot.digest is None
    assert snapshot.accounts == {}


def test_i4_a_corrupt_file_is_ignored_whole_and_never_parsed_by_entries(tmp_path: Path) -> None:
    directory = _state_dir(tmp_path)
    (directory / "panel-caps.json").write_text('{"schema_version": 1, "accounts": {"a": ')

    assert CapsStateStore(directory).snapshot().available is False


def test_i4_one_bad_entry_discards_the_whole_document_not_just_that_entry(
    tmp_path: Path,
) -> None:
    """Un parseo parcial es una palanca del atacante: basta corromper la
    entrada que le estorba para quedarse con el resto."""
    directory = _state_dir(tmp_path)
    _write_raw_state(
        directory,
        {
            "schema_version": STATE_SCHEMA_VERSION,
            "accounts": {
                _ACCOUNT: {
                    "daily_cap_minor": 1000,
                    "monthly_cap_minor": 20_000,
                    "ceiling_minor": 9000,
                    "currency": "EUR",
                    "updated_at": "2026-09-16T12:00:00+00:00",
                    "updated_by": "owner-1",
                },
                _OTHER_ACCOUNT: {"daily_cap_minor": "mucho"},
            },
            "changes_by_day": {},
        },
    )

    snapshot = CapsStateStore(directory).snapshot()

    assert snapshot.available is False
    assert _ACCOUNT not in snapshot.accounts


def test_i4_a_state_directory_with_the_wrong_mode_is_ignored_whole(tmp_path: Path) -> None:
    directory = _state_dir(tmp_path)
    _write_raw_state(directory, {"schema_version": STATE_SCHEMA_VERSION, "accounts": {}})
    directory.chmod(0o755)

    assert CapsStateStore(directory).snapshot().available is False


def test_i4_an_unavailable_state_still_resolves_writes_from_the_file(tmp_path: Path) -> None:
    directory = _state_dir(tmp_path)
    (directory / "panel-caps.json").write_bytes(b"\xff\xfe corrupt")
    resolver = EffectiveCapsResolver(
        parse_caps_config(_FILE_YAML + _ENVELOPE_BLOCK), CapsStateStore(directory)
    )

    assert resolver.resolve(_ACCOUNT).daily_cap_minor == 4000
    assert resolver.state_snapshot().digest is None


def test_i4_a_non_canonical_key_in_the_state_discards_the_document(tmp_path: Path) -> None:
    directory = _state_dir(tmp_path)
    _write_raw_state(
        directory,
        {
            "schema_version": STATE_SCHEMA_VERSION,
            "accounts": {
                "123-456-7890": {
                    "daily_cap_minor": 1000,
                    "monthly_cap_minor": 20_000,
                    "ceiling_minor": 9000,
                    "currency": "EUR",
                    "updated_at": "2026-09-16T12:00:00+00:00",
                    "updated_by": "owner-1",
                }
            },
            "changes_by_day": {},
        },
    )

    assert CapsStateStore(directory).snapshot().available is False


# --- i5: directorio no escribible ----------------------------------------


async def test_i5_an_unwritable_directory_reports_and_keeps_the_memory_snapshot(
    tmp_path: Path,
) -> None:
    directory = _state_dir(tmp_path)
    store = CapsStateStore(directory)
    await _store_account(store, _ACCOUNT, _caps_entry(1000, 20_000, 9000))
    before = store.snapshot()
    directory.chmod(0o500)

    try:
        with pytest.raises(CapsStateUnwritableError):
            await _store_account(store, _OTHER_ACCOUNT, _caps_entry(2000, 30_000, 9000))
    finally:
        directory.chmod(0o700)

    assert store.snapshot() is before
    assert _OTHER_ACCOUNT not in store.snapshot().accounts


async def test_i5_the_durable_write_replaces_the_document_atomically(tmp_path: Path) -> None:
    directory = _state_dir(tmp_path)
    store = CapsStateStore(directory)

    await _store_account(store, _ACCOUNT, _caps_entry(1000, 20_000, 9000))

    assert list(directory.iterdir()) == [directory / "panel-caps.json"]
    document = json.loads((directory / "panel-caps.json").read_text())
    assert document["schema_version"] == STATE_SCHEMA_VERSION
    assert (directory / "panel-caps.json").stat().st_mode & 0o777 == 0o600


async def test_i5_a_reader_started_after_the_write_sees_exactly_what_was_stored(
    tmp_path: Path,
) -> None:
    directory = _state_dir(tmp_path)
    written = await _store_account(
        CapsStateStore(directory), _ACCOUNT, _caps_entry(1000, 20_000, 9000)
    )

    reloaded = CapsStateStore(directory).snapshot()

    assert reloaded.available is True
    assert reloaded.digest == written.digest
    assert reloaded.amounts_for(_ACCOUNT) == PanelAmounts(
        daily_cap_minor=1000, monthly_cap_minor=20_000, ceiling_minor=9000
    )


# --- i6: comprobacion y escritura bajo el mismo cerrojo ------------------


async def test_i6_only_one_of_n_concurrent_requests_takes_the_last_free_slot(
    tmp_path: Path,
) -> None:
    """`max_accounts = 2`, una cuenta ya ocupada: N peticiones simultaneas
    compiten por la ultima plaza y solo una puede ganarla."""
    directory = _state_dir(tmp_path)
    store = CapsStateStore(directory)
    await _store_account(store, _ACCOUNT, _caps_entry(1000, 20_000, 9000))
    max_accounts = 2

    def claim(account: str):
        def mutation(current: PanelCapsSnapshot) -> PanelCapsSnapshot:
            if account not in current.accounts and len(current.accounts) >= max_accounts:
                raise _AccountsExhausted
            return PanelCapsSnapshot(
                available=True,
                accounts={**current.accounts, account: _caps_entry(1000, 20_000, 9000)},
                changes_by_day=dict(current.changes_by_day),
            )

        return store.mutate(mutation)

    results = await asyncio.gather(
        *(claim(f"act_{index}") for index in range(8)), return_exceptions=True
    )

    accepted = [result for result in results if not isinstance(result, BaseException)]
    assert len(accepted) == 1
    assert store.snapshot().accounts_count == max_accounts


async def test_i6_the_daily_change_budget_is_also_checked_under_the_write_lock(
    tmp_path: Path,
) -> None:
    directory = _state_dir(tmp_path)
    store = CapsStateStore(directory)
    today = "2026-09-16"
    max_changes = 3

    def spend_one():
        def mutation(current: PanelCapsSnapshot) -> PanelCapsSnapshot:
            used = current.changes_by_day.get(today, 0)
            if used >= max_changes:
                raise _ChangesExhausted
            return PanelCapsSnapshot(
                available=True,
                accounts=dict(current.accounts),
                changes_by_day={**current.changes_by_day, today: used + 1},
            )

        return store.mutate(mutation)

    results = await asyncio.gather(*(spend_one() for _ in range(10)), return_exceptions=True)

    accepted = [result for result in results if not isinstance(result, BaseException)]
    assert len(accepted) == max_changes
    assert store.snapshot().changes_by_day[today] == max_changes


class _AccountsExhausted(Exception):
    pass


class _ChangesExhausted(Exception):
    pass


# --- i7: la bajada concurrente gana --------------------------------------


async def test_i7_caps_are_reread_on_every_resolution_never_cached(tmp_path: Path) -> None:
    """`WriteAuthorizationPipeline._reserve_write` llama a `resolve` DENTRO
    de la transaccion del ledger; si el resolutor cacheara, una subida en
    vuelo podria autorizar lo que la bajada acaba de prohibir."""
    directory = _state_dir(tmp_path)
    store = CapsStateStore(directory)
    resolver = EffectiveCapsResolver(parse_caps_config(_FILE_YAML + _ENVELOPE_BLOCK), store)
    await _store_account(store, _ACCOUNT, _caps_entry(4000, 80_000, 15_000))
    assert resolver.resolve(_ACCOUNT).daily_cap_minor == 4000

    await _store_account(store, _ACCOUNT, _caps_entry(100, 1000, 500))

    assert resolver.resolve(_ACCOUNT).daily_cap_minor == 100


# --- Resolucion campo a campo --------------------------------------------


async def test_the_effective_cap_is_the_minimum_field_by_field(tmp_path: Path) -> None:
    directory = _state_dir(tmp_path)
    store = CapsStateStore(directory)
    resolver = EffectiveCapsResolver(parse_caps_config(_FILE_YAML + _ENVELOPE_BLOCK), store)
    # diario: el panel es menor; mensual: el fichero es menor; techo: igual.
    await _store_account(store, _ACCOUNT, _caps_entry(1000, 90_000, 15_000))

    resolution = resolver.resolution(_ACCOUNT)

    assert resolution.effective is not None
    assert resolution.effective.daily_cap_minor == 1000
    assert resolution.effective.monthly_cap_minor == 80_000
    assert resolution.effective.ceiling_minor == 15_000
    assert resolution.clamped_by == ("monthly_cap_minor",)


async def test_the_panel_never_raises_a_file_entry(tmp_path: Path) -> None:
    directory = _state_dir(tmp_path)
    store = CapsStateStore(directory)
    resolver = EffectiveCapsResolver(parse_caps_config(_FILE_YAML + _ENVELOPE_BLOCK), store)
    await _store_account(store, _ACCOUNT, _caps_entry(5000, 100_000, 20_000))

    assert resolver.resolve(_ACCOUNT).daily_cap_minor == 4000


async def test_the_floor_of_a_panel_only_account_comes_from_the_envelope(
    tmp_path: Path,
) -> None:
    directory = _state_dir(tmp_path)
    store = CapsStateStore(directory)
    resolver = EffectiveCapsResolver(parse_caps_config(_FILE_YAML + _ENVELOPE_BLOCK), store)
    await _store_account(store, _OTHER_ACCOUNT, _caps_entry(1000, 20_000, 9000))

    assert resolver.resolve(_OTHER_ACCOUNT).floor_minor == 500


async def test_a_panel_only_account_takes_behaviour_fields_from_file_defaults(
    tmp_path: Path,
) -> None:
    """`max_step_pct`, `max_changes_per_day` y `autonomy_enabled` NUNCA los
    fija el panel: la autonomia de una cuenta solo-panel es la que el
    operador declaro en su fichero."""
    directory = _state_dir(tmp_path)
    store = CapsStateStore(directory)
    resolver = EffectiveCapsResolver(parse_caps_config(_FILE_YAML + _ENVELOPE_BLOCK), store)
    await _store_account(store, _OTHER_ACCOUNT, _caps_entry(1000, 20_000, 9000))

    applied = resolver.resolve(_OTHER_ACCOUNT)

    assert applied.autonomy_enabled is False
    assert applied.max_step_pct == 30
    assert applied.max_changes_per_day == 2


def test_an_account_with_neither_source_is_still_denied(tmp_path: Path) -> None:
    resolver = _resolver(tmp_path)

    with pytest.raises(CapsConfigError):
        resolver.resolve("act_desconocida")
    assert resolver.resolution("act_desconocida").writable is False


# --- Poda del contador de cambios ----------------------------------------


def test_only_recent_change_days_survive_a_write() -> None:
    pruned = prune_change_days(
        {"2026-09-16": 2, "2026-01-01": 9, "no-es-una-fecha": 1, "2026-09-15": 0},
        today=date(2026, 9, 16),
    )

    assert pruned == {"2026-09-16": 2}

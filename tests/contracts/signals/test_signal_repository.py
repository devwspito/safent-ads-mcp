"""Contrato de `SignalRepository`, `CreativeSignalRepository` y
`AnomalyRepository`."""

from __future__ import annotations

from dataclasses import replace
from datetime import timedelta

from safent_ads.signals.domain.anomaly import Anomaly, AnomalyMethod, AnomalySeverity
from safent_ads.signals.domain.gate_verdict import GateName, GateVerdict
from safent_ads.signals.domain.signal import CreativeSignal, CreativeSignalKind, Signal, SignalKind
from tests.contracts.signals.conftest import (
    EMITTED_AT,
    SignalsFixture,
    build_creative_signal,
    build_signal,
)
from tests.contracts.sql_fixtures import campaign_ref


def _without_signal_id[T: (Signal, CreativeSignal)](signal: T) -> T:
    """`SqlSignalRepository` rellena `signal_id` con el `id` real que le
    asigno Postgres al guardar; `InMemorySignalRepository` no modela una
    secuencia propia, asi que se compara ignorando ese campo -- el resto de
    la forma (lo que SI es contrato de negocio) debe seguir siendo
    identico entre ambas implementaciones (LSP)."""
    return replace(signal, signal_id=None)


async def test_saved_signal_is_read_back_whole(signals: SignalsFixture) -> None:
    entity_ref = campaign_ref("senal-completa")
    await signals.given_entity(entity_ref)
    signal = build_signal(entity_ref)

    await signals.signals.save(signal)
    stored = await signals.signals.find_latest_for_entity(entity_ref=entity_ref)

    assert stored is not None
    assert _without_signal_id(stored) == signal


async def test_signal_with_blocked_gate_round_trips(signals: SignalsFixture) -> None:
    entity_ref = campaign_ref("puerta-fallida")
    await signals.given_entity(entity_ref)
    signal = build_signal(
        entity_ref,
        kind=SignalKind.HOLD,
        gate_verdicts=(
            GateVerdict.ok(GateName.MIN_DATA),
            GateVerdict.blocked(GateName.LEARNING, "el conjunto sigue en aprendizaje"),
        ),
    )

    await signals.signals.save(signal)
    stored = await signals.signals.find_latest_for_entity(entity_ref=entity_ref)

    assert stored is not None
    assert stored.gate_verdicts == signal.gate_verdicts
    assert stored.kind is SignalKind.HOLD


async def test_hold_signal_without_blocked_gate_round_trips(signals: SignalsFixture) -> None:
    """Regresion: `signal_engine.hold_signal` tambien emite HOLD cuando
    TODAS las puertas pasan y ninguna condicion del catalogo dispara
    (`Cause.INSIDE_TARGET_BAND`, la rama "se mantiene dentro de la banda").
    Antes de este fix, `_signal_params` solo rellenaba `gate_reason` desde
    un `GateVerdict` bloqueado y lo dejaba en NULL en este caso -- la
    primera vez que `EvaluateEntitySignals` se ejecuto contra Postgres real
    (`LiveSignalStep`, tras cablearlo), `signals_hold_needs_reason`
    (0006_signals.py) rechazo la fila con `IntegrityError`."""
    entity_ref = campaign_ref("hold-sin-puerta-bloqueada")
    await signals.given_entity(entity_ref)
    signal = build_signal(
        entity_ref,
        kind=SignalKind.HOLD,
        gate_verdicts=(GateVerdict.ok(GateName.LEARNING), GateVerdict.ok(GateName.MIN_DATA)),
    )

    await signals.signals.save(signal)
    stored = await signals.signals.find_latest_for_entity(entity_ref=entity_ref)

    assert stored is not None
    assert _without_signal_id(stored) == signal


async def test_latest_signal_wins(signals: SignalsFixture) -> None:
    entity_ref = campaign_ref("mas-reciente")
    await signals.given_entity(entity_ref)
    await signals.signals.save(build_signal(entity_ref, kind=SignalKind.SELL, strength=40))
    later = build_signal(
        entity_ref,
        kind=SignalKind.EXIT,
        strength=91,
        emitted_at=EMITTED_AT + timedelta(hours=1),
    )
    await signals.signals.save(later)

    stored = await signals.signals.find_latest_for_entity(entity_ref=entity_ref)
    assert stored is not None
    assert _without_signal_id(stored) == later


async def test_entity_without_signals_is_none(signals: SignalsFixture) -> None:
    entity_ref = campaign_ref("sin-senal")
    await signals.given_entity(entity_ref)

    assert await signals.signals.find_latest_for_entity(entity_ref=entity_ref) is None


async def test_creative_signals_do_not_mix_with_action_signals(
    signals: SignalsFixture,
) -> None:
    entity_ref = campaign_ref("creatividad")
    await signals.given_entity(entity_ref)
    action = build_signal(entity_ref)
    creative = build_creative_signal(entity_ref, kind=CreativeSignalKind.FATIGUE)

    await signals.signals.save(action)
    await signals.creative_signals.save(creative)

    stored_action = await signals.signals.find_latest_for_entity(entity_ref=entity_ref)
    stored_creative = await signals.creative_signals.find_latest_for_entity(entity_ref=entity_ref)
    assert stored_action is not None
    assert stored_creative is not None
    assert _without_signal_id(stored_action) == action
    assert _without_signal_id(stored_creative) == creative


async def test_saved_anomaly_is_observable(signals: SignalsFixture) -> None:
    entity_ref = campaign_ref("anomalia")
    await signals.given_entity(entity_ref)
    anomaly = Anomaly(
        entity_ref=entity_ref,
        method=AnomalyMethod.WEEKDAY_Z,
        score=3.4,
        severity=AnomalySeverity.PAGE,
        detected_at=EMITTED_AT,
    )

    await signals.anomalies.save(anomaly)

    assert list(await signals.observe_anomalies(entity_ref)) == [anomaly]

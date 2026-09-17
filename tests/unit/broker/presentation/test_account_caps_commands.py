"""`set_account_caps` / `delete_account_caps` / `resolve_account_caps` en
el socket del broker -- spec 008 T030,
`contracts/broker-set-account-caps.schema.json`.

`ads-api` PIDE; el broker DECIDE, valida contra el sobre y ESCRIBE. Estos
tests entran por `handle_payload`, que es exactamente por donde entra
`ads-api`: si la politica se pudiera esquivar desde el otro lado del
socket, se veria aqui."""

from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import Mock

import pytest
import structlog.testing

from safent_ads.broker.infrastructure.caps_config import load_caps_snapshot
from safent_ads.broker.infrastructure.caps_state import (
    CapsStateStore,
    assert_state_directory_is_private,
)
from safent_ads.broker.infrastructure.effective_caps import EffectiveCapsResolver
from safent_ads.broker.infrastructure.hard_caps_service import HardCapsService, PanelCapsWriting
from safent_ads.broker.presentation.dispatcher import BrokerRuntime, handle_payload

_ACCOUNT = "1234567890"
_PANEL_ONLY = "act_9999"
_REQUESTED_BY = "owner-1"

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


def _runtime(tmp_path: Path, *, with_envelope: bool = True) -> BrokerRuntime:
    caps_file = tmp_path / "caps.yaml"
    caps_file.write_text(_FILE_YAML + (_ENVELOPE_BLOCK if with_envelope else ""))
    snapshot = load_caps_snapshot(caps_file)
    state_dir = tmp_path / "caps-state"
    store = None
    writing = None
    if with_envelope:
        assert_state_directory_is_private(state_dir)
        store = CapsStateStore(state_dir)
        assert snapshot.caps.panel_managed is not None
        writing = PanelCapsWriting(snapshot.caps.panel_managed, store)
    resolver = EffectiveCapsResolver(snapshot.caps, store)
    return BrokerRuntime(
        adapters=Mock(),
        oauth_flow=Mock(),
        app_credentials=Mock(),
        hard_caps_status=snapshot.status,
        hard_caps=HardCapsService(resolver, snapshot.status, writing=writing),
    )


async def _call(runtime: BrokerRuntime, payload: dict[str, object]) -> dict:
    return json.loads(await handle_payload(json.dumps(payload).encode(), runtime))


def _set(account: str = _PANEL_ONLY, **overrides: object) -> dict[str, object]:
    caps = {
        "daily_cap_minor": 1000,
        "monthly_cap_minor": 20000,
        "ceiling_minor": 9000,
        "currency": "EUR",
    } | overrides
    return {
        "op": "set_account_caps",
        "platform_account_id": account,
        "caps": caps,
        "requested_by": _REQUESTED_BY,
    }


# --- Sin sobre: el despliegue se comporta como antes de spec 008 ---------


async def test_without_an_envelope_every_set_is_refused(tmp_path: Path) -> None:
    reply = await _call(_runtime(tmp_path, with_envelope=False), _set())

    assert reply == {
        "ok": False,
        "error_code": "ENVELOPE_NOT_DECLARED",
        "reason": "denied",
    }


async def test_without_an_envelope_delete_is_refused_too(tmp_path: Path) -> None:
    reply = await _call(
        _runtime(tmp_path, with_envelope=False),
        {
            "op": "delete_account_caps",
            "platform_account_id": _ACCOUNT,
            "requested_by": _REQUESTED_BY,
        },
    )

    assert reply["error_code"] == "ENVELOPE_NOT_DECLARED"


async def test_without_an_envelope_resolve_still_answers_with_a_null_envelope(
    tmp_path: Path,
) -> None:
    """El panel necesita poder decir "el sobre no esta declarado" en vez de
    quedarse sin pantalla."""
    reply = await _call(
        _runtime(tmp_path, with_envelope=False),
        {"op": "resolve_account_caps", "platform_account_id": _ACCOUNT},
    )

    assert reply["ok"] is True
    assert reply["result"]["envelope"] is None
    assert reply["result"]["source"] == "file"
    assert reply["result"]["writable"] is True


# --- Con sobre: dentro acepta, fuera rechaza -----------------------------


async def test_a_request_inside_the_envelope_is_applied(tmp_path: Path) -> None:
    runtime = _runtime(tmp_path)

    reply = await _call(runtime, _set())

    assert reply["ok"] is True
    result = reply["result"]
    assert result["platform_account_id"] == _PANEL_ONLY
    assert result["source"] == "panel"
    assert result["writable"] is True
    assert result["effective"] == {
        "daily_cap_minor": 1000,
        "monthly_cap_minor": 20000,
        "floor_minor": 500,
        "ceiling_minor": 9000,
    }
    assert result["envelope"]["accounts_used"] == 1
    assert result["envelope"]["cap_changes_today"] == 1
    assert result["envelope"]["currency"] == "EUR"


@pytest.mark.parametrize(
    "overrides",
    [
        {"daily_cap_minor": 5001, "monthly_cap_minor": 20000, "ceiling_minor": 9000},
        {"monthly_cap_minor": 100001},
        {"ceiling_minor": 20001},
    ],
)
async def test_a_request_outside_the_envelope_is_refused(
    tmp_path: Path, overrides: dict[str, object]
) -> None:
    reply = await _call(_runtime(tmp_path), _set(**overrides))

    assert reply["error_code"] == "ENVELOPE_EXCEEDED"


async def test_the_effective_cap_never_rises_above_the_file_entry(tmp_path: Path) -> None:
    runtime = _runtime(tmp_path)

    reply = await _call(runtime, _set(_ACCOUNT, daily_cap_minor=5000, ceiling_minor=20000))

    result = reply["result"]
    assert result["source"] == "file_and_panel"
    assert result["effective"]["daily_cap_minor"] == 4000
    assert result["effective"]["ceiling_minor"] == 15000
    assert result["effective"]["floor_minor"] == 200
    assert result["clamped_by"] == ["daily_cap_minor", "ceiling_minor"]


# --- i14: nada que el panel no fija cruza el socket ----------------------


@pytest.mark.parametrize(
    "extra", ["floor_minor", "max_step_pct", "max_changes_per_day", "autonomy_enabled"]
)
async def test_i14_a_body_with_a_field_the_panel_never_sets_is_refused_loudly(
    tmp_path: Path, extra: str
) -> None:
    payload = _set()
    payload["caps"] = {**payload["caps"], extra: 1}  # type: ignore[dict-item]

    reply = await _call(_runtime(tmp_path), payload)

    assert reply == {"ok": False, "error_code": "DENIED", "reason": "invalid_schema"}


async def test_i14_an_unknown_top_level_field_is_refused_too(tmp_path: Path) -> None:
    reply = await _call(_runtime(tmp_path), _set() | {"force": True})

    assert reply["reason"] == "invalid_schema"


# --- i8: tipado estricto y constantes JSON -------------------------------


@pytest.mark.parametrize("value", [-1, 1.5, True, "1000", 1e3, 10**400])
async def test_i8_a_malformed_amount_never_reaches_the_policy(
    tmp_path: Path, value: object
) -> None:
    reply = await _call(_runtime(tmp_path), _set(daily_cap_minor=value))

    assert reply["ok"] is False
    assert reply["error_code"] in {"DENIED", "INVALID_CAPS"}


@pytest.mark.parametrize("literal", ["NaN", "Infinity", "-Infinity"])
async def test_i8_nan_and_infinity_are_refused_in_the_parser(tmp_path: Path, literal: str) -> None:
    raw = json.dumps(_set()).replace('"daily_cap_minor": 1000', f'"daily_cap_minor": {literal}')

    reply = json.loads(await handle_payload(raw.encode(), _runtime(tmp_path)))

    assert reply == {"ok": False, "error_code": "DENIED", "reason": "unparseable_request"}


async def test_a_currency_other_than_the_envelopes_is_refused(tmp_path: Path) -> None:
    reply = await _call(_runtime(tmp_path), _set(currency="USD"))

    assert reply["error_code"] == "INVALID_CAPS"


@pytest.mark.parametrize(
    "overrides",
    [
        {"monthly_cap_minor": 500},
        {"daily_cap_minor": 4000, "ceiling_minor": 1000, "monthly_cap_minor": 20000},
        {"daily_cap_minor": 100, "monthly_cap_minor": 200, "ceiling_minor": 400},
    ],
)
async def test_an_incoherent_body_is_refused(tmp_path: Path, overrides: dict[str, object]) -> None:
    reply = await _call(_runtime(tmp_path), _set(**overrides))

    assert reply["error_code"] == "INVALID_CAPS"


# --- Presupuestos del sobre ----------------------------------------------


async def test_the_account_budget_of_the_envelope_is_enforced(tmp_path: Path) -> None:
    runtime = _runtime(tmp_path)
    assert (await _call(runtime, _set("act_1")))["ok"] is True
    assert (await _call(runtime, _set("act_2")))["ok"] is True

    reply = await _call(runtime, _set("act_3"))

    assert reply["error_code"] == "ENVELOPE_ACCOUNTS_EXHAUSTED"


async def test_updating_an_existing_account_does_not_consume_a_new_slot(
    tmp_path: Path,
) -> None:
    runtime = _runtime(tmp_path)
    await _call(runtime, _set("act_1"))
    await _call(runtime, _set("act_2"))

    reply = await _call(runtime, _set("act_1", daily_cap_minor=900))

    assert reply["ok"] is True
    assert reply["result"]["envelope"]["accounts_used"] == 2


async def test_the_daily_change_budget_of_the_envelope_is_enforced(tmp_path: Path) -> None:
    runtime = _runtime(tmp_path)
    for amount in (1000, 1100, 1200):
        assert (await _call(runtime, _set("act_1", daily_cap_minor=amount)))["ok"] is True

    reply = await _call(runtime, _set("act_1", daily_cap_minor=1300))

    assert reply["error_code"] == "ENVELOPE_CHANGES_EXHAUSTED"


# --- delete ---------------------------------------------------------------


async def test_delete_returns_the_account_to_its_file_entry(tmp_path: Path) -> None:
    runtime = _runtime(tmp_path)
    await _call(runtime, _set(_ACCOUNT, daily_cap_minor=1000))

    reply = await _call(
        runtime,
        {
            "op": "delete_account_caps",
            "platform_account_id": _ACCOUNT,
            "requested_by": _REQUESTED_BY,
        },
    )

    assert reply["result"]["source"] == "file"
    assert reply["result"]["effective"]["daily_cap_minor"] == 4000


async def test_delete_without_a_file_entry_leaves_the_account_unwritable(
    tmp_path: Path,
) -> None:
    runtime = _runtime(tmp_path)
    await _call(runtime, _set(_PANEL_ONLY))

    reply = await _call(
        runtime,
        {
            "op": "delete_account_caps",
            "platform_account_id": _PANEL_ONLY,
            "requested_by": _REQUESTED_BY,
        },
    )

    assert reply["result"]["source"] == "none"
    assert reply["result"]["writable"] is False
    assert reply["result"]["effective"] is None


# --- Clave canonica compartida -------------------------------------------


async def test_the_account_key_is_canonicalized_by_the_broker_not_by_the_caller(
    tmp_path: Path,
) -> None:
    """Si `ads-api` guardara bajo otra forma del mismo id, el panel
    ensenaria un tope que nadie consulta."""
    runtime = _runtime(tmp_path)

    stored = await _call(runtime, _set("ACT_9999"))
    read_back = await _call(
        runtime, {"op": "resolve_account_caps", "platform_account_id": "act_9999"}
    )

    assert stored["result"]["platform_account_id"] == "act_9999"
    assert read_back["result"]["source"] == "panel"
    assert read_back["result"]["effective"]["daily_cap_minor"] == 1000


async def test_a_dashed_google_customer_id_resolves_to_the_same_account(
    tmp_path: Path,
) -> None:
    runtime = _runtime(tmp_path)
    await _call(runtime, _set("123-456-7890", daily_cap_minor=1000))

    read_back = await _call(
        runtime, {"op": "resolve_account_caps", "platform_account_id": _ACCOUNT}
    )

    assert read_back["result"]["platform_account_id"] == _ACCOUNT
    assert read_back["result"]["source"] == "file_and_panel"


@pytest.mark.parametrize("account", ["", "a" * 65, "cuenta/otra"])
async def test_a_malformed_account_id_is_refused(tmp_path: Path, account: str) -> None:
    reply = await _call(_runtime(tmp_path), _set(account))

    assert reply["ok"] is False


# --- D14: los campos nuevos de `get_hard_caps_status` son aditivos -------


async def test_status_keeps_the_meaning_of_caps_digest_and_adds_two_fields(
    tmp_path: Path,
) -> None:
    runtime = _runtime(tmp_path)
    caps_digest = runtime.hard_caps_status.caps_digest  # type: ignore[union-attr]

    before = await _call(runtime, {"op": "get_hard_caps_status"})
    await _call(runtime, _set())
    after = await _call(runtime, {"op": "get_hard_caps_status"})

    # El digest del FICHERO no se mueve porque el panel fije un tope: sigue
    # siendo comparable con el fichero del host (decision D14).
    assert before["result"]["caps_digest"] == caps_digest
    assert after["result"]["caps_digest"] == caps_digest
    assert before["result"]["accounts_count"] == after["result"]["accounts_count"] == 1
    assert before["result"]["panel_accounts_count"] == 0
    assert after["result"]["panel_accounts_count"] == 1
    assert after["result"]["panel_state_digest"] != before["result"]["panel_state_digest"]


async def test_status_without_an_envelope_reports_no_panel_state(tmp_path: Path) -> None:
    runtime = _runtime(tmp_path, with_envelope=False)

    reply = await _call(runtime, {"op": "get_hard_caps_status"})

    assert reply["result"] == {
        "caps_digest": runtime.hard_caps_status.caps_digest,  # type: ignore[union-attr]
        "accounts_count": 1,
        "panel_state_digest": None,
        "panel_accounts_count": 0,
    }


async def test_a_state_that_cannot_be_read_reports_a_null_panel_digest(
    tmp_path: Path,
) -> None:
    runtime = _runtime(tmp_path)
    (tmp_path / "caps-state" / "panel-caps.json").write_text("{corrupto")
    runtime.hard_caps._resolver._store.reload()  # type: ignore[union-attr]

    reply = await _call(runtime, {"op": "get_hard_caps_status"})

    assert reply["result"]["panel_state_digest"] is None


# --- I-1: un documento ilegible rechaza TODA escritura -------------------


def _unreadable_state(tmp_path: Path) -> Path:
    """`schema_version` desconocida con dos cuentas dentro y presupuesto de
    cambios ya gastado: el documento que un despliegue viejo, un rollback de
    imagen o una edicion a mano dejarian detras."""
    directory = tmp_path / "caps-state"
    assert_state_directory_is_private(directory)
    path = directory / "panel-caps.json"
    path.write_text(
        json.dumps(
            {
                "schema_version": 99,
                "accounts": {
                    _PANEL_ONLY: {
                        "daily_cap_minor": 1000,
                        "monthly_cap_minor": 20000,
                        "ceiling_minor": 9000,
                        "currency": "EUR",
                        "updated_at": "2026-09-16T12:00:00+00:00",
                        "updated_by": "owner-1",
                    },
                    _ACCOUNT: {
                        "daily_cap_minor": 2000,
                        "monthly_cap_minor": 40000,
                        "ceiling_minor": 12000,
                        "currency": "EUR",
                        "updated_at": "2026-09-16T12:00:00+00:00",
                        "updated_by": "owner-1",
                    },
                },
                "changes_by_day": {"2026-09-16": 3},
            }
        )
    )
    return path


async def test_i1_an_unreadable_document_refuses_every_write(tmp_path: Path) -> None:
    """Escribir partiendo de una instantanea vacia BORRARIA los topes de las
    demas cuentas y el presupuesto de cambios ya gastado -- y ese borrado le
    daria a un `ads-api` comprometido un presupuesto nuevo. Leer puede caer
    al fichero (siempre mas restrictivo); escribir a ciegas, no."""
    state_path = _unreadable_state(tmp_path)
    before = state_path.read_bytes()
    runtime = _runtime(tmp_path)

    stored = await _call(runtime, _set(_PANEL_ONLY))
    withdrawn = await _call(
        runtime,
        {
            "op": "delete_account_caps",
            "platform_account_id": _ACCOUNT,
            "requested_by": _REQUESTED_BY,
        },
    )

    assert stored["error_code"] == "CAPS_STATE_UNWRITABLE"
    assert withdrawn["error_code"] == "CAPS_STATE_UNWRITABLE"
    assert state_path.read_bytes() == before


async def test_i1_an_unreadable_document_still_resolves_from_the_file(tmp_path: Path) -> None:
    """La otra mitad del teorema: leer NO se rompe, cae al fichero."""
    _unreadable_state(tmp_path)
    runtime = _runtime(tmp_path)

    reply = await _call(runtime, {"op": "resolve_account_caps", "platform_account_id": _ACCOUNT})

    assert reply["result"]["source"] == "file"
    assert reply["result"]["effective"]["daily_cap_minor"] == 4000
    assert reply["result"]["panel_state_available"] is False


# --- I-2: una denegacion dice que se pidio, que habia y con que sobre ----


def _denials(logs: list[dict[str, object]]) -> list[dict[str, object]]:
    return [entry for entry in logs if entry["event"] == "broker_account_caps_denied"]


async def test_i2_a_denial_records_what_was_asked_the_before_and_the_envelope(
    tmp_path: Path,
) -> None:
    """La traza del broker es la que sobrevive a un compromiso de `ads-api`.
    Sin lo pedido, el antes y el sobre en vigor no permite reconstruir que
    se intento -- solo que algo se rechazo."""
    runtime = _runtime(tmp_path)
    await _call(runtime, _set(_PANEL_ONLY, daily_cap_minor=1000))

    with structlog.testing.capture_logs() as logs:
        reply = await _call(runtime, _set(_PANEL_ONLY, daily_cap_minor=5001))

    assert reply["error_code"] == "ENVELOPE_EXCEEDED"
    record = _denials(logs)[0]
    assert record["error_code"] == "ENVELOPE_EXCEEDED"
    assert record["platform_account_id"] == _PANEL_ONLY
    assert record["requested"] == {
        "daily_cap_minor": 5001,
        "monthly_cap_minor": 20000,
        "ceiling_minor": 9000,
        "currency": "EUR",
    }
    assert record["before"] == {
        "daily_cap_minor": 1000,
        "monthly_cap_minor": 20000,
        "ceiling_minor": 9000,
    }
    assert record["envelope"]["max_daily_cap_minor"] == 5000  # type: ignore[index]
    assert record["requested_by"] == _REQUESTED_BY


async def test_i2_a_deployment_without_an_envelope_audits_a_null_envelope(
    tmp_path: Path,
) -> None:
    """`envelope=None` no es un hueco: es lo que EXPLICA el rechazo."""
    with structlog.testing.capture_logs() as logs:
        await _call(_runtime(tmp_path, with_envelope=False), _set())

    record = _denials(logs)[0]
    assert record["error_code"] == "ENVELOPE_NOT_DECLARED"
    assert record["envelope"] is None
    assert record["requested"] is not None


async def test_i2_an_id_that_is_not_an_account_id_is_audited_too(tmp_path: Path) -> None:
    """Si el unico rechazo sin traza del broker fuese el que un atacante
    provoca a voluntad, la auditoria tendria un punto ciego elegible."""
    with structlog.testing.capture_logs() as logs:
        reply = await _call(_runtime(tmp_path), _set("cuenta/otra"))

    assert reply["error_code"] == "INVALID_CAPS"
    assert _denials(logs)[0]["error_code"] == "INVALID_CAPS"


async def test_i2_the_audited_account_label_carries_nothing_printable_from_the_caller(
    tmp_path: Path,
) -> None:
    """Un id manipulado no se registra tal cual: no salta de linea en el
    log. Mas de 64 caracteres ni llega hasta aqui -- el esquema del socket
    los corta antes --, asi que el recorte es la segunda vuelta de llave."""
    with structlog.testing.capture_logs() as logs:
        reply = await _call(_runtime(tmp_path), _set("act\n\r_9999"))

    assert reply["error_code"] == "INVALID_CAPS"
    label = _denials(logs)[0]["platform_account_id"]
    assert isinstance(label, str)
    assert "\n" not in label and "\r" not in label
    assert len(label) <= 64


# --- request_id: el nonce de la confirmacion del panel -------------------


async def test_the_request_id_reaches_the_broker_audit(tmp_path: Path) -> None:
    runtime = _runtime(tmp_path)

    with structlog.testing.capture_logs() as logs:
        applied = await _call(runtime, _set(_PANEL_ONLY) | {"request_id": "nonce-1234"})
        denied = await _call(
            runtime, _set(_PANEL_ONLY, daily_cap_minor=5001) | {"request_id": "nonce-5678"}
        )

    assert applied["ok"] is True
    assert denied["error_code"] == "ENVELOPE_EXCEEDED"
    trail = [entry for entry in logs if str(entry["event"]).startswith("broker_account_caps_")]
    assert [entry["request_id"] for entry in trail] == ["nonce-1234", "nonce-5678"]


# --- M-2: retirar tiene su propio evento, y es idempotente ---------------


async def test_m2_a_withdrawal_has_its_own_event(tmp_path: Path) -> None:
    runtime = _runtime(tmp_path)
    await _call(runtime, _set(_PANEL_ONLY))

    with structlog.testing.capture_logs() as logs:
        await _call(
            runtime,
            {
                "op": "delete_account_caps",
                "platform_account_id": _PANEL_ONLY,
                "requested_by": _REQUESTED_BY,
            },
        )

    applied = [entry for entry in logs if entry["event"] == "broker_account_caps_deleted"]
    assert len(applied) == 1
    assert applied[0]["before"] == {
        "daily_cap_minor": 1000,
        "monthly_cap_minor": 20000,
        "ceiling_minor": 9000,
    }
    assert applied[0]["after"] is None


async def test_a_withdrawal_without_a_panel_entry_writes_nothing_and_audits_nothing(
    tmp_path: Path,
) -> None:
    """Idempotente de verdad: ni disco, ni presupuesto de cambios, ni una
    traza de "aplicado" que no aplico nada. Un bucle de DELETE desde un
    `ads-api` comprometido no es ni trabajo ni ruido."""
    runtime = _runtime(tmp_path)
    state_path = tmp_path / "caps-state" / "panel-caps.json"

    with structlog.testing.capture_logs() as logs:
        reply = await _call(
            runtime,
            {
                "op": "delete_account_caps",
                "platform_account_id": _ACCOUNT,
                "requested_by": _REQUESTED_BY,
            },
        )

    assert reply["ok"] is True
    assert reply["result"]["source"] == "file"
    assert not state_path.exists()
    assert not [entry for entry in logs if "caps" in str(entry["event"])]

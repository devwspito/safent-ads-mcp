from dataclasses import replace

import pytest

from safent_ads.accounts.application.ports import WriteIntent, WriteOperation
from safent_ads.broker.domain.operation_semantics import matches_signed_transition
from safent_ads.shared.ids import EntityRef


def intent(parameter="status", before="ACTIVE", after="PAUSED", operation=WriteOperation.PAUSE):
    return WriteIntent(
        EntityRef.parse("meta:campaign:act_1/2"),
        operation,
        parameter,
        before,
        after,
        "hash",
        "state",
    )


def test_signed_pause_cannot_be_swapped_for_resume():
    approved = intent()
    assert matches_signed_transition(approved)
    assert not matches_signed_transition(replace(approved, operation=WriteOperation.RESUME))


@pytest.mark.parametrize("amount", ["NaN", "Infinity", "-1", "0.001"])
def test_invalid_money_cannot_escape_caps(amount):
    assert not matches_signed_transition(
        intent(
            "daily_budget",
            {"amount": "10", "currency": "EUR"},
            {"amount": amount, "currency": "EUR"},
            WriteOperation.LOWER_BUDGET,
        )
    )


def test_budget_direction_and_currency_bound_to_signature():
    change = intent(
        "daily_budget",
        {"amount": "10", "currency": "EUR"},
        {"amount": "12", "currency": "EUR"},
        WriteOperation.RAISE_BUDGET,
    )
    assert matches_signed_transition(change)
    assert not matches_signed_transition(replace(change, operation=WriteOperation.LOWER_BUDGET))
    assert not matches_signed_transition(
        replace(change, valor_propuesto={"amount": "12", "currency": "USD"})
    )


def test_unknown_parameter_and_noop_fail_closed():
    assert not matches_signed_transition(intent("unknown"))
    assert not matches_signed_transition(intent(after="ACTIVE"))


def test_signed_delete_cannot_be_swapped_for_pause():
    deleted = intent(before="ACTIVE", after="DELETED", operation=WriteOperation.DELETE)
    assert matches_signed_transition(deleted)
    assert not matches_signed_transition(replace(deleted, operation=WriteOperation.PAUSE))


def test_native_write_binds_to_its_own_operation_only():
    """004 tasks-2.md W3: sin esta rama, `propose_native_write` firmado y
    aprobado nunca pasaba de `matches_signed_transition` -- el prefijo
    `native:` no aparecia en `_BY_PARAMETER`."""
    native = intent(
        "native:meta:update_targeting",
        None,
        {"custom_audiences": ["123"]},
        WriteOperation.NATIVE_WRITE,
    )
    assert matches_signed_transition(native)
    assert not matches_signed_transition(replace(native, operation=WriteOperation.SET_TARGETING))


def test_native_write_con_status_o_presupuesto_no_casa_la_transicion_firmada():
    """A-3: `validate_native_write_payload` (mcp/domain, el mismo puro que
    ya se aplico al proponer) tambien se aplica AQUI -- un payload que la
    lista negra ya rechazaria (status, daily_budget, access_token...) no
    puede colarse aunque alguien lo firmara."""
    for forbidden_payload in (
        {"status": "PAUSED"},
        {"daily_budget": {"amount": "10"}},
        {"access_token": "EAAxxx"},
        {"configured_status": "PAUSED"},
    ):
        native = intent(
            "native:meta:update_targeting",
            None,
            forbidden_payload,
            WriteOperation.NATIVE_WRITE,
        )
        assert not matches_signed_transition(native)


def test_creative_publication_at_ad_set_level_matches_rotate_out_creative():
    """H-1 (004 tasks-2.md §1): `propose_creative_publication` firma
    `parameter="creative"` a nivel de ad set con un payload de
    `creative_asset_ids`/`ad_copy` -- distinto del rotar-fuera a nivel de
    anuncio, pero mapeado a la misma `WriteOperation.ROTATE_OUT_CREATIVE`
    (`composition/mcp_write_adapter.py::_CREATIVE_PARAMETER`)."""
    publication = intent(
        "creative",
        None,
        {"creative_asset_ids": ["a1"], "ad_copy": {"headline": "x"}},
        WriteOperation.ROTATE_OUT_CREATIVE,
    )
    publication = replace(publication, entity_ref=EntityRef.parse("meta:ad_set:act_1/2"))
    assert matches_signed_transition(publication)
    assert not matches_signed_transition(replace(publication, operation=WriteOperation.PAUSE))


def test_creative_rotation_at_ad_level_still_requires_paused_status():
    rotation = intent("creative", "ACTIVE", "PAUSED", WriteOperation.ROTATE_OUT_CREATIVE)
    rotation = replace(rotation, entity_ref=EntityRef.parse("meta:ad:act_1/2/3"))
    assert matches_signed_transition(rotation)
    assert not matches_signed_transition(replace(rotation, valor_propuesto="ACTIVE"))

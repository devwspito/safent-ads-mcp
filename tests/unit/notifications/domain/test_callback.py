"""`CallbackData` (contracts/telegram.md §`callback_data`): parseo/formato
≤64 bytes, generacion de nonce y TTL = min(proposal.expires_at, now+6h)."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from safent_ads.notifications.domain.callback import (
    CallbackAction,
    CallbackData,
    callback_ttl,
    generate_nonce,
    short_proposal_id,
)
from safent_ads.notifications.domain.errors import InvalidCallbackDataError

_NOW = datetime(2026, 9, 9, 14, 0, tzinfo=UTC)


def test_encode_matches_contract_example() -> None:
    data = CallbackData(
        proposal_id_short="9f3a1c07", nonce="Kd7Qx2mVaP", action=CallbackAction.APPROVE
    )

    encoded = data.encode()

    assert encoded == "p:9f3a1c07:Kd7Qx2mVaP:a"
    # contracts/telegram.md dice "24 bytes" para este ejemplo exacto; el
    # recuento real de la cadena literal del contrato es 23 -- error
    # aritmetico menor del documento, no del formato (que sigue <= 64).
    assert len(encoded.encode("ascii")) == 23


def test_parse_round_trips_encode() -> None:
    data = CallbackData(
        proposal_id_short="9f3a1c07", nonce="Kd7Qx2mVaP", action=CallbackAction.CONFIRM
    )

    parsed = CallbackData.parse(data.encode())

    assert parsed == data


@pytest.mark.parametrize(
    "raw",
    [
        "p:short:Kd7Qx2mVaP:a",  # proposal_id_short corto
        "p:9f3a1c07:tooshort:a",  # nonce corto
        "p:9f3a1c07:Kd7Qx2mVaP:z",  # accion fuera del alfabeto
        "p:9f3a1c07:Kd7Qx2mVaP",  # sin accion
        "libre texto",
    ],
)
def test_parse_rejects_malformed_data(raw: str) -> None:
    with pytest.raises(InvalidCallbackDataError):
        CallbackData.parse(raw)


def test_build_derives_short_id_from_full_proposal_uuid() -> None:
    data = CallbackData.build(
        proposal_id="9f3a1c07-1234-4321-8888-abcdefabcdef",
        nonce="Kd7Qx2mVaP",
        action=CallbackAction.REJECT,
    )

    assert data.proposal_id_short == "9f3a1c07"


def test_short_proposal_id_is_first_eight_hex_chars() -> None:
    assert short_proposal_id("9f3a1c07-1234-4321-8888-abcdefabcdef") == "9f3a1c07"


def test_generate_nonce_is_ten_base62_chars_and_not_deterministic() -> None:
    first = generate_nonce()
    second = generate_nonce()

    assert len(first) == 10
    assert first.isalnum()
    assert first != second


def test_callback_ttl_caps_at_six_hours() -> None:
    proposal_expires_at = _NOW + timedelta(days=3)

    ttl = callback_ttl(proposal_expires_at=proposal_expires_at, now=_NOW)

    assert ttl == _NOW + timedelta(hours=6)


def test_callback_ttl_never_outlives_the_proposal() -> None:
    proposal_expires_at = _NOW + timedelta(hours=2)

    ttl = callback_ttl(proposal_expires_at=proposal_expires_at, now=_NOW)

    assert ttl == proposal_expires_at

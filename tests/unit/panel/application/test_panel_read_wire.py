"""Real SQL row mappers and JSON serializer share a fixture with the panel.

The installed API returned PAUSED/HOLD as paused/hold. Fake-only REST and
frontend fixtures each passed in isolation while both real pages failed Zod.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path
from typing import cast

import pytest
from sqlalchemy.engine import RowMapping
from sqlalchemy.ext.asyncio import AsyncSession

from safent_ads.panel.infrastructure.sql_read_model import SqlPanelReadPort
from safent_ads.shared.read_models.serialization import to_json_dict

_NOW = datetime(2026, 9, 14, 9, tzinfo=UTC)
_REF = "google:campaign:1234567890"
_FIXTURE = Path(__file__).resolve().parents[3] / "contracts" / "panel-read-rows.json"


def _entity(status: str = "PAUSED") -> RowMapping:
    return cast(
        RowMapping,
        {
            "entity_ref": _REF,
            "name": "Synthetic campaign",
            "platform": "google",
            "platform_account_id": "22222222-2222-2222-2222-222222222222",
            "platform_account_ref": "google:account:22222222-2222-2222-2222-222222222222",
            "status": status,
            "currency": "EUR",
            "budget_amount_minor": 1000,
            "budget_currency": "EUR",
            "is_controllable": True,
            "learning_state": "NOT_APPLICABLE",
            "account_status": "ACTIVE",
        },
    )


def _signal(kind: str = "HOLD") -> RowMapping:
    return cast(
        RowMapping,
        {
            "id": "33333333-3333-3333-3333-333333333333",
            "business_id": "11111111-1111-1111-1111-111111111111",
            "entity_ref": _REF,
            "kind": kind,
            "strength": 25,
            "cause": "Synthetic evidence",
            "money_at_stake_minor": 0,
            "money_at_stake_currency": "EUR",
            "data_window": "7D",
            "emitted_at": _NOW,
        },
    )


def _port() -> SqlPanelReadPort:
    # The selected row mapping methods are pure; no database/provider access.
    return SqlPanelReadPort(cast(AsyncSession, None))


def test_real_sql_mappers_match_shared_typescript_wire_fixture() -> None:
    port = _port()
    actual = {
        "portfolio_row": to_json_dict(
            port._portfolio_row(
                _entity(),
                {},
                {},
                {_REF: _signal()},
                _NOW.date(),
            )
        ),
        "signal_row": to_json_dict(port._signal_view(_signal(), _NOW)),
    }
    assert actual == json.loads(_FIXTURE.read_text())


@pytest.mark.parametrize("status", ["ACTIVE", "PAUSED", "REMOVED", "DRIFTED", "LEARNING"])
def test_portfolio_preserves_database_status_enum(status: str) -> None:
    row = _port()._portfolio_row(_entity(status), {}, {}, {}, _NOW.date())
    assert row.status == status
    assert row.signal is None


@pytest.mark.parametrize("kind", ["BUY", "HOLD", "SELL", "EXIT"])
def test_signal_kind_matches_in_portfolio_and_signal_feed(kind: str) -> None:
    port = _port()
    row = port._portfolio_row(_entity(), {}, {}, {_REF: _signal(kind)}, _NOW.date())
    assert row.signal is not None
    assert row.signal.kind == port._signal_view(_signal(kind), _NOW).kind == kind

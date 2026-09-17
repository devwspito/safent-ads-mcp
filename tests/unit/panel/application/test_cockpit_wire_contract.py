"""A shared wire fixture consumed by the panel, not a UI-only mock contract."""

from __future__ import annotations

import json
from pathlib import Path

from safent_ads.panel.application.cockpit_dto import DetailRef
from safent_ads.shared.read_models.serialization import to_cockpit_json_value


def test_python_serialization_matches_the_shared_panel_fixture() -> None:
    fixture = Path(__file__).resolve().parents[3] / "contracts" / "cockpit-wire.json"
    payload = {
        "sparkline": (0.0,) * 14,
        "detail_refs": (
            DetailRef(kind="signal", id="signal-1"),
            DetailRef(kind="proposal", id="proposal-1"),
            DetailRef(kind="execution", id="execution-1"),
            None,
        ),
    }
    assert to_cockpit_json_value(payload) == json.loads(fixture.read_text())

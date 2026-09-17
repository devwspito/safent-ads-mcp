"""`propose_campaign_draft` vs `ADS_GOOGLE_CHANNELS_ENABLED` (tasks.md T076,
POLISH "canales por configuracion"): rechazado antes de tocar ningun
puerto (`resolve_account_ref`, `store.save`) -- ni siquiera se guarda un
borrador para un canal apagado."""

from __future__ import annotations

import pytest

from safent_ads.mcp.application.errors import ToolValidationError
from safent_ads.mcp.presentation.campaign_draft_tools import DraftSaveArgs, build_draft_tools
from safent_ads.proposals.domain.google_channel_spec import GoogleAdvertisingChannelType

BUSINESS = "0609e9cf-e861-4c9b-94cf-4611e527fc69"

_DISPLAY_PLAN: dict[str, object] = {
    "schema_version": 1,
    "platform": "google",
    "name": "Reserva de citas",
    "status": "PAUSED",
    "daily_budget": {"amount": "20.00", "currency": "EUR"},
    "native": {
        "advertising_channel_type": "DISPLAY",
        "bidding_strategy": {"kind": "MANUAL_CPC"},
        "contains_eu_political_advertising": "DOES_NOT_CONTAIN_EU_POLITICAL_ADVERTISING",
    },
}


class _Store:
    def __init__(self) -> None:
        self.saved: list[tuple] = []

    async def save(self, business_id, draft_key, expected_revision, changes):  # noqa: ANN001
        self.saved.append((business_id, draft_key, expected_revision, changes))
        return {"draft_id": "d1", "revision": 1}


def _save_args(**changes: object) -> DraftSaveArgs:
    return DraftSaveArgs.model_validate(
        {
            "business_id": BUSINESS,
            "draft_key": "acme-google-consulta-2026-09",
            "changes": {"title": "Consulta veterinaria", "platform": "google", **changes},
        }
    )


async def test_performance_max_channel_type_denied_by_default_before_saving() -> None:
    store = _Store()
    tools = {tool.name: tool for tool in build_draft_tools(store)}  # type: ignore[arg-type]
    args = _save_args(google_channel_type="PERFORMANCE_MAX")

    with pytest.raises(ToolValidationError, match="CHANNEL_TYPE_NOT_ENABLED") as excinfo:
        await tools["propose_campaign_draft"].handler(args, None)

    assert "PERFORMANCE_MAX" in str(excinfo.value)
    assert store.saved == []


async def test_display_creation_plan_denied_by_default_before_saving() -> None:
    store = _Store()
    tools = {tool.name: tool for tool in build_draft_tools(store)}  # type: ignore[arg-type]
    args = _save_args(creation_plan=_DISPLAY_PLAN)

    with pytest.raises(ToolValidationError, match="CHANNEL_TYPE_NOT_ENABLED"):
        await tools["propose_campaign_draft"].handler(args, None)

    assert store.saved == []


async def test_search_channel_type_stays_allowed_by_default() -> None:
    store = _Store()
    tools = {tool.name: tool for tool in build_draft_tools(store)}  # type: ignore[arg-type]
    args = _save_args(google_channel_type="SEARCH")

    await tools["propose_campaign_draft"].handler(args, None)

    assert len(store.saved) == 1


async def test_performance_max_accepted_when_enabled() -> None:
    store = _Store()
    tools = {
        tool.name: tool
        for tool in build_draft_tools(
            store,
            enabled_google_channels=frozenset(
                {GoogleAdvertisingChannelType.SEARCH, GoogleAdvertisingChannelType.PERFORMANCE_MAX}
            ),
        )  # type: ignore[arg-type]
    }
    args = _save_args(google_channel_type="PERFORMANCE_MAX")

    await tools["propose_campaign_draft"].handler(args, None)

    assert len(store.saved) == 1

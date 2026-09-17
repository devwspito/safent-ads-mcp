"""Reglas puras de ensamblado de `get_project_context`
(`mcp.domain.context_pack`): resumen de inventario, top/bottom performers,
resumen de marca y el recorte de tamano de payload."""

from __future__ import annotations

import json
from datetime import UTC, datetime
from decimal import Decimal

from safent_ads.mcp.application.dto import (
    BrandAssetSummary,
    BrandKitDetail,
    CampaignStatus,
    CampaignSummary,
    ColorSwatchDetail,
    Page,
    SignalKind,
    ToneOfVoiceDetail,
    TopMover,
    TypographyDetail,
)
from safent_ads.mcp.domain.context_pack import (
    MAX_PAYLOAD_BYTES,
    cap_payload_size,
    summarize_brand_kit,
    summarize_inventory,
    top_and_bottom_movers,
)
from safent_ads.shared.read_models.dto import Money


def _campaign(
    *, status: CampaignStatus, learning_state: str, controllable: bool
) -> CampaignSummary:
    return CampaignSummary(
        f"google:campaign:{id(object())}",
        "Campana",
        status,
        Money(Decimal("10.00")),
        learning_state,
        controllable,
        SignalKind.HOLD,
    )


def test_summarize_inventory_counts_by_status_and_learning_state() -> None:
    page = Page(
        items=[
            _campaign(status=CampaignStatus.ACTIVE, learning_state="learned", controllable=True),
            _campaign(status=CampaignStatus.ACTIVE, learning_state="learning", controllable=True),
            _campaign(status=CampaignStatus.PAUSED, learning_state="learned", controllable=False),
        ],
        cursor=None,
    )

    summary = summarize_inventory(page, requested_limit=200)

    assert summary.total_campaigns == 3
    assert summary.active_campaigns == 2
    assert summary.paused_campaigns == 1
    assert summary.learning_campaigns == 1
    assert summary.controllable_campaigns == 2
    assert summary.truncated is False


def test_summarize_inventory_flags_truncation_when_page_has_more() -> None:
    page = Page(items=[], cursor="opaque-cursor")

    summary = summarize_inventory(page, requested_limit=200)

    assert summary.truncated is True


def test_top_and_bottom_movers_orders_by_delta() -> None:
    movers = [
        TopMover("e1", "Peor", -30.0, "cpl"),
        TopMover("e2", "Mejor", 50.0, "cpl"),
        TopMover("e3", "Medio", 5.0, "cpl"),
    ]

    top, bottom = top_and_bottom_movers(movers, limit=2)

    assert [m.entity_ref for m in top] == ["e2", "e3"]
    assert [m.entity_ref for m in bottom] == ["e1", "e3"]


def test_top_and_bottom_movers_handles_empty_list() -> None:
    top, bottom = top_and_bottom_movers([], limit=5)

    assert top == []
    assert bottom == []


def test_summarize_brand_kit_reports_completeness_and_counts() -> None:
    detail = BrandKitDetail(
        brand_kit_id="kit-1",
        business_id="biz-1",
        typography=TypographyDetail("Fake Sans", None, "SIL OFL", ["regular"]),
        palette=[ColorSwatchDetail("primary", "#000000", 21.0, True)],
        tone_of_voice=ToneOfVoiceDetail("Cercano.", [], []),
        assets=[
            BrandAssetSummary("logo-1", "logo_vector", "s3://logo.svg", "no deformar"),
            BrandAssetSummary("photo-1", "reference_photo", "s3://photo.jpg", "x"),
        ],
        claims_allowlist=[],
        forbidden_claims=["garantizado"],
        legal_disclaimers=[],
        platform_constraints=[],
        is_complete=True,
        updated_at=datetime.now(UTC),
    )

    summary = summarize_brand_kit(detail)

    assert summary.logos_count == 1
    assert summary.has_typography is True
    assert summary.has_palette is True
    assert summary.forbidden_claims_count == 1
    assert summary.is_complete is True


def test_cap_payload_size_is_a_noop_when_already_under_the_cap() -> None:
    payload = {"signals": [{"id": 1}], "capability_notes": []}

    result = cap_payload_size(dict(payload))

    assert result == payload


def test_cap_payload_size_trims_lists_until_under_budget() -> None:
    oversized_note = "x" * 500
    payload = {
        "signals": [{"id": i, "cause": oversized_note} for i in range(200)],
        "open_proposals": [{"id": i, "cause": oversized_note} for i in range(200)],
        "capability_notes": [],
    }

    original_size = len(json.dumps(payload).encode("utf-8"))
    result = cap_payload_size(payload)
    trimmed_size = len(json.dumps(result, default=str).encode("utf-8"))

    assert trimmed_size <= MAX_PAYLOAD_BYTES
    assert trimmed_size < original_size
    assert "payload recortado" in " ".join(result["capability_notes"])

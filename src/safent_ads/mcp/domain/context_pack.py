"""Reglas puras de ensamblado de `get_project_context`: resumen de
inventario, top/bottom performers y el recorte por tamano de payload. Sin
I/O -- `GetProjectContext` (application) llama los puertos y le pasa los
DTOs ya resueltos a estas funciones."""

from __future__ import annotations

import json
from typing import Any

from safent_ads.mcp.application.dto import (
    BrandKitDetail,
    BrandKitSummary,
    CampaignStatus,
    CampaignSummary,
    InventorySummary,
    Page,
    TopMover,
)

MAX_PAYLOAD_BYTES = 32_768
"""32 KiB: el agente debe poder razonar sobre `get_project_context` en una
sola llamada (encargo de esta lane), no descargar un volcado completo."""

_LEARNED_STATE = "learned"
_TRIMMABLE_LIST_KEYS = (
    "offerings",
    "open_calendar_events",
    "signals",
    "open_proposals",
    "top_performers",
    "bottom_performers",
    "guardrails",
    "platform_accounts",
)
_SIZE_CAP_NOTE = "payload recortado para caber en el limite de tamano (32 KiB)"


def summarize_inventory(page: Page[CampaignSummary], *, requested_limit: int) -> InventorySummary:
    campaigns = page.items
    return InventorySummary(
        total_campaigns=len(campaigns),
        active_campaigns=sum(1 for c in campaigns if c.status == CampaignStatus.ACTIVE),
        paused_campaigns=sum(1 for c in campaigns if c.status == CampaignStatus.PAUSED),
        learning_campaigns=sum(1 for c in campaigns if c.learning_state != _LEARNED_STATE),
        controllable_campaigns=sum(1 for c in campaigns if c.is_controllable),
        truncated=page.cursor is not None or len(campaigns) >= requested_limit,
        note=(
            "recuento a nivel de campana; conjuntos de anuncios y anuncios "
            "no se cuentan aqui (usar list_ad_sets/list_ads por campana)"
        ),
    )


_LOGO_KINDS = ("logo_vector", "logo_raster")


def summarize_brand_kit(detail: BrandKitDetail) -> BrandKitSummary:
    return BrandKitSummary(
        logos_count=sum(1 for asset in detail.assets if asset.kind in _LOGO_KINDS),
        has_typography=bool(detail.typography.primary_family.strip()),
        has_palette=bool(detail.palette),
        has_tone_of_voice=bool(detail.tone_of_voice.description.strip()),
        forbidden_claims_count=len(detail.forbidden_claims),
        legal_disclaimers_count=len(detail.legal_disclaimers),
        is_complete=detail.is_complete,
        updated_at=detail.updated_at,
    )


def top_and_bottom_movers(
    movers: list[TopMover], *, limit: int
) -> tuple[list[TopMover], list[TopMover]]:
    ordered_best_first = sorted(movers, key=lambda mover: mover.delta_pct, reverse=True)
    top = ordered_best_first[:limit]
    bottom = list(reversed(ordered_best_first[-limit:])) if ordered_best_first else []
    return top, bottom


def cap_payload_size(payload: dict[str, Any]) -> dict[str, Any]:
    """Recorta listas variables (nunca los bloques escalares: negocio,
    inventario, autonomia) hasta caber en `MAX_PAYLOAD_BYTES`. Deja
    constancia del recorte en `capability_notes` -- nunca desaparece
    informacion en silencio."""
    if _json_size(payload) <= MAX_PAYLOAD_BYTES:
        return payload
    trimmed_any = _trim_lists(payload)
    if trimmed_any:
        notes = list(payload.get("capability_notes") or [])
        notes.append(_SIZE_CAP_NOTE)
        payload["capability_notes"] = notes
    return payload


def _trim_lists(payload: dict[str, Any]) -> bool:
    trimmed_any = False
    for key in _TRIMMABLE_LIST_KEYS:
        items = payload.get(key)
        if not isinstance(items, list):
            continue
        while items and _json_size(payload) > MAX_PAYLOAD_BYTES:
            items.pop()
            trimmed_any = True
        if _json_size(payload) <= MAX_PAYLOAD_BYTES:
            break
    return trimmed_any


def _json_size(payload: dict[str, Any]) -> int:
    return len(json.dumps(payload, default=str, ensure_ascii=False).encode("utf-8"))

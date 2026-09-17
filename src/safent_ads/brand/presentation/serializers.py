"""Traduce el agregado `BrandKit` a `dict` serializable, compartido por el
router REST y (via `mcp.application.serialization.to_json_value`, que ya
sabe recorrer dataclasses) los handlers MCP no necesitan esta forma
propia -- pero el panel si, con nombres estables independientes del DTO
interno de `mcp` (mismo motivo que `creative.presentation.serializers`)."""

from __future__ import annotations

from safent_ads.brand.domain.brand_asset import BrandAsset
from safent_ads.brand.domain.brand_kit import BrandKit
from safent_ads.brand.domain.claims_policy import DEFAULT_FORBIDDEN_CLAIMS
from safent_ads.brand.domain.discovery import BrandDiscoveryDraft, ColorCandidate, LogoCandidate

_FLOOR_FORBIDDEN_CLAIMS_CF = {claim.casefold() for claim in DEFAULT_FORBIDDEN_CLAIMS}


def asset_preview_url(asset_id: str) -> str:
    """URL relativa al mismo origen que sirve
    `GET /brand/assets/{asset_id}/preview` -- mismo nombre de campo que
    `creative.presentation.serializers` (`preview_url`), pero sin firma ni
    TTL: la autorizacion real es la sesion `ads_session` que el navegador
    ya envia en cualquier peticion del mismo sitio, incluida la de un
    `<img>` (rest-api.md §Marca)."""
    return f"/api/v1/brand/assets/{asset_id}/preview"


def brand_asset_summary(asset: BrandAsset) -> dict[str, object]:
    return {
        "asset_id": asset.asset_id,
        "kind": asset.kind.value,
        "url": asset.storage_uri,
        "usage": asset.usage_rule,
        "preview_url": asset_preview_url(asset.asset_id),
    }


def forbidden_claim_summary(claim: str) -> dict[str, object]:
    """`is_floor` distingue el suelo de seguridad (`DEFAULT_FORBIDDEN_CLAIMS`,
    nunca editable por el propietario) de lo que el propio negocio anadio
    encima (`PUT /brand/claims`) -- sin esto el panel no puede explicar
    por que un reclamo prohibido concreto no se puede quitar."""
    return {"claim": claim, "is_floor": claim.casefold() in _FLOOR_FORBIDDEN_CLAIMS_CF}


def brand_kit_detail(brand_kit: BrandKit) -> dict[str, object]:
    return {
        "brand_kit_id": str(brand_kit.brand_kit_id),
        "business_id": str(brand_kit.business_id),
        "typography": {
            "primary_family": brand_kit.typography.primary_family,
            "secondary_family": brand_kit.typography.secondary_family,
            "licence_note": brand_kit.typography.licence_note,
            "weights": list(brand_kit.typography.weights),
        },
        "palette": [
            {
                "role": swatch.role.value,
                "hex": swatch.hex,
                "contrast_ratio_on_white": swatch.contrast_ratio_on_white,
                "meets_wcag_aa_normal_text": swatch.meets_wcag_aa_normal_text(),
            }
            for swatch in brand_kit.palette.swatches
        ],
        "tone_of_voice": {
            "description": brand_kit.tone_of_voice.description,
            "adjectives": list(brand_kit.tone_of_voice.adjectives),
            "avoid": list(brand_kit.tone_of_voice.avoid),
        },
        "assets": [brand_asset_summary(asset) for asset in brand_kit.assets],
        "claims_allowlist": sorted(brand_kit.claims_allowlist),
        "forbidden_claims": [
            forbidden_claim_summary(claim) for claim in sorted(brand_kit.forbidden_claims)
        ],
        "legal_disclaimers": [
            {
                "text": disclaimer.text,
                "applies_to": [p.value for p in disclaimer.applies_to]
                if disclaimer.applies_to is not None
                else None,
            }
            for disclaimer in brand_kit.legal_disclaimers
        ],
        "platform_constraints": [
            {
                "platform": constraint.platform.value,
                "max_headline_chars": constraint.max_headline_chars,
                "requires_disclaimer": constraint.requires_disclaimer,
                "notes": constraint.notes,
            }
            for constraint in brand_kit.platform_constraints
        ],
        "is_complete": brand_kit.is_complete(),
        "is_confirmed": brand_kit.is_confirmed,
        "confirmed_website_host": brand_kit.confirmed_website_host,
        "updated_at": brand_kit.updated_at.isoformat(),
    }


def draft_detail(draft: BrandDiscoveryDraft) -> dict[str, object]:
    """Forma estable para el panel y para el handler MCP `get_brand_draft`
    (owner request: "el usuario ... pone el enlace y que se rastree";
    esto es lo que revisa antes de `confirm_brand_draft`)."""
    return {
        "business_id": str(draft.business_id),
        "source_url": draft.source_url,
        "discovered_at": draft.discovered_at.isoformat(),
        "logo_candidates": [_logo_candidate_summary(c) for c in draft.logo_candidates],
        "color_candidates": [_color_candidate_summary(c) for c in draft.color_candidates],
        "typography_candidates": [
            {"family": c.family, "source": c.source.value, "confidence": c.confidence}
            for c in draft.typography_candidates
        ],
        "business_name_candidates": [
            {"name": c.name, "source": c.source.value, "confidence": c.confidence}
            for c in draft.business_name_candidates
        ],
        "social_links": [
            {"network": link.network.value, "url": link.url} for link in draft.social_links
        ],
        "contact_channels": [
            {"kind": c.kind.value, "page_url": c.page_url} for c in draft.contact_channels
        ],
        "copy_samples": [
            {"source": s.source.value, "text": s.text} for s in draft.copy_samples
        ],
    }


def _logo_candidate_summary(candidate: LogoCandidate) -> dict[str, object]:
    return {
        "asset_id": candidate.asset_id,
        "kind": candidate.kind.value,
        "storage_uri": candidate.storage_uri,
        "source": candidate.source.value,
        "confidence": candidate.confidence,
        "preview_url": asset_preview_url(candidate.asset_id),
    }


def _color_candidate_summary(candidate: ColorCandidate) -> dict[str, object]:
    return {
        "hex": candidate.hex,
        "source": candidate.source.value,
        "confidence": candidate.confidence,
        "role_hint": candidate.role_hint.value if candidate.role_hint is not None else None,
    }

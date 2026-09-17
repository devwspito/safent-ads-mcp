"""DTO de entrada de un `CreativeBrief` completo, compartido por
`mcp_tools.py` (`generate_creative_assets { brief }`) y `router.py`
(`POST /creative-jobs { brief }`) — la misma validacion estricta
(threat-model.md C-11) para las dos superficies que aceptan un brief
inline."""

from __future__ import annotations

from typing import Annotated

from pydantic import BaseModel, ConfigDict, Field

from safent_ads.creative.application.ports import ProposedAdCopy
from safent_ads.creative.domain.brand_kit import BrandKit, SafeArea
from safent_ads.creative.domain.brief import CreativeBrief
from safent_ads.creative.domain.enums import CampaignObjective
from safent_ads.creative.domain.identifiers import AssetId, CalendarEventId, SignalId
from safent_ads.creative.domain.shot import ShotDescription
from safent_ads.shared.ids import BusinessId

ULID_PATTERN = r"^[0-9A-HJKMNP-TV-Z]{26}$"
UUID_PATTERN = r"^[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}$"
_ENTITY_REF_PATTERN = r"^[a-z]+:[a-z_]+:.+$"
_MAX_SHOTS = 5
_MIN_SHOTS = 3
_MAX_VARIANT_COUNT = 8
_MAX_REASON_LEN = 500
_MAX_EXTRA_ASSETS = 10


class StrictModel(BaseModel):
    """Base comun: rechaza campos no declarados (threat-model.md C-11)."""

    model_config = ConfigDict(extra="forbid", frozen=True)


class ShotPayload(StrictModel):
    order: int = Field(ge=1, le=_MAX_SHOTS)
    description: str = Field(min_length=1, max_length=280)
    duration_s: int | None = Field(default=None, gt=0)


class BrandKitPayload(StrictModel):
    primary_font: str = Field(min_length=1, max_length=100)
    secondary_font: str = Field(min_length=1, max_length=100)
    primary_color_hex: str = Field(pattern=r"^#[0-9A-Fa-f]{6}$")
    secondary_color_hex: str = Field(pattern=r"^#[0-9A-Fa-f]{6}$")
    logo_asset_id: str = Field(pattern=ULID_PATTERN)
    safe_area_top: float = Field(default=0.0, ge=0.0, le=0.5)
    safe_area_bottom: float = Field(default=0.0, ge=0.0, le=0.5)
    safe_area_left: float = Field(default=0.0, ge=0.0, le=0.5)
    safe_area_right: float = Field(default=0.0, ge=0.0, le=0.5)


class CreativeBriefPayload(StrictModel):
    business_id: str = Field(pattern=UUID_PATTERN)
    calendar_event_id: str | None = Field(default=None, pattern=UUID_PATTERN)
    objective: CampaignObjective
    audience_summary: str = Field(min_length=1, max_length=280)
    hook: str = Field(min_length=1, max_length=200)
    shots: list[ShotPayload] = Field(min_length=_MIN_SHOTS, max_length=_MAX_SHOTS)
    on_screen_text: list[str] = Field(default_factory=list, max_length=10)
    cta: str = Field(min_length=1, max_length=100)
    voiceover_lines: list[str] = Field(default_factory=list, max_length=20)
    brand_kit: BrandKitPayload
    source_signal_id: str | None = Field(default=None, pattern=UUID_PATTERN)
    variant_count: int = Field(ge=1, le=_MAX_VARIANT_COUNT)


def brief_from_payload(payload: CreativeBriefPayload) -> CreativeBrief:
    brand_kit = BrandKit(
        primary_font=payload.brand_kit.primary_font,
        secondary_font=payload.brand_kit.secondary_font,
        primary_color_hex=payload.brand_kit.primary_color_hex,
        secondary_color_hex=payload.brand_kit.secondary_color_hex,
        logo_asset_id=AssetId.parse(payload.brand_kit.logo_asset_id),
        safe_area=SafeArea(
            top=payload.brand_kit.safe_area_top,
            bottom=payload.brand_kit.safe_area_bottom,
            left=payload.brand_kit.safe_area_left,
            right=payload.brand_kit.safe_area_right,
        ),
    )
    return CreativeBrief(
        business_id=BusinessId.parse(payload.business_id),
        calendar_event_id=(
            CalendarEventId.parse(payload.calendar_event_id) if payload.calendar_event_id else None
        ),
        objective=payload.objective,
        audience_summary=payload.audience_summary,
        hook=payload.hook,
        shots=[
            ShotDescription(order=s.order, description=s.description, duration_s=s.duration_s)
            for s in payload.shots
        ],
        on_screen_text=payload.on_screen_text,
        cta=payload.cta,
        voiceover_lines=payload.voiceover_lines,
        brand_kit=brand_kit,
        source_signal_id=(
            SignalId.parse(payload.source_signal_id) if payload.source_signal_id else None
        ),
        variant_count=payload.variant_count,
    )


class RejectCreativeRequest(StrictModel):
    reason: str = Field(min_length=1, max_length=_MAX_REASON_LEN)


class RegenerateCreativeRequest(StrictModel):
    reason: str = Field(min_length=1, max_length=_MAX_REASON_LEN)


class ProposedAdCopyPayload(StrictModel):
    headline: str = Field(min_length=1, max_length=200)
    primary_text: str = Field(min_length=1, max_length=2000)
    cta: str = Field(min_length=1, max_length=100)


class ProposePublicationRequest(StrictModel):
    """`POST /creatives/{asset_id}/propose-publication` (rest-api.md
    §Creatividades). `ad_set_ref` sigue el formato canonico
    `<platform>:<level>:<external_id>` (`shared.ids.EntityRef`) -- el
    patron aqui es defensa en profundidad de forma; `EntityRef.parse` en
    `composition/creative_proposal_gateway.py` es quien de verdad decide
    si resuelve a una entidad existente del mismo negocio."""

    ad_set_ref: str = Field(pattern=_ENTITY_REF_PATTERN, max_length=300)
    ad_copy: ProposedAdCopyPayload
    extra_asset_ids: list[Annotated[str, Field(pattern=ULID_PATTERN)]] = Field(
        default_factory=list, max_length=_MAX_EXTRA_ASSETS
    )
    typed_confirmation: str = Field(min_length=1, max_length=50)


def proposed_ad_copy_from_payload(payload: ProposedAdCopyPayload) -> ProposedAdCopy:
    return ProposedAdCopy(
        headline=payload.headline, primary_text=payload.primary_text, cta=payload.cta
    )

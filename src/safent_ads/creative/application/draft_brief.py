"""`DraftBrief` (plan.md §5 `creative`): registra un `CreativeBrief` nuevo.
Sin renderizar nada; eso es `GenerateCreativeAssets`."""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass

from safent_ads.creative.application.ports import CreativeBriefRepository
from safent_ads.creative.domain.brand_kit import BrandKit
from safent_ads.creative.domain.brief import CreativeBrief
from safent_ads.creative.domain.enums import CampaignObjective, Language
from safent_ads.creative.domain.identifiers import BriefId, CalendarEventId, SignalId
from safent_ads.creative.domain.shot import ShotDescription
from safent_ads.shared.ids import BusinessId


@dataclass(frozen=True, kw_only=True)
class DraftBriefRequest:
    business_id: BusinessId
    calendar_event_id: CalendarEventId | None
    objective: CampaignObjective
    audience_summary: str
    hook: str
    shots: Sequence[ShotDescription]
    on_screen_text: Sequence[str]
    cta: str
    voiceover_lines: Sequence[str]
    brand_kit: BrandKit
    source_signal_id: SignalId | None
    variant_count: int
    language: Language = Language.ES_ES


class DraftBrief:
    def __init__(self, briefs: CreativeBriefRepository) -> None:
        self._briefs = briefs

    async def execute(self, request: DraftBriefRequest) -> BriefId:
        brief = CreativeBrief(
            business_id=request.business_id,
            calendar_event_id=request.calendar_event_id,
            objective=request.objective,
            audience_summary=request.audience_summary,
            hook=request.hook,
            shots=request.shots,
            on_screen_text=request.on_screen_text,
            cta=request.cta,
            voiceover_lines=request.voiceover_lines,
            brand_kit=request.brand_kit,
            source_signal_id=request.source_signal_id,
            variant_count=request.variant_count,
            language=request.language,
        )
        brief_id = BriefId.new()
        await self._briefs.add(brief_id, brief)
        return brief_id

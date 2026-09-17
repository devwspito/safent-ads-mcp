"""`package_codec` (T027): ida y vuelta exacta -- incluido `preview_key`,
que `to_canonical()` deja fuera a proposito de la huella (BL-1)."""

from __future__ import annotations

from datetime import UTC, datetime

from safent_ads.packages.domain.values import ResearchNote, ResearchNoteKind, ResearchSummary
from safent_ads.packages.infrastructure.package_codec import (
    decode_budget,
    decode_plan,
    decode_publish_as,
    decode_rationale,
    decode_research,
    encode_budget,
    encode_plan,
    encode_publish_as,
    encode_rationale,
    encode_research,
)

from ..domain.conftest import (
    google_ad_set,
    google_campaign,
    meta_ad_set,
    meta_campaign,
    meta_publish_as,
    package_budget,
    package_rationale,
)


class TestPlanRoundTrip:
    def test_meta_plan_round_trips_including_preview_key(self) -> None:
        campaign = meta_campaign()
        ad_set = meta_ad_set()

        raw = encode_plan(campaign, (ad_set,))
        decoded_campaign, decoded_ad_sets = decode_plan(raw)

        assert decoded_campaign == campaign
        assert decoded_ad_sets == (ad_set,)
        assert decoded_ad_sets[0].ads[0].creative.preview_key == "preview-key-1"  # type: ignore[union-attr]

    def test_google_plan_round_trips(self) -> None:
        campaign = google_campaign()
        ad_set = google_ad_set()

        raw = encode_plan(campaign, (ad_set,))
        decoded_campaign, decoded_ad_sets = decode_plan(raw)

        assert decoded_campaign == campaign
        assert decoded_ad_sets == (ad_set,)


class TestBudgetRationaleResearchPublishAs:
    def test_budget_round_trips(self) -> None:
        budget = package_budget()

        assert decode_budget(encode_budget(budget)) == budget

    def test_rationale_round_trips(self) -> None:
        rationale = package_rationale()

        assert decode_rationale(encode_rationale(rationale)) == rationale

    def test_research_none_round_trips_to_none(self) -> None:
        assert decode_research(encode_research(None)) is None

    def test_research_round_trips(self) -> None:
        research = ResearchSummary(
            internal=(
                ResearchNote(
                    kind=ResearchNoteKind.CRM,
                    summary="El 40% de las reservas llegan fuera de horario",
                    observed_at=datetime(2026, 9, 10, 9, 0, tzinfo=UTC),
                ),
            ),
            external=(
                ResearchNote(
                    kind=ResearchNoteKind.WEB,
                    summary="La competencia ofrece cita online 24h",
                    url="https://example.com/blog",
                    observed_at=datetime(2026, 9, 12, 9, 0, tzinfo=UTC),
                ),
            ),
        )

        assert decode_research(encode_research(research)) == research

    def test_publish_as_round_trips(self) -> None:
        publish_as = meta_publish_as()

        assert decode_publish_as(encode_publish_as(publish_as)) == publish_as

    def test_publish_as_none_round_trips_to_none(self) -> None:
        assert decode_publish_as(encode_publish_as(None)) is None

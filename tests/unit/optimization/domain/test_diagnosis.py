"""`diagnose()` (profitability-engine.md §5): un caso por nodo, precedencia
(tracking roto + CTR bajo -> gana tracking) y el nodo 6 nunca propone puja."""

from __future__ import annotations

import dataclasses

from safent_ads.optimization.domain.diagnosis import (
    DiagnosisAction,
    DiagnosisNodeName,
    EntityDiagnosisMetrics,
    diagnose,
)

_CLEAN = EntityDiagnosisMetrics(
    unattributed_share=0.10,
    delta_hat=1.0,
    utm_valid=True,
    bridge_has_recent_events_24h=True,
    is_stale=False,
    is_suspended=False,
    is_drifted=False,
    is_learning=False,
    lost_is_budget_pct=0.05,
    lost_is_rank_pct=0.05,
    cpm_change_vs_14d_pct=0.0,
    ctr_7d_vs_median90d_ratio=1.0,
    quality_score_below_average=False,
    click_to_lead_vs_median90d_ratio=1.0,
    lead_to_business_conversion_vs_offering_median_ratio=1.0,
    frequency=1.5,
    is_retargeting_audience=False,
    ctr_declining=False,
    cpm_rising=False,
    cohort_maturity=0.90,
    projected_cpe_meets_target=False,
    within_seasonality_band=False,
)


def _with(**overrides: object) -> EntityDiagnosisMetrics:
    return dataclasses.replace(_CLEAN, **overrides)


class TestNoIssueHolds:
    def test_clean_entity_falls_through_to_hold(self) -> None:
        path = diagnose(_CLEAN)
        assert path.first_match is None
        assert path.action is DiagnosisAction.HOLD


class TestNode1Measurement:
    def test_unattributed_share_freezes_buy(self) -> None:
        path = diagnose(_with(unattributed_share=0.50))
        assert path.first_match is not None
        assert path.first_match.name is DiagnosisNodeName.MEASUREMENT
        assert path.action is DiagnosisAction.FREEZE_BUY_AND_FIX

    def test_delta_hat_outside_sanity_band_freezes_buy(self) -> None:
        path = diagnose(_with(delta_hat=2.0))
        assert path.first_match is not None
        assert path.first_match.name is DiagnosisNodeName.MEASUREMENT

    def test_broken_utm_freezes_buy(self) -> None:
        path = diagnose(_with(utm_valid=False))
        assert path.first_match is not None
        assert path.first_match.name is DiagnosisNodeName.MEASUREMENT


class TestNode2FreshnessAndState:
    def test_learning_entity_holds(self) -> None:
        path = diagnose(_with(is_learning=True))
        assert path.first_match is not None
        assert path.first_match.name is DiagnosisNodeName.FRESHNESS_AND_STATE
        assert path.action is DiagnosisAction.HOLD


class TestNode3VolumeAndAuction:
    def test_lost_impression_share_by_budget_is_buy_candidate(self) -> None:
        path = diagnose(_with(lost_is_budget_pct=0.30))
        assert path.first_match is not None
        assert path.first_match.name is DiagnosisNodeName.VOLUME_AND_AUCTION
        assert path.action is DiagnosisAction.BUY_CANDIDATE_FIX_BID_OR_QUALITY


class TestNode4Relevance:
    def test_low_ctr_vs_median_flags_creative(self) -> None:
        path = diagnose(_with(ctr_7d_vs_median90d_ratio=0.5))
        assert path.first_match is not None
        assert path.first_match.name is DiagnosisNodeName.RELEVANCE
        assert path.action is DiagnosisAction.FIX_CREATIVE_OR_KEYWORDS


class TestNode5Landing:
    def test_low_click_to_lead_with_normal_ctr_notifies(self) -> None:
        path = diagnose(_with(click_to_lead_vs_median90d_ratio=0.5))
        assert path.first_match is not None
        assert path.first_match.name is DiagnosisNodeName.LANDING
        assert path.action is DiagnosisAction.NOTIFY_LANDING_OUT_OF_SCOPE


class TestNode6OfferAndPriceNeverTouchesBid:
    def test_low_lead_to_business_conversion_reports_to_owner(self) -> None:
        path = diagnose(_with(lead_to_business_conversion_vs_offering_median_ratio=0.5))
        assert path.first_match is not None
        assert path.first_match.name is DiagnosisNodeName.OFFER_AND_PRICE
        assert path.action is DiagnosisAction.REPORT_TO_OWNER_NEVER_TOUCH_BID

    def test_action_is_never_a_bid_change_across_all_nodes(self) -> None:
        for node in diagnose(_with(lead_to_business_conversion_vs_offering_median_ratio=0.5)).nodes:
            if node.action is not None:
                assert node.action is not DiagnosisAction.BUY_CANDIDATE_FIX_BID_OR_QUALITY or (
                    node.name is DiagnosisNodeName.VOLUME_AND_AUCTION
                )


class TestNode7Saturation:
    def test_high_frequency_with_declining_ctr_and_rising_cpm_expands_audience(self) -> None:
        path = diagnose(
            _with(frequency=4.0, ctr_declining=True, cpm_rising=True, is_retargeting_audience=False)
        )
        assert path.first_match is not None
        assert path.first_match.name is DiagnosisNodeName.SATURATION


class TestNode8AttributionLagFalseNegative:
    def test_immature_cohort_meeting_target_is_never_acted_on(self) -> None:
        path = diagnose(_with(cohort_maturity=0.30, projected_cpe_meets_target=True))
        assert path.first_match is not None
        assert path.first_match.name is DiagnosisNodeName.ATTRIBUTION_LAG
        assert path.action is DiagnosisAction.FALSE_NEGATIVE_DO_NOT_ACT


class TestNode9Seasonality:
    def test_within_seasonal_band_holds(self) -> None:
        path = diagnose(_with(within_seasonality_band=True))
        assert path.first_match is not None
        assert path.first_match.name is DiagnosisNodeName.SEASONALITY


class TestPrecedence:
    def test_tracking_beats_ctr(self) -> None:
        path = diagnose(_with(unattributed_share=0.50, ctr_7d_vs_median90d_ratio=0.5))
        assert path.first_match is not None
        assert path.first_match.name is DiagnosisNodeName.MEASUREMENT
        measurement_node = next(n for n in path.nodes if n.name is DiagnosisNodeName.MEASUREMENT)
        relevance_node = next(n for n in path.nodes if n.name is DiagnosisNodeName.RELEVANCE)
        assert measurement_node.matched is True
        assert relevance_node.matched is True  # evaluado igualmente: camino completo
        assert path.first_match.order < relevance_node.order

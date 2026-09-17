"""`PlatformDivergence` (profitability-engine.md §2): `delta_hat` con poco
volumen tiende a 1,0 (shrinkage); un salto > 30% es anomalia de medicion."""

from __future__ import annotations

import pytest

from safent_ads.economics.domain.platform_divergence import (
    PlatformDivergence,
    exceeds_unattributed_share_threshold,
)


class TestShrinkage:
    def test_low_volume_shrinks_towards_one(self) -> None:
        # 1 conversion CRM vs 2 en plataforma: crudo seria 0.5, muy lejos de 1.
        divergence = PlatformDivergence.compute(crm_conversions=1, platform_conversions=2)
        assert abs(divergence.value - 1.0) < abs(1 / 2 - 1.0)

    def test_high_volume_converges_to_raw_ratio(self) -> None:
        divergence = PlatformDivergence.compute(crm_conversions=900, platform_conversions=1000)
        assert divergence.value == pytest.approx(910 / 1010, abs=1e-6)


class TestSanityBand:
    def test_within_band_is_not_flagged(self) -> None:
        divergence = PlatformDivergence.compute(crm_conversions=100, platform_conversions=100)
        assert divergence.is_outside_sanity_band is False

    def test_outside_band_flagged(self) -> None:
        divergence = PlatformDivergence.compute(crm_conversions=10, platform_conversions=100)
        assert divergence.is_outside_sanity_band is True


class TestJumpAnomaly:
    def test_jump_over_threshold_flagged(self) -> None:
        previous = PlatformDivergence.compute(crm_conversions=100, platform_conversions=100)
        current = PlatformDivergence.compute(crm_conversions=140, platform_conversions=100)
        assert current.is_jump_anomaly(previous) is True

    def test_stable_ratio_not_flagged(self) -> None:
        previous = PlatformDivergence.compute(crm_conversions=100, platform_conversions=100)
        current = PlatformDivergence.compute(crm_conversions=105, platform_conversions=100)
        assert current.is_jump_anomaly(previous) is False


class TestUnattributedShare:
    def test_above_threshold_blocks_raises(self) -> None:
        assert exceeds_unattributed_share_threshold(0.40) is True

    def test_below_threshold_allows_raises(self) -> None:
        assert exceeds_unattributed_share_threshold(0.20) is False

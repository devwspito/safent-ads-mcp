"""`MmmChannelBridge` (profitability-engine.md §3b): campana con >= 5% del
gasto de su plataforma es su propio canal, el resto cae en `residual_<platform>`;
agregacion diaria->semanal->diaria conserva el total."""

from __future__ import annotations

from datetime import date
from decimal import Decimal

from safent_ads.optimization.domain.bridge import CampaignDailySpend, MmmChannelBridge
from safent_ads.shared.ids import EntityLevel, EntityRef, PlatformCode

_BIG = EntityRef(platform=PlatformCode.GOOGLE, level=EntityLevel.CAMPAIGN, external_id="big")
_SMALL_1 = EntityRef(platform=PlatformCode.GOOGLE, level=EntityLevel.CAMPAIGN, external_id="small1")
_SMALL_2 = EntityRef(platform=PlatformCode.GOOGLE, level=EntityLevel.CAMPAIGN, external_id="small2")


def _records() -> tuple[CampaignDailySpend, ...]:
    day = date(2026, 1, 5)
    return (
        CampaignDailySpend(entity_ref=_BIG, platform="google", day=day, spend=Decimal("950")),
        CampaignDailySpend(entity_ref=_SMALL_1, platform="google", day=day, spend=Decimal("30")),
        CampaignDailySpend(entity_ref=_SMALL_2, platform="google", day=day, spend=Decimal("20")),
    )


class TestChannelMapping:
    def test_dominant_campaign_gets_own_channel(self) -> None:
        mapping = MmmChannelBridge.build_channel_mapping(_records())
        own = [m for m in mapping if not m.is_residual]
        assert len(own) == 1
        assert own[0].campaign_refs == (_BIG,)

    def test_small_campaigns_fall_into_residual_bucket(self) -> None:
        mapping = MmmChannelBridge.build_channel_mapping(_records())
        residual = [m for m in mapping if m.is_residual]
        assert len(residual) == 1
        assert residual[0].channel_id == "residual_google"
        assert set(residual[0].campaign_refs) == {_SMALL_1, _SMALL_2}


class TestAggregationRoundTrip:
    def test_daily_to_weekly_preserves_total_spend(self) -> None:
        records = _records()
        mapping = MmmChannelBridge.build_channel_mapping(records)
        weekly = MmmChannelBridge.aggregate_daily_to_weekly(records, mapping)
        assert sum(weekly.values()) == sum(r.spend for r in records)

    def test_weekly_to_daily_preserves_total_and_uses_channel_key(self) -> None:
        week_start = date(2026, 1, 5)
        daily = MmmChannelBridge.disaggregate_weekly_to_daily(
            channel_id="residual_google",
            week_start=week_start,
            weekly_amount=Decimal("700.00"),
            historical_daily_shares=(),
        )
        assert len(daily) == 7
        assert all(key[0] == "residual_google" for key in daily)
        assert sum(daily.values()) == Decimal("700.00")

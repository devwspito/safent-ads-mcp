"""`MmmChannelBridge` (profitability-engine.md §3b, build sin base OSS):
`BudgetOptimizer` optimiza por *canal*, nosotros decidimos por *campana*.

Regla: una campana con >= 5% del gasto de su plataforma en la ventana es su
propio canal; el resto cae en `residual_<platform>`. El gasto diario se
agrega a semanal para ajustar la MMM y se devuelve a diario (reparto
proporcional al peso historico de cada dia) para proponer el cambio."""

from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass
from datetime import date, timedelta
from decimal import Decimal

from safent_ads.shared.ids import EntityRef

OWN_CHANNEL_SPEND_SHARE_THRESHOLD = 0.05
DAYS_PER_WEEK = 7
_RESIDUAL_PREFIX = "residual_"


@dataclass(frozen=True, kw_only=True, slots=True)
class CampaignDailySpend:
    entity_ref: EntityRef
    platform: str
    day: date
    spend: Decimal


@dataclass(frozen=True, kw_only=True, slots=True)
class ChannelMapping:
    """`channel_id -> EntityRef[]` (profitability-engine.md §3: 'vive en
    response_curves')."""

    channel_id: str
    platform: str
    campaign_refs: tuple[EntityRef, ...]
    is_residual: bool


def _residual_channel_id(platform: str) -> str:
    return f"{_RESIDUAL_PREFIX}{platform}"


class MmmChannelBridge:
    @staticmethod
    def build_channel_mapping(
        records: tuple[CampaignDailySpend, ...],
    ) -> tuple[ChannelMapping, ...]:
        totals_by_platform: dict[str, Decimal] = defaultdict(lambda: Decimal("0"))
        totals_by_campaign: dict[tuple[str, EntityRef], Decimal] = defaultdict(lambda: Decimal("0"))
        for record in records:
            totals_by_platform[record.platform] += record.spend
            totals_by_campaign[(record.platform, record.entity_ref)] += record.spend

        own_channels: list[ChannelMapping] = []
        residual_by_platform: dict[str, list[EntityRef]] = defaultdict(list)
        for (platform, entity_ref), campaign_spend in totals_by_campaign.items():
            platform_total = totals_by_platform[platform]
            share = float(campaign_spend / platform_total) if platform_total > 0 else 0.0
            if share >= OWN_CHANNEL_SPEND_SHARE_THRESHOLD:
                own_channels.append(
                    ChannelMapping(
                        channel_id=str(entity_ref),
                        platform=platform,
                        campaign_refs=(entity_ref,),
                        is_residual=False,
                    )
                )
            else:
                residual_by_platform[platform].append(entity_ref)

        residual_channels = [
            ChannelMapping(
                channel_id=_residual_channel_id(platform),
                platform=platform,
                campaign_refs=tuple(refs),
                is_residual=True,
            )
            for platform, refs in residual_by_platform.items()
            if refs
        ]
        return tuple(own_channels) + tuple(residual_channels)

    @staticmethod
    def aggregate_daily_to_weekly(
        records: tuple[CampaignDailySpend, ...], mapping: tuple[ChannelMapping, ...]
    ) -> dict[tuple[str, date], Decimal]:
        """Suma el gasto diario por canal dentro de cada semana ISO (lunes de
        inicio). Clave: `(channel_id, week_start)`."""
        channel_by_campaign = {
            (m.platform, ref): m.channel_id for m in mapping for ref in m.campaign_refs
        }
        weekly: dict[tuple[str, date], Decimal] = defaultdict(lambda: Decimal("0"))
        for record in records:
            channel_id = channel_by_campaign.get((record.platform, record.entity_ref))
            if channel_id is None:
                continue
            week_start = record.day - timedelta(days=record.day.weekday())
            weekly[(channel_id, week_start)] += record.spend
        return dict(weekly)

    @staticmethod
    def disaggregate_weekly_to_daily(
        *,
        channel_id: str,
        week_start: date,
        weekly_amount: Decimal,
        historical_daily_shares: tuple[float, ...],
    ) -> dict[tuple[str, date], Decimal]:
        """Reparte `weekly_amount` en 7 dias proporcionalmente a
        `historical_daily_shares` (`DAYS_PER_WEEK` pesos que suman 1,0;
        reparto equitativo si no hay historico). Clave: `(channel_id, day)`,
        simetrica a `aggregate_daily_to_weekly`."""
        shares = (
            historical_daily_shares
            if historical_daily_shares
            else (1 / DAYS_PER_WEEK,) * DAYS_PER_WEEK
        )
        if len(shares) != DAYS_PER_WEEK:
            raise ValueError(
                f"historical_daily_shares debe tener {DAYS_PER_WEEK} valores, tiene {len(shares)}"
            )
        total_share = sum(shares)
        return {
            (channel_id, week_start + timedelta(days=offset)): (
                weekly_amount * Decimal(str(share / total_share))
            ).quantize(Decimal("0.01"))
            for offset, share in enumerate(shares)
        }

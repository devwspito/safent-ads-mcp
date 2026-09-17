"""`SqlTopPerformingAdsReadPort` (R8, historia 24): parte (b) de la revision
previa obligatoria. El filtro por `business_id` y el `ORDER BY ... LIMIT`
corren en Postgres de verdad -- una entidad de otro negocio nunca debe
poder colarse ni siquiera si su metrica de ranking es mayor."""

from __future__ import annotations

import uuid
from datetime import UTC, date, datetime

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from safent_ads.mcp.application.company_read_ports import (
    TopAdsLevel,
    TopAdsMetric,
    TopAdsWindowPreset,
)
from safent_ads.mcp.infrastructure.sql_top_ads_read_port import SqlTopPerformingAdsReadPort
from safent_ads.shared.clock import FixedClock

pytestmark = pytest.mark.integration

_NOW = datetime(2026, 9, 14, 12, tzinfo=UTC)
_TODAY = _NOW.date()
# 30D: [2026-08-16, 2026-09-14]. Previa: [2026-07-17, 2026-08-15].
_CUR_STAT_DATE = date(2026, 9, 10)
_PREV_STAT_DATE = date(2026, 8, 1)
_PARTITION_MONTHS = (date(2026, 7, 1), date(2026, 8, 1), date(2026, 9, 1))


async def _ensure_partitions(session: AsyncSession) -> None:
    for month in _PARTITION_MONTHS:
        await session.execute(
            text("SELECT metrics_daily_ensure_partition(:month)"), {"month": month}
        )


async def _seed_business(session: AsyncSession) -> uuid.UUID:
    business_id = uuid.uuid4()
    await session.execute(
        text(
            """
            INSERT INTO businesses (id, slug, name, timezone, reference_currency)
            VALUES (:id, :slug, 'Top ads contrato', 'Europe/Madrid', 'EUR')
            """
        ),
        {"id": business_id, "slug": f"topads-{business_id.hex[:12]}"},
    )
    return business_id


async def _seed_account(session: AsyncSession, business_id: uuid.UUID) -> uuid.UUID:
    credential_id = uuid.uuid4()
    account_id = uuid.uuid4()
    suffix = uuid.uuid4().hex[:12]
    await session.execute(
        text("INSERT INTO credential_refs (id, platform, alias) VALUES (:id, 'meta', :alias)"),
        {"id": credential_id, "alias": f"alias-{suffix}"},
    )
    await session.execute(
        text(
            """
            INSERT INTO platform_accounts (id, business_id, platform, external_account_id,
                                            currency, timezone, api_tier, credential_ref_id,
                                            status)
            VALUES (:id, :business_id, 'meta', :external_account_id, 'EUR', 'Europe/Madrid',
                    'meta_full', :credential_ref_id, 'ACTIVE')
            """
        ),
        {
            "id": account_id,
            "business_id": business_id,
            "external_account_id": f"act_{suffix}",
            "credential_ref_id": credential_id,
        },
    )
    return account_id


async def _seed_campaign(
    session: AsyncSession, business_id: uuid.UUID, account_id: uuid.UUID, *, name: str
) -> str:
    external_id = uuid.uuid4().hex[:16]
    await session.execute(
        text(
            """
            INSERT INTO ad_entities (business_id, platform_account_id, platform, level,
                                     external_id, name, status, platform_state_hash)
            VALUES (:business_id, :account_id, 'meta', 'campaign', :external_id, :name,
                    'ACTIVE', :state_hash)
            """
        ),
        {
            "business_id": business_id,
            "account_id": account_id,
            "external_id": external_id,
            "name": name,
            "state_hash": "b" * 64,
        },
    )
    return f"meta:campaign:{external_id}"


async def _seed_metrics_day(
    session: AsyncSession,
    business_id: uuid.UUID,
    account_id: uuid.UUID,
    entity_ref: str,
    *,
    stat_date: date,
    spend: int,
    conversions_lead: int,
) -> None:
    await session.execute(
        text(
            """
            INSERT INTO metrics_daily (business_id, entity_ref, entity_level,
                                       platform_account_id, stat_date, account_timezone,
                                       currency, spend, impressions, clicks, conversions_lead)
            VALUES (:business_id, :entity_ref, 'campaign', :account_id, :stat_date,
                    'Europe/Madrid', 'EUR', :spend, 1000, 100, :conversions_lead)
            """
        ),
        {
            "business_id": business_id,
            "entity_ref": entity_ref,
            "account_id": account_id,
            "stat_date": stat_date,
            "spend": spend,
            "conversions_lead": conversions_lead,
        },
    )


async def test_ordena_por_metrica_y_nunca_devuelve_entidades_de_otro_negocio(
    mcp_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    async with mcp_session_factory() as session:
        await _ensure_partitions(session)
        business_id = await _seed_business(session)
        account_id = await _seed_account(session, business_id)
        top_ref = await _seed_campaign(session, business_id, account_id, name="Top")
        low_ref = await _seed_campaign(session, business_id, account_id, name="Low")

        await _seed_metrics_day(
            session, business_id, account_id, top_ref,
            stat_date=_CUR_STAT_DATE, spend=10_000, conversions_lead=20,
        )
        await _seed_metrics_day(
            session, business_id, account_id, top_ref,
            stat_date=_PREV_STAT_DATE, spend=8_000, conversions_lead=10,
        )
        await _seed_metrics_day(
            session, business_id, account_id, low_ref,
            stat_date=_CUR_STAT_DATE, spend=5_000, conversions_lead=5,
        )
        await _seed_metrics_day(
            session, business_id, account_id, low_ref,
            stat_date=_PREV_STAT_DATE, spend=5_000, conversions_lead=5,
        )

        # Otro negocio con conversiones absurdamente altas: nunca debe salir.
        other_business_id = await _seed_business(session)
        other_account_id = await _seed_account(session, other_business_id)
        other_ref = await _seed_campaign(
            session, other_business_id, other_account_id, name="Ajena"
        )
        await _seed_metrics_day(
            session, other_business_id, other_account_id, other_ref,
            stat_date=_CUR_STAT_DATE, spend=1, conversions_lead=1_000,
        )
        await session.commit()

    port = SqlTopPerformingAdsReadPort(mcp_session_factory, FixedClock(_NOW))

    result = await port.list_top_performing_ads(
        str(business_id),
        window=TopAdsWindowPreset.THIRTY_DAYS,
        level=TopAdsLevel.CAMPAIGN,
        metric=TopAdsMetric.CONVERSIONS,
        limit=10,
    )

    assert [ad.entity_ref for ad in result.ads] == [top_ref, low_ref]
    assert all(ad.entity_ref != other_ref for ad in result.ads)

    top = result.ads[0]
    assert top.current.conversions == 20
    assert top.current.spend_minor == 10_000
    assert top.previous.conversions == 10
    assert top.delta_metric_pct == pytest.approx(100.0)

    low = result.ads[1]
    assert low.delta_metric_pct == pytest.approx(0.0)


async def test_metrica_cpa_ordena_de_forma_ascendente(
    mcp_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    async with mcp_session_factory() as session:
        await _ensure_partitions(session)
        business_id = await _seed_business(session)
        account_id = await _seed_account(session, business_id)
        cheap_ref = await _seed_campaign(session, business_id, account_id, name="Barato")
        expensive_ref = await _seed_campaign(session, business_id, account_id, name="Caro")

        await _seed_metrics_day(
            session, business_id, account_id, cheap_ref,
            stat_date=_CUR_STAT_DATE, spend=1_000, conversions_lead=10,
        )
        await _seed_metrics_day(
            session, business_id, account_id, expensive_ref,
            stat_date=_CUR_STAT_DATE, spend=10_000, conversions_lead=10,
        )
        await session.commit()

    port = SqlTopPerformingAdsReadPort(mcp_session_factory, FixedClock(_NOW))

    result = await port.list_top_performing_ads(
        str(business_id),
        window=TopAdsWindowPreset.THIRTY_DAYS,
        level=TopAdsLevel.CAMPAIGN,
        metric=TopAdsMetric.CPA,
        limit=10,
    )

    assert [ad.entity_ref for ad in result.ads] == [cheap_ref, expensive_ref]


async def test_limit_nunca_supera_el_tope_de_25(
    mcp_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    async with mcp_session_factory() as session:
        await _ensure_partitions(session)
        business_id = await _seed_business(session)
        account_id = await _seed_account(session, business_id)
        for index in range(30):
            ref = await _seed_campaign(
                session, business_id, account_id, name=f"Campana {index}"
            )
            await _seed_metrics_day(
                session, business_id, account_id, ref,
                stat_date=_CUR_STAT_DATE, spend=100, conversions_lead=index,
            )
        await session.commit()

    port = SqlTopPerformingAdsReadPort(mcp_session_factory, FixedClock(_NOW))

    result = await port.list_top_performing_ads(
        str(business_id),
        window=TopAdsWindowPreset.THIRTY_DAYS,
        level=TopAdsLevel.CAMPAIGN,
        metric=TopAdsMetric.CONVERSIONS,
        limit=999,
    )

    assert len(result.ads) == 25

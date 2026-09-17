"""`Sql*Repository` de `creative` contra Postgres real (0021_creative_review):
los dobles en memoria no modelan el `CAST(... AS JSONB)`, el `CHECK` de
`state`/`policy_verdict`/`generation_status`, ni el `UNIQUE (brief_hash,
variant_index)` de `creative_jobs` (T098) -- mismo criterio que
`tests/integration/brand/test_sql_brand_kit_repository.py`."""

from __future__ import annotations

import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from safent_ads.creative.domain.creative_job import CreativeJob, CreativeJobIdempotencyKey
from safent_ads.creative.domain.enums import PolicySeverity, PolicyVerdictResult
from safent_ads.creative.domain.identifiers import BriefId, JobId
from safent_ads.creative.domain.policy import PolicyFinding, PolicyVerdict
from safent_ads.creative.infrastructure.sql_repositories import (
    SqlCreativeAssetRepository,
    SqlCreativeBriefRepository,
    SqlCreativeJobRepository,
)
from safent_ads.shared.ids import BusinessId
from tests.conftest import BusinessFactory
from tests.unit.creative.domain.factories import make_ad_copy, make_brief
from tests.unit.creative.infrastructure.fakes import make_creative_asset

pytestmark = pytest.mark.integration


async def test_round_trips_a_creative_brief(
    db_session: AsyncSession, business_factory: BusinessFactory
) -> None:
    business_id = BusinessId.parse(str(await business_factory.create()))
    brief = make_brief(business_id=business_id)
    brief_id = BriefId.new()
    repository = SqlCreativeBriefRepository(db_session)

    await repository.add(brief_id, brief)
    reloaded = await repository.get(brief_id)
    listed = await repository.list_for_business(business_id)

    assert reloaded is not None
    assert reloaded.business_id == business_id
    assert reloaded.hook == brief.hook
    assert [s.description for s in reloaded.shots] == [s.description for s in brief.shots]
    assert reloaded.brand_kit.primary_color_hex == brief.brand_kit.primary_color_hex
    assert reloaded.brand_kit.logo_asset_id == brief.brand_kit.logo_asset_id
    assert [bid for bid, _ in listed] == [brief_id]


async def test_get_brief_returns_none_when_missing(db_session: AsyncSession) -> None:
    repository = SqlCreativeBriefRepository(db_session)

    assert await repository.get(BriefId.new()) is None


async def test_round_trips_a_creative_asset_through_full_review_lifecycle(
    db_session: AsyncSession, business_factory: BusinessFactory
) -> None:
    business_id = BusinessId.parse(str(await business_factory.create()))
    briefs = SqlCreativeBriefRepository(db_session)
    brief_id = BriefId.new()
    brief = make_brief(business_id=business_id)
    await briefs.add(brief_id, brief)

    assets = SqlCreativeAssetRepository(db_session)
    asset = make_creative_asset(business_id=business_id, brief_id=brief_id)
    await assets.add(asset)

    reloaded = await assets.get(asset.asset_id)
    assert reloaded is not None
    assert reloaded.business_id == business_id
    assert reloaded.checksum == asset.checksum
    assert reloaded.state == asset.state
    assert reloaded.review_state == asset.review_state

    reloaded.mark_ready(PolicyVerdict(verdict=PolicyVerdictResult.PASS_, findings=()))
    await assets.update(reloaded)

    reread = await assets.get(asset.asset_id)
    assert reread is not None
    assert reread.state.value == "ready"
    assert reread.review_state.value == "pending"
    assert reread.policy_verdict is not None
    assert reread.policy_verdict.verdict == PolicyVerdictResult.PASS_

    listed = await assets.list_for_business(business_id)
    assert [a.asset_id for a in listed] == [asset.asset_id]


async def test_round_trips_policy_findings_and_ad_copy(
    db_session: AsyncSession, business_factory: BusinessFactory
) -> None:
    business_id = BusinessId.parse(str(await business_factory.create()))
    briefs = SqlCreativeBriefRepository(db_session)
    brief_id = BriefId.new()
    await briefs.add(brief_id, make_brief(business_id=business_id))

    assets = SqlCreativeAssetRepository(db_session)
    ad_copy = make_ad_copy()
    asset = make_creative_asset(business_id=business_id, ad_copy=ad_copy, brief_id=brief_id)
    await assets.add(asset)

    reloaded = await assets.get(asset.asset_id)
    assert reloaded is not None
    reloaded.mark_ready(
        PolicyVerdict(
            verdict=PolicyVerdictResult.WARN,
            findings=(
                PolicyFinding(
                    code="CAPS_ABUSE", severity=PolicySeverity.WARN, human_message="x"
                ),
            ),
        )
    )
    await assets.update(reloaded)

    reread = await assets.get(asset.asset_id)
    assert reread is not None
    assert reread.ad_copy is not None
    assert reread.ad_copy.headline == ad_copy.headline
    assert reread.policy_verdict is not None
    assert reread.policy_verdict.verdict == PolicyVerdictResult.WARN
    assert reread.policy_verdict.findings[0].code == "CAPS_ABUSE"


async def test_round_trips_a_creative_job_and_enforces_idempotency_key(
    db_session: AsyncSession, business_factory: BusinessFactory
) -> None:
    business_id = BusinessId.parse(str(await business_factory.create()))
    briefs = SqlCreativeBriefRepository(db_session)
    brief_id = BriefId.new()
    await briefs.add(brief_id, make_brief(business_id=business_id))

    jobs = SqlCreativeJobRepository(db_session)
    key = CreativeJobIdempotencyKey(brief_hash="a" * 64, variant_index=0)
    job = CreativeJob(
        job_id=JobId.new(), business_id=business_id, brief_id=brief_id, idempotency_key=key
    )
    await jobs.add(job)

    reloaded = await jobs.get(job.job_id)
    by_key = await jobs.get_by_idempotency_key(key)
    assert reloaded is not None
    assert by_key is not None
    assert reloaded.job_id == job.job_id == by_key.job_id
    assert reloaded.state.value == "queued"

    job.start_rendering()
    job.start_composing()
    job.start_checking()
    await jobs.update(job)

    reread = await jobs.get(job.job_id)
    assert reread is not None
    assert reread.state.value == "checking"


async def test_get_job_returns_none_when_missing(db_session: AsyncSession) -> None:
    repository = SqlCreativeJobRepository(db_session)

    assert await repository.get(JobId.new()) is None
    assert (
        await repository.get_by_idempotency_key(
            CreativeJobIdempotencyKey(brief_hash="b" * 64, variant_index=0)
        )
        is None
    )

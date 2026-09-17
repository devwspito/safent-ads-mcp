"""Contrato de `LeadAttributionRepository` (T145): identico para el doble en
memoria y para `SqlLeadAttributionRepository` sobre `lead_attributions`
(0005_catalog_crm)."""

from __future__ import annotations

import uuid
from datetime import UTC, date, datetime

from safent_ads.crm.domain.conversion_kind import ConversionKind
from safent_ads.shared.ids import EntityLevel, EntityRef, PlatformCode
from tests.contracts.crm.conftest import RepositoryFixture, build_attribution, new_business_id


async def test_saved_attribution_round_trips(repositories: RepositoryFixture) -> None:
    business_id = new_business_id()
    await repositories.given_business(business_id)
    attribution = build_attribution(business_id)

    await repositories.attributions.save(attribution)
    count = await repositories.attributions.count_by_kind_in_window(
        business_id=business_id,
        conversion_kind=ConversionKind.BUSINESS_CONVERSION,
        window_start=attribution.occurred_at.date(),
        window_end=attribution.occurred_at.date().replace(day=attribution.occurred_at.day + 1),
    )
    assert count == 1


async def test_reingesting_the_same_conversion_never_duplicates(
    repositories: RepositoryFixture,
) -> None:
    business_id = new_business_id()
    await repositories.given_business(business_id)
    attribution = build_attribution(business_id)

    await repositories.attributions.save(attribution)
    await repositories.attributions.save(attribution)

    count = await repositories.attributions.count_by_kind_in_window(
        business_id=business_id,
        conversion_kind=ConversionKind.BUSINESS_CONVERSION,
        window_start=attribution.occurred_at.date(),
        window_end=attribution.occurred_at.date().replace(day=attribution.occurred_at.day + 1),
    )
    assert count == 1


async def test_save_returns_true_when_new_and_false_when_duplicate(
    repositories: RepositoryFixture,
) -> None:
    business_id = new_business_id()
    await repositories.given_business(business_id)
    attribution = build_attribution(business_id)

    first = await repositories.attributions.save(attribution)
    second = await repositories.attributions.save(attribution)

    assert first is True
    assert second is False


async def test_count_by_kind_only_counts_its_own_business(
    repositories: RepositoryFixture,
) -> None:
    mine = new_business_id()
    other = new_business_id()
    await repositories.given_business(mine)
    await repositories.given_business(other)
    await repositories.attributions.save(build_attribution(mine, raw_identity="a@example.com"))
    await repositories.attributions.save(build_attribution(other, raw_identity="b@example.com"))

    count = await repositories.attributions.count_by_kind_in_window(
        business_id=mine,
        conversion_kind=ConversionKind.BUSINESS_CONVERSION,
        window_start=datetime(2026, 1, 1, tzinfo=UTC).date(),
        window_end=datetime(2026, 12, 31, tzinfo=UTC).date(),
    )
    assert count == 1


async def test_find_for_calendar_events_returns_only_matching_events(
    repositories: RepositoryFixture,
) -> None:
    business_id = new_business_id()
    await repositories.given_business(business_id)
    matching_event = str(uuid.uuid4())
    other_event = str(uuid.uuid4())
    await repositories.given_calendar_event(business_id, matching_event)
    await repositories.given_calendar_event(business_id, other_event)
    await repositories.attributions.save(
        build_attribution(business_id, calendar_event_id=matching_event, raw_identity="a@x.com")
    )
    await repositories.attributions.save(
        build_attribution(business_id, calendar_event_id=other_event, raw_identity="b@x.com")
    )

    found = await repositories.attributions.find_for_calendar_events(
        business_id=business_id, calendar_event_ids=[matching_event]
    )

    assert len(found) == 1
    assert found[0].calendar_event_id == matching_event


async def test_find_for_calendar_events_empty_list_returns_nothing(
    repositories: RepositoryFixture,
) -> None:
    business_id = new_business_id()
    await repositories.given_business(business_id)

    found = await repositories.attributions.find_for_calendar_events(
        business_id=business_id, calendar_event_ids=[]
    )

    assert found == []


async def test_last_event_at_is_none_without_any_conversion(
    repositories: RepositoryFixture,
) -> None:
    business_id = new_business_id()
    await repositories.given_business(business_id)

    last = await repositories.attributions.last_event_at(
        business_id=business_id, conversion_kind=ConversionKind.WHATSAPP
    )

    assert last is None


async def test_last_event_at_returns_the_most_recent_occurrence(
    repositories: RepositoryFixture,
) -> None:
    business_id = new_business_id()
    await repositories.given_business(business_id)
    earlier = datetime(2026, 3, 1, 9, 0, tzinfo=UTC)
    later = datetime(2026, 3, 5, 9, 0, tzinfo=UTC)
    await repositories.attributions.save(
        build_attribution(
            business_id,
            conversion_kind=ConversionKind.WHATSAPP,
            occurred_at=earlier,
            raw_identity="a@x.com",
        )
    )
    await repositories.attributions.save(
        build_attribution(
            business_id,
            conversion_kind=ConversionKind.WHATSAPP,
            occurred_at=later,
            raw_identity="b@x.com",
        )
    )

    last = await repositories.attributions.last_event_at(
        business_id=business_id, conversion_kind=ConversionKind.WHATSAPP
    )

    assert last == later


async def test_list_distinct_entity_refs_in_window_only_counts_attributed_rows(
    repositories: RepositoryFixture,
) -> None:
    entity_ref = EntityRef(
        platform=PlatformCode.GOOGLE, level=EntityLevel.CAMPAIGN, external_id="c1"
    )
    business_id = await repositories.given_entity(entity_ref)
    in_window = datetime(2026, 3, 5, tzinfo=UTC)
    out_of_window = datetime(2026, 4, 1, tzinfo=UTC)
    await repositories.attributions.save(
        build_attribution(
            business_id, entity_ref=entity_ref, occurred_at=in_window, raw_identity="a@x.com"
        )
    )
    await repositories.attributions.save(
        build_attribution(business_id, occurred_at=in_window, raw_identity="b@x.com")  # AGGREGATE
    )
    await repositories.attributions.save(
        build_attribution(
            business_id, entity_ref=entity_ref, occurred_at=out_of_window, raw_identity="c@x.com"
        )
    )

    found = await repositories.attributions.list_distinct_entity_refs_in_window(
        business_id=business_id, window_start=date(2026, 3, 1), window_end=date(2026, 3, 31)
    )

    assert found == [str(entity_ref)]

"""`build_lag_observations` (T156): traduce `LeadAttribution` en
`LagObservation` por cohorte diaria de lead, censurada por la derecha."""

from __future__ import annotations

from datetime import UTC, date, datetime

from safent_ads.crm.domain.conversion_kind import ConversionKind
from safent_ads.crm.domain.hashed_identity import HashedIdentity
from safent_ads.crm.domain.lead_attribution import (
    AttributionRung,
    LeadAttribution,
    LeadAttributionId,
)
from safent_ads.economics.application.cohort_builder import build_lag_observations
from safent_ads.shared.ids import BusinessId, EntityLevel, EntityRef, PlatformCode

_BUSINESS_ID = BusinessId.new()
_GOOGLE_CAMPAIGN = EntityRef(
    platform=PlatformCode.GOOGLE, level=EntityLevel.CAMPAIGN, external_id="c1"
)
_META_CAMPAIGN = EntityRef(platform=PlatformCode.META, level=EntityLevel.CAMPAIGN, external_id="c2")


def _identity(raw: str) -> HashedIdentity:
    return HashedIdentity.compute(business_id=_BUSINESS_ID, raw_identifier=raw, salt="s")


def _row(
    *,
    raw_identity: str,
    kind: ConversionKind,
    occurred_at: datetime,
    entity_ref: EntityRef | None,
) -> LeadAttribution:
    return LeadAttribution(
        lead_attribution_id=LeadAttributionId.new(),
        business_id=_BUSINESS_ID,
        hashed_identity=_identity(raw_identity),
        entity_ref=entity_ref,
        attribution_rung=(
            AttributionRung.AGGREGATE if entity_ref is None else AttributionRung.HASHED_IDENTITY
        ),
        conversion_kind=kind,
        value_minor=100_000,
        occurred_at=occurred_at,
        observed_at=occurred_at,
    )


def test_converted_cohort_gets_a_finite_duration() -> None:
    lead = _row(
        raw_identity="a@x.com",
        kind=ConversionKind.LEAD,
        occurred_at=datetime(2026, 1, 1, tzinfo=UTC),
        entity_ref=_GOOGLE_CAMPAIGN,
    )
    conversion = _row(
        raw_identity="a@x.com",
        kind=ConversionKind.BUSINESS_CONVERSION,
        occurred_at=datetime(2026, 1, 11, tzinfo=UTC),
        entity_ref=_GOOGLE_CAMPAIGN,
    )

    observations = build_lag_observations(
        [lead, conversion], platform="google", as_of=date(2026, 2, 1)
    )

    assert len(observations) == 1
    assert observations[0].converted is True
    assert observations[0].duration_days == 10


def test_unconverted_cohort_is_censored_at_as_of() -> None:
    lead = _row(
        raw_identity="b@x.com",
        kind=ConversionKind.LEAD,
        occurred_at=datetime(2026, 1, 20, tzinfo=UTC),
        entity_ref=_GOOGLE_CAMPAIGN,
    )

    observations = build_lag_observations([lead], platform="google", as_of=date(2026, 2, 1))

    assert len(observations) == 1
    assert observations[0].converted is False
    assert observations[0].duration_days == 12


def test_lead_on_a_different_platform_is_excluded() -> None:
    lead = _row(
        raw_identity="c@x.com",
        kind=ConversionKind.LEAD,
        occurred_at=datetime(2026, 1, 1, tzinfo=UTC),
        entity_ref=_META_CAMPAIGN,
    )

    observations = build_lag_observations([lead], platform="google", as_of=date(2026, 2, 1))

    assert observations == []


def test_aggregate_lead_without_entity_ref_is_excluded() -> None:
    lead = _row(
        raw_identity="d@x.com",
        kind=ConversionKind.LEAD,
        occurred_at=datetime(2026, 1, 1, tzinfo=UTC),
        entity_ref=None,
    )

    observations = build_lag_observations([lead], platform="google", as_of=date(2026, 2, 1))

    assert observations == []


def test_conversion_can_switch_attribution_away_from_the_lead_platform() -> None:
    lead = _row(
        raw_identity="e@x.com",
        kind=ConversionKind.LEAD,
        occurred_at=datetime(2026, 1, 1, tzinfo=UTC),
        entity_ref=_GOOGLE_CAMPAIGN,
    )
    conversion = _row(
        raw_identity="e@x.com",
        kind=ConversionKind.BUSINESS_CONVERSION,
        occurred_at=datetime(2026, 1, 6, tzinfo=UTC),
        entity_ref=_META_CAMPAIGN,
    )

    observations = build_lag_observations(
        [lead, conversion], platform="google", as_of=date(2026, 2, 1)
    )

    assert len(observations) == 1
    assert observations[0].converted is True
    assert observations[0].duration_days == 5


def test_conversion_before_the_lead_is_ignored_as_noise() -> None:
    lead = _row(
        raw_identity="f@x.com",
        kind=ConversionKind.LEAD,
        occurred_at=datetime(2026, 1, 10, tzinfo=UTC),
        entity_ref=_GOOGLE_CAMPAIGN,
    )
    stale_conversion = _row(
        raw_identity="f@x.com",
        kind=ConversionKind.BUSINESS_CONVERSION,
        occurred_at=datetime(2026, 1, 5, tzinfo=UTC),
        entity_ref=_GOOGLE_CAMPAIGN,
    )

    observations = build_lag_observations(
        [lead, stale_conversion], platform="google", as_of=date(2026, 2, 1)
    )

    assert len(observations) == 1
    assert observations[0].converted is False

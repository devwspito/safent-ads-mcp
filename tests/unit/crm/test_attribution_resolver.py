"""`AttributionResolver`: escalera gclid/fbclid -> UTM -> identidad hasheada
-> agregado (plan.md §5)."""

from __future__ import annotations

from datetime import UTC, datetime

import pytest

from safent_ads.crm.domain.attribution_resolver import AttributionResolver, ConversionSignal
from safent_ads.crm.domain.conversion_kind import ConversionKind
from safent_ads.crm.domain.hashed_identity import HashedIdentity
from safent_ads.crm.domain.lead_attribution import AttributionRung
from safent_ads.shared.ids import BusinessId, EntityLevel, EntityRef, PlatformCode

_BUSINESS_ID = BusinessId.new()
_ENTITY = EntityRef(platform=PlatformCode.GOOGLE, level=EntityLevel.CAMPAIGN, external_id="c1")
_NOW = datetime(2026, 9, 9, tzinfo=UTC)


def _signal(**overrides: object) -> ConversionSignal:
    defaults: dict[str, object] = {
        "business_id": _BUSINESS_ID,
        "conversion_kind": ConversionKind.LEAD,
        "value_minor": 1_500,
        "occurred_at": _NOW,
        "observed_at": _NOW,
    }
    defaults.update(overrides)
    return ConversionSignal(**defaults)  # type: ignore[arg-type]


@pytest.fixture
def resolver() -> AttributionResolver:
    return AttributionResolver()


def test_prefers_click_id_over_every_other_rung(resolver: AttributionResolver) -> None:
    signal = _signal(gclid="g1", utm_campaign="camp1")

    attribution = resolver.resolve(
        signal,
        click_id_to_entity={"g1": _ENTITY},
        utm_campaign_to_entity={"camp1": _ENTITY},
        hashed_identity_to_entity={},
    )

    assert attribution.attribution_rung == AttributionRung.CLICK_ID
    assert attribution.entity_ref == _ENTITY


def test_falls_back_to_utm_when_click_id_unknown(resolver: AttributionResolver) -> None:
    signal = _signal(gclid="unknown-gclid", utm_campaign="camp1")

    attribution = resolver.resolve(
        signal,
        click_id_to_entity={},
        utm_campaign_to_entity={"camp1": _ENTITY},
        hashed_identity_to_entity={},
    )

    assert attribution.attribution_rung == AttributionRung.UTM


def test_falls_back_to_hashed_identity_when_no_click_id_or_utm(
    resolver: AttributionResolver,
) -> None:
    identity = HashedIdentity.from_digest(business_id=_BUSINESS_ID, digest="d1")
    signal = _signal(hashed_identity=identity)

    attribution = resolver.resolve(
        signal,
        click_id_to_entity={},
        utm_campaign_to_entity={},
        hashed_identity_to_entity={identity: _ENTITY},
    )

    assert attribution.attribution_rung == AttributionRung.HASHED_IDENTITY
    assert attribution.entity_ref == _ENTITY


def test_falls_back_to_aggregate_when_nothing_matches(resolver: AttributionResolver) -> None:
    signal = _signal()

    attribution = resolver.resolve(
        signal, click_id_to_entity={}, utm_campaign_to_entity={}, hashed_identity_to_entity={}
    )

    assert attribution.attribution_rung == AttributionRung.AGGREGATE
    assert attribution.entity_ref is None
    assert attribution.is_attributed_to_entity is False

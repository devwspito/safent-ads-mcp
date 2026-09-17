"""`GetConversionBridgeHealth` (T160): WhatsApp y llamada como conversion,
¿siguen llegando?"""

from __future__ import annotations

from datetime import UTC, datetime

from safent_ads.crm.domain.conversion_kind import ConversionKind
from safent_ads.crm.domain.hashed_identity import HashedIdentity
from safent_ads.crm.domain.lead_attribution import (
    AttributionRung,
    LeadAttribution,
    LeadAttributionId,
)
from safent_ads.crm.testing.in_memory_repositories import InMemoryLeadAttributionRepository
from safent_ads.economics.application.get_conversion_bridge_health import (
    GetConversionBridgeHealth,
)
from safent_ads.shared.ids import BusinessId

_BUSINESS_ID = BusinessId.new()
_AS_OF = datetime(2026, 3, 1, 12, 0, tzinfo=UTC)


def _identity(raw: str) -> HashedIdentity:
    return HashedIdentity.compute(business_id=_BUSINESS_ID, raw_identifier=raw, salt="s")


def _event(raw: str, kind: ConversionKind, occurred_at: datetime) -> LeadAttribution:
    return LeadAttribution(
        lead_attribution_id=LeadAttributionId.new(),
        business_id=_BUSINESS_ID,
        hashed_identity=_identity(raw),
        entity_ref=None,
        attribution_rung=AttributionRung.AGGREGATE,
        conversion_kind=kind,
        value_minor=0,
        occurred_at=occurred_at,
        observed_at=occurred_at,
    )


async def test_a_recent_event_is_healthy() -> None:
    lead_attributions = InMemoryLeadAttributionRepository()
    await lead_attributions.save(
        _event("a@x.com", ConversionKind.WHATSAPP, datetime(2026, 3, 1, 8, 0, tzinfo=UTC))
    )

    use_case = GetConversionBridgeHealth(lead_attributions)
    rows = await use_case.execute(business_id=_BUSINESS_ID, as_of=_AS_OF)

    whatsapp = next(row for row in rows if row.action == "whatsapp")
    assert whatsapp.healthy is True
    assert whatsapp.daily_count == 1


async def test_a_stale_bridge_is_unhealthy() -> None:
    lead_attributions = InMemoryLeadAttributionRepository()
    await lead_attributions.save(
        _event("a@x.com", ConversionKind.CALL, datetime(2026, 2, 1, 8, 0, tzinfo=UTC))
    )

    use_case = GetConversionBridgeHealth(lead_attributions)
    rows = await use_case.execute(business_id=_BUSINESS_ID, as_of=_AS_OF)

    call = next(row for row in rows if row.action == "call")
    assert call.healthy is False


async def test_a_bridge_with_no_events_ever_is_unhealthy() -> None:
    use_case = GetConversionBridgeHealth(InMemoryLeadAttributionRepository())

    rows = await use_case.execute(business_id=_BUSINESS_ID, as_of=_AS_OF)

    assert all(row.healthy is False for row in rows)
    assert all(row.last_event_at is None for row in rows)

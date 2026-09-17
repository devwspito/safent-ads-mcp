"""`CrmBridgeHealth.evaluate` (spec 027 A-3): el estado del conector solo
puede EMPEORAR la salud calculada desde los hechos, nunca mejorarla."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from safent_ads.crm.domain.bridge_health import ConnectorBridgeState, CrmBridgeHealth
from safent_ads.shared.ids import BusinessId

_AS_OF = datetime(2026, 3, 10, 12, 0, tzinfo=UTC)


def _evaluate(
    *, connector_state: ConnectorBridgeState, last_event_at: datetime | None
) -> CrmBridgeHealth:
    return CrmBridgeHealth.evaluate(
        business_id=BusinessId.new(),
        connector_id="connector-crm",
        connector_state=connector_state,
        last_event_at=last_event_at,
        as_of=_AS_OF,
        cause=None,
    )


def test_ready_connector_with_recent_events_is_healthy() -> None:
    health = _evaluate(
        connector_state=ConnectorBridgeState.READY, last_event_at=_AS_OF - timedelta(hours=1)
    )

    assert health.has_recent_events_24h is True


def test_ready_connector_with_stale_events_is_unhealthy() -> None:
    health = _evaluate(
        connector_state=ConnectorBridgeState.READY, last_event_at=_AS_OF - timedelta(hours=25)
    )

    assert health.has_recent_events_24h is False


def test_degraded_connector_cannot_self_report_healthy_even_with_recent_events() -> None:
    health = _evaluate(
        connector_state=ConnectorBridgeState.DEGRADED, last_event_at=_AS_OF - timedelta(minutes=5)
    )

    assert health.has_recent_events_24h is False


def test_suspended_connector_is_unhealthy_regardless_of_events() -> None:
    health = _evaluate(
        connector_state=ConnectorBridgeState.SUSPENDED, last_event_at=_AS_OF - timedelta(minutes=1)
    )

    assert health.has_recent_events_24h is False


def test_no_events_at_all_is_unhealthy() -> None:
    health = _evaluate(connector_state=ConnectorBridgeState.READY, last_event_at=None)

    assert health.has_recent_events_24h is False

"""`ConversionReconciliation`/`SignalContradicted` (tasks.md T116): la
plataforma reclama lo que el CRM desmiente, con volumen minimo para no
juzgar sobre ruido."""

from __future__ import annotations

from datetime import UTC, date, datetime

import pytest

from safent_ads.metrics.domain.reconciliation import ConversionReconciliation, SignalContradicted
from safent_ads.shared.ids import BusinessId, EntityLevel, EntityRef, PlatformCode

_ENTITY = EntityRef(platform=PlatformCode.GOOGLE, level=EntityLevel.CAMPAIGN, external_id="c1")
_WINDOW_START = date(2026, 8, 1)
_WINDOW_END = date(2026, 8, 7)


def _reconciliation(*, platform_conversions: int, crm_conversions: int) -> ConversionReconciliation:
    return ConversionReconciliation(
        entity_ref=_ENTITY,
        window_start=_WINDOW_START,
        window_end=_WINDOW_END,
        platform_conversions=platform_conversions,
        crm_conversions=crm_conversions,
    )


def test_platform_claim_disproved_when_crm_confirms_far_less() -> None:
    reconciliation = _reconciliation(platform_conversions=10, crm_conversions=1)

    assert reconciliation.is_platform_claim_disproved is True


def test_not_disproved_when_crm_roughly_confirms_platform() -> None:
    reconciliation = _reconciliation(platform_conversions=10, crm_conversions=8)

    assert reconciliation.is_platform_claim_disproved is False


def test_not_disproved_below_minimum_volume() -> None:
    # 0/4 seria una razon de 0 -- pero el volumen no llega al minimo para juzgar.
    reconciliation = _reconciliation(platform_conversions=4, crm_conversions=0)

    assert reconciliation.is_platform_claim_disproved is False


def test_not_disproved_when_platform_reported_nothing() -> None:
    reconciliation = _reconciliation(platform_conversions=0, crm_conversions=0)

    assert reconciliation.is_platform_claim_disproved is False


@pytest.mark.parametrize("platform_conversions,crm_conversions", [(-1, 0), (0, -1)])
def test_rejects_negative_counts(platform_conversions: int, crm_conversions: int) -> None:
    with pytest.raises(ValueError, match=">= 0"):
        _reconciliation(platform_conversions=platform_conversions, crm_conversions=crm_conversions)


def test_rejects_inverted_window() -> None:
    with pytest.raises(ValueError, match="window_start"):
        ConversionReconciliation(
            entity_ref=_ENTITY,
            window_start=_WINDOW_END,
            window_end=_WINDOW_START,
            platform_conversions=1,
            crm_conversions=1,
        )


def test_signal_contradicted_carries_the_reconciliation_that_triggered_it() -> None:
    reconciliation = _reconciliation(platform_conversions=10, crm_conversions=1)
    business_id = BusinessId.new()

    event = SignalContradicted(
        business_id=business_id,
        occurred_at=datetime(2026, 8, 10, tzinfo=UTC),
        signal_id="sig-1",
        entity_ref=_ENTITY,
        account_id="acc-1",
        rule_code="M05",
        reconciliation=reconciliation,
    )

    assert event.business_id == business_id
    assert event.reconciliation is reconciliation

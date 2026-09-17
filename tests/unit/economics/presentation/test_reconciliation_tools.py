"""`build_reconciliation_tool_specs` (T159/T160): las 5 herramientas
resuelven contra dobles en memoria y ninguna es una escritura (contracts/
mcp-tools.md regla 2: verbo primero)."""

from __future__ import annotations

from datetime import UTC, date, datetime

from safent_ads.crm.testing.in_memory_repositories import InMemoryLeadAttributionRepository
from safent_ads.economics.application.compare_attribution_windows import (
    CompareAttributionWindows,
)
from safent_ads.economics.application.get_conversion_bridge_health import (
    GetConversionBridgeHealth,
)
from safent_ads.economics.application.get_crm_reconciliation import GetCrmReconciliation
from safent_ads.economics.presentation import args as a
from safent_ads.economics.presentation.reconciliation_tools import (
    build_reconciliation_tool_specs,
)
from safent_ads.mcp.domain.tool_naming import (
    is_forbidden_decision_verb,
    is_proposal_verb,
    is_read_verb,
)
from safent_ads.metrics.testing.in_memory_metric_fact_repository import (
    InMemoryMetricFactRepository,
)
from safent_ads.shared.ids import BusinessId

_BUSINESS_ID = BusinessId.new()


def _build_specs() -> dict[str, object]:
    lead_attributions = InMemoryLeadAttributionRepository()
    metrics = InMemoryMetricFactRepository()
    specs = build_reconciliation_tool_specs(
        crm_reconciliation=GetCrmReconciliation(lead_attributions, metrics),
        compare_windows=CompareAttributionWindows(lead_attributions, metrics),
        bridge_health=GetConversionBridgeHealth(lead_attributions),
    )
    return {spec.name: spec for spec in specs}


def test_ninguna_de_las_cinco_escribe() -> None:
    specs = _build_specs()

    assert len(specs) == 5
    for name in specs:
        assert is_read_verb(name), f"{name} deberia ser un verbo de lectura"
        assert not is_proposal_verb(name)
        assert not is_forbidden_decision_verb(name)


async def test_build_tracking_template_handler() -> None:
    specs = _build_specs()
    args = a.BuildTrackingTemplateArgs(
        business_id=str(_BUSINESS_ID.value), platform="google", campaign_ref="c1"
    )

    result = await specs["build_tracking_template"].handler(args)  # type: ignore[attr-defined]

    assert result["utm"]["utm_source"] == "google"


async def test_validate_utm_consistency_without_a_wired_source_reports_nothing() -> None:
    specs = _build_specs()
    args = a.ValidateUtmConsistencyArgs(
        business_id=str(_BUSINESS_ID.value), account_ref="google:act_1"
    )

    result = await specs["validate_utm_consistency"].handler(args)  # type: ignore[attr-defined]

    assert result == {"findings": []}


async def test_get_crm_reconciliation_handler() -> None:
    specs = _build_specs()
    args = a.GetCrmReconciliationArgs(
        business_id=str(_BUSINESS_ID.value),
        window_start=date(2026, 1, 1),
        window_end=date(2026, 1, 8),
    )

    result = await specs["get_crm_reconciliation"].handler(args)  # type: ignore[attr-defined]

    assert result == {
        "platform_conversions": 0,
        "crm_conversions": 0,
        "lag_days": 0,
        "gap_pct": 0.0,
        # Spec 027 T017: `None` porque `_build_specs()` no pasa
        # `crm_bridge_health` -- ningun puente configurado que preguntar.
        "customer_bridge_healthy": None,
    }


async def test_compare_attribution_windows_handler() -> None:
    specs = _build_specs()
    args = a.CompareAttributionWindowsArgs(
        business_id=str(_BUSINESS_ID.value),
        entity_ref="google:campaign:c1",
        as_of=date(2026, 3, 1),
    )

    result = await specs["compare_attribution_windows"].handler(args)  # type: ignore[attr-defined]

    assert [row["window_days"] for row in result["windows"]] == [1, 7, 28]


async def test_get_conversion_bridge_health_handler() -> None:
    specs = _build_specs()
    args = a.GetConversionBridgeHealthArgs(
        business_id=str(_BUSINESS_ID.value), as_of=datetime(2026, 3, 1, tzinfo=UTC)
    )

    result = await specs["get_conversion_bridge_health"].handler(args)  # type: ignore[attr-defined]

    assert {row["action"] for row in result["bridges"]} == {"whatsapp", "call"}

"""`GetCapabilities`: nunca lanza por credencial ausente -- la ausencia se
reporta (tool-surface.md §0: "la capacidad se declara, nunca se finge")."""

from __future__ import annotations

from dataclasses import asdict
from datetime import UTC, datetime

from safent_ads.mcp.application.get_capabilities import GetCapabilities
from safent_ads.mcp.presentation.read_model_ports import ReadModelPorts
from safent_ads.mcp.testing.fakes import (
    BUSINESS_A,
    FakeAuditReadPort,
    FakeBrandReadPort,
    FakeBusinessDirectory,
    FakeCapabilityReadPort,
    FakeCatalogReadPort,
    FakeCreativeReadPort,
    FakeEntityReadPort,
    FakeGaqlPort,
    FakePortfolioReadPort,
    FakeProposalReadPort,
    FakeRuleReadPort,
    FakeSignalReadPort,
)
from safent_ads.shared.clock import FixedClock

_NOW = FixedClock(datetime(2026, 9, 9, 10, 0, tzinfo=UTC))


def _build_ports(*, configured_keys: frozenset[str] = frozenset()) -> ReadModelPorts:
    return ReadModelPorts(
        business_directory=FakeBusinessDirectory(),
        portfolio=FakePortfolioReadPort(),
        entity=FakeEntityReadPort(),
        gaql=FakeGaqlPort(),
        signal=FakeSignalReadPort(),
        rule=FakeRuleReadPort(),
        proposal=FakeProposalReadPort(),
        catalog=FakeCatalogReadPort(),
        audit=FakeAuditReadPort(),
        creative=FakeCreativeReadPort(),
        brand=FakeBrandReadPort(),
        capability=FakeCapabilityReadPort(configured_keys),
    )


async def test_reports_no_platform_writes_available() -> None:
    use_case = GetCapabilities(_build_ports(), _NOW)

    report = await use_case.execute(BUSINESS_A)

    assert all(not write.writes_available for write in report.platform_writes)


async def test_reports_missing_credentials_without_raising_when_nothing_is_configured() -> None:
    """Cero claves BYOK configuradas y ninguna cuenta de plataforma activa
    -- el caso de uso completa igual, listando el hueco."""
    use_case = GetCapabilities(_build_ports(configured_keys=frozenset()), _NOW)

    report = await use_case.execute(BUSINESS_A)

    assert report.missing_credentials != []
    assert not any("FAL_KEY" in item for item in report.missing_credentials)
    assert "FAL_KEY" in report.optional_missing_credentials
    assert all(not backend.configured for backend in report.generation_backends)


async def test_reports_configured_backend_when_key_is_present() -> None:
    use_case = GetCapabilities(_build_ports(configured_keys=frozenset({"FAL_KEY"})), _NOW)

    report = await use_case.execute(BUSINESS_A)

    image_backend = next(b for b in report.generation_backends if b.capability == "image")
    assert image_backend.configured is True
    assert image_backend.backend == "fal"
    assert not any("FAL_KEY" in item for item in report.missing_credentials)
    assert "FAL_KEY" not in report.optional_missing_credentials


async def test_report_distinguishes_proposals_from_direct_writes_and_optional_services() -> None:
    report = await GetCapabilities(_build_ports(), _NOW).execute(BUSINESS_A)
    payload = asdict(report)

    assert all(w["scope"] == "direct_platform_writes" for w in payload["platform_writes"])
    assert payload["autonomy"]["scope"] == "automatic_defensive_rule_execution"
    assert all(b["optional"] for b in payload["generation_backends"])
    assert all(not b["required_for_campaign_drafts"] for b in payload["generation_backends"])
    assert "propose_*" in payload["interpretation"][0]
    assert "does not certify" in payload["interpretation"][1]
    assert set(payload["optional_missing_credentials"]) == {
        "FAL_KEY",
        "ELEVENLABS_API_KEY",
        "BRAVE_API_KEY",
    }


async def test_generated_at_comes_from_the_clock() -> None:
    use_case = GetCapabilities(_build_ports(), _NOW)

    report = await use_case.execute(BUSINESS_A)

    assert report.generated_at == _NOW.now()

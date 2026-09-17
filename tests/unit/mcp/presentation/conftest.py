from __future__ import annotations

import pytest

from safent_ads.mcp.presentation.catalog import build_default_registry
from safent_ads.mcp.presentation.read_model_ports import ReadModelPorts
from safent_ads.mcp.presentation.registry import ToolRegistry
from safent_ads.mcp.testing.fakes import (
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
from safent_ads.shared.clock import SystemClock


@pytest.fixture
def read_model_ports() -> ReadModelPorts:
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
        capability=FakeCapabilityReadPort(),
    )


@pytest.fixture
def registry(read_model_ports: ReadModelPorts) -> ToolRegistry:
    return build_default_registry(read_model_ports, SystemClock())

"""`GetProjectContext`: el paquete que responde a "hazme unos banners para
una campana de Display de X" en una sola llamada -- ensamblado desde
`ReadModelPorts` ya existentes, sin PII y bajo el limite de tamano."""

from __future__ import annotations

import dataclasses
import json
import re
from datetime import UTC, datetime

import pytest

from safent_ads.mcp.application.dto import (
    AutonomyStatus,
    BrandAssetSummary,
    BrandKitSummary,
    BusinessSummary,
    CalendarEventDetail,
    CalendarEventSummary,
    Cause,
    Evidence,
    GuardrailInfo,
    InventorySummary,
    OfferingSummary,
    PlatformAccountSummary,
    ProjectContextPack,
    ProposalSummary,
    ProposedDiff,
    SignalSummary,
    TopMover,
)
from safent_ads.mcp.application.errors import EntityNotFoundError
from safent_ads.mcp.application.get_project_context import GetProjectContext
from safent_ads.mcp.domain.context_pack import MAX_PAYLOAD_BYTES
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
from safent_ads.shared.read_models.dto import Freshness

_NOW = FixedClock(datetime(2026, 9, 9, 10, 0, tzinfo=UTC))

# threat-model.md C-31 / tests/integration/migrations/test_catalog_crm.py:
# mismo vocabulario de dato personal, aplicado a nombres de campo de DTO.
_PII_FIELD_PATTERN = re.compile(
    r"(email|mail|phone|telefono|movil|apellido|dni|nif|nie|"
    r"passport|address|direccion|postal|birth|nacimiento|ip_address)",
    re.IGNORECASE,
)

# Todo DTO alcanzable desde `ProjectContextPack` (dto.py): lista cerrada a
# proposito para que anadir un campo nuevo obligue a revisarla.
_REACHABLE_DTOS = (
    ProjectContextPack,
    BusinessSummary,
    PlatformAccountSummary,
    CalendarEventDetail,
    CalendarEventSummary,
    InventorySummary,
    TopMover,
    SignalSummary,
    Cause,
    ProposalSummary,
    ProposedDiff,
    Evidence,
    AutonomyStatus,
    GuardrailInfo,
    BrandKitSummary,
    OfferingSummary,
    Freshness,
)


def _build_ports(*, brand=None) -> ReadModelPorts:
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
        brand=brand or FakeBrandReadPort(),
        capability=FakeCapabilityReadPort(),
    )


class _NoBrandKitReadPort:
    async def get_brand_kit(self, business_id: str) -> BrandAssetSummary:  # pragma: no cover
        raise EntityNotFoundError(f"sin kit de marca para {business_id}")

    async def list_brand_assets(self, business_id: str, *, kind: str | None) -> list:
        del business_id, kind
        return []


async def test_returns_a_shape_with_every_documented_block() -> None:
    use_case = GetProjectContext(_build_ports(), _NOW)

    payload = await use_case.execute(BUSINESS_A)

    assert payload["business"]["business_id"] == BUSINESS_A
    for key in (
        "platform_accounts",
        "open_calendar_events",
        "inventory",
        "top_performers",
        "bottom_performers",
        "signals",
        "open_proposals",
        "autonomy",
        "guardrails",
        "brand_kit",
        "offerings",
        "freshness",
        "capability_notes",
        "generated_at",
    ):
        assert key in payload, key


async def test_raises_when_business_is_unknown() -> None:
    use_case = GetProjectContext(_build_ports(), _NOW)

    with pytest.raises(EntityNotFoundError):
        await use_case.execute("99999999-9999-9999-9999-999999999999")


async def test_missing_brand_kit_is_reported_not_faked() -> None:
    use_case = GetProjectContext(_build_ports(brand=_NoBrandKitReadPort()), _NOW)

    payload = await use_case.execute(BUSINESS_A)

    assert payload["brand_kit"] is None
    assert any("kit de marca" in note for note in payload["capability_notes"])


async def test_payload_has_no_pii_field_names() -> None:
    offending: list[str] = []
    for dto_class in _REACHABLE_DTOS:
        for field in dataclasses.fields(dto_class):
            if _PII_FIELD_PATTERN.search(field.name):
                offending.append(f"{dto_class.__name__}.{field.name}")

    assert offending == []


async def test_payload_stays_under_the_size_cap() -> None:
    use_case = GetProjectContext(_build_ports(), _NOW)

    payload = await use_case.execute(BUSINESS_A)

    size = len(json.dumps(payload, default=str, ensure_ascii=False).encode("utf-8"))
    assert size <= MAX_PAYLOAD_BYTES

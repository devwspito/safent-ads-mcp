"""Agrupa los puertos de lectura que la lane necesita cablear
(`mcp/application/ports.py`) en un unico objeto de construccion para
`build_default_registry` (T045). La integracion sustituye cada campo por el
adaptador real; `mcp/testing/fakes.py` los sustituye en los tests."""

from __future__ import annotations

from dataclasses import dataclass

from safent_ads.mcp.application.ports import (
    AuditReadPort,
    BrandReadPort,
    BusinessDirectoryPort,
    CapabilityReadPort,
    CatalogReadPort,
    CreativeReadPort,
    EntityReadPort,
    GaqlPort,
    PortfolioReadPort,
    ProposalReadPort,
    RuleReadPort,
    SignalReadPort,
)


@dataclass(frozen=True, slots=True)
class ReadModelPorts:
    business_directory: BusinessDirectoryPort
    portfolio: PortfolioReadPort
    entity: EntityReadPort
    gaql: GaqlPort
    signal: SignalReadPort
    rule: RuleReadPort
    proposal: ProposalReadPort
    catalog: CatalogReadPort
    audit: AuditReadPort
    creative: CreativeReadPort
    brand: BrandReadPort
    capability: CapabilityReadPort

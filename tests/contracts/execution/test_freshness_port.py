"""Contrato de `FreshnessPort`: identico para el doble en memoria y para
`SqlFreshnessPort`."""

from __future__ import annotations

from tests.contracts.execution.conftest import FreshnessFixture


async def test_recent_ingestion_is_not_stale(freshness: FreshnessFixture) -> None:
    entity_ref = await freshness.given_fresh_entity()
    assert not await freshness.freshness.is_stale(entity_ref)


async def test_late_ingestion_is_stale(freshness: FreshnessFixture) -> None:
    entity_ref = await freshness.given_stale_entity()
    assert await freshness.freshness.is_stale(entity_ref)


async def test_never_ingested_is_stale(freshness: FreshnessFixture) -> None:
    """Denegar por defecto: "no se ha ingestado nada" no puede leerse como
    "los datos estan al dia"."""
    entity_ref = await freshness.given_never_ingested_entity()
    assert await freshness.freshness.is_stale(entity_ref)

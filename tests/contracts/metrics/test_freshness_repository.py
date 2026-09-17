"""Contrato de `SqlFreshnessRepository` (metrics/application/ports.py::
FreshnessRepository), la contraparte de escritura de
`execution.infrastructure.sql_freshness.SqlFreshnessPort`, que solo lee
`data_freshness`. Sin doble en memoria: ningun camino de produccion
necesita evaluar frescura sin Postgres real de por medio."""

from __future__ import annotations

from datetime import UTC, datetime

import pytest
from sqlalchemy import text

from safent_ads.metrics.domain.freshness import Freshness
from safent_ads.metrics.infrastructure.errors import UnknownAccountRefError
from safent_ads.metrics.infrastructure.sql_repositories import SqlFreshnessRepository
from safent_ads.shared.ids import EntityLevel
from tests.conftest import rolled_back_session
from tests.contracts.sql_fixtures import account_external_id, campaign_ref, seed_entity

pytestmark = pytest.mark.integration

_LAST_INGESTED_AT = datetime(2026, 9, 9, 9, 0, tzinfo=UTC)


async def test_save_is_observable_in_data_freshness(database_url: str) -> None:
    """Regresion GAP 1: antes de este fix, ningun codigo de produccion
    escribia `data_freshness` -- `ComputeFreshness.execute()` devolvia un
    `Freshness` en memoria sin adaptador `Sql*` que lo persistiera."""
    async with rolled_back_session(database_url) as session:
        entity_ref = campaign_ref("frescura-nueva")
        await seed_entity(session, entity_ref)
        account_ref = f"{entity_ref.platform.value}:{account_external_id(entity_ref)}"
        freshness = Freshness(
            platform_account_ref=account_ref,
            entity_level=EntityLevel.CAMPAIGN,
            last_ingested_at=_LAST_INGESTED_AT,
        )

        await SqlFreshnessRepository(session).save(freshness)

        row = (
            await session.execute(
                text(
                    """
                    SELECT freshness.last_ingested_at
                      FROM data_freshness AS freshness
                      JOIN platform_accounts AS account
                        ON account.id = freshness.platform_account_id
                     WHERE account.platform = :platform
                       AND account.external_account_id = :external_account_id
                       AND freshness.entity_level = :entity_level
                    """
                ),
                {
                    "platform": entity_ref.platform.value,
                    "external_account_id": account_external_id(entity_ref),
                    "entity_level": EntityLevel.CAMPAIGN.value,
                },
            )
        ).mappings().one()
    assert row["last_ingested_at"] == _LAST_INGESTED_AT


async def test_save_again_updates_instead_of_duplicating(database_url: str) -> None:
    """Reingerir (mismo ciclo repetido, o el siguiente) actualiza la fila en
    vez de duplicarla: la PK es `(platform_account_id, entity_level,
    granularity)`, no un id propio."""
    async with rolled_back_session(database_url) as session:
        entity_ref = campaign_ref("frescura-repetida")
        await seed_entity(session, entity_ref)
        account_ref = f"{entity_ref.platform.value}:{account_external_id(entity_ref)}"
        repository = SqlFreshnessRepository(session)
        await repository.save(
            Freshness(
                platform_account_ref=account_ref,
                entity_level=EntityLevel.CAMPAIGN,
                last_ingested_at=_LAST_INGESTED_AT,
            )
        )
        later = datetime(2026, 9, 9, 10, 0, tzinfo=UTC)

        await repository.save(
            Freshness(
                platform_account_ref=account_ref,
                entity_level=EntityLevel.CAMPAIGN,
                last_ingested_at=later,
            )
        )

        count_and_latest = (
            await session.execute(
                text(
                    """
                    SELECT count(*) AS total, max(freshness.last_ingested_at) AS latest
                      FROM data_freshness AS freshness
                      JOIN platform_accounts AS account
                        ON account.id = freshness.platform_account_id
                     WHERE account.platform = :platform
                       AND account.external_account_id = :external_account_id
                       AND freshness.entity_level = :entity_level
                    """
                ),
                {
                    "platform": entity_ref.platform.value,
                    "external_account_id": account_external_id(entity_ref),
                    "entity_level": EntityLevel.CAMPAIGN.value,
                },
            )
        ).mappings().one()
    assert count_and_latest["total"] == 1
    assert count_and_latest["latest"] == later


async def test_save_rejects_unknown_account(database_url: str) -> None:
    async with rolled_back_session(database_url) as session:
        freshness = Freshness(
            platform_account_ref="google:no-existe",
            entity_level=EntityLevel.CAMPAIGN,
            last_ingested_at=_LAST_INGESTED_AT,
        )

        with pytest.raises(UnknownAccountRefError):
            await SqlFreshnessRepository(session).save(freshness)

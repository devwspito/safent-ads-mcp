"""0003_ad_entities: la jerarquia la sostiene la base, no la aplicacion."""

from __future__ import annotations

import asyncpg
import pytest

from tests.integration.migrations.conftest import (
    make_business,
    make_entity,
    make_platform_account,
    state_hash,
)

pytestmark = pytest.mark.integration


async def test_parent_level_violation_raises(pg: asyncpg.Connection) -> None:
    business_id = await make_business(pg)
    account_id = await make_platform_account(pg, business_id)
    campaign = await make_entity(pg, business_id, account_id)

    with pytest.raises(asyncpg.CheckViolationError, match="ad_entities_parent_level_check"):
        await make_entity(pg, business_id, account_id, level="ad", parent_id=campaign["id"])


async def test_valid_hierarchy_fills_parent_level(pg: asyncpg.Connection) -> None:
    business_id = await make_business(pg)
    account_id = await make_platform_account(pg, business_id)

    campaign = await make_entity(pg, business_id, account_id, level="campaign")
    ad_set = await make_entity(pg, business_id, account_id, "ad_set", campaign["id"])
    ad = await make_entity(pg, business_id, account_id, "ad", ad_set["id"])
    creative = await make_entity(pg, business_id, account_id, "creative", ad["id"])

    assert campaign["parent_level"] is None
    assert ad_set["parent_level"] == "campaign"
    assert ad["parent_level"] == "ad_set"
    assert creative["parent_level"] == "ad"
    assert campaign["entity_ref"].startswith("meta:campaign:")


async def test_campaign_cannot_have_a_parent(pg: asyncpg.Connection) -> None:
    business_id = await make_business(pg)
    account_id = await make_platform_account(pg, business_id)
    campaign = await make_entity(pg, business_id, account_id)

    with pytest.raises(asyncpg.CheckViolationError, match="ad_entities_parent_level_check"):
        await make_entity(pg, business_id, account_id, "campaign", campaign["id"])


async def test_parent_from_another_account_is_rejected(pg: asyncpg.Connection) -> None:
    business_id = await make_business(pg)
    account_id = await make_platform_account(pg, business_id)
    other_account_id = await make_platform_account(pg, business_id)
    campaign = await make_entity(pg, business_id, account_id)

    with pytest.raises(asyncpg.RaiseError, match="otra cuenta o plataforma"):
        await make_entity(pg, business_id, other_account_id, "ad_set", campaign["id"])


async def test_level_is_immutable(pg: asyncpg.Connection) -> None:
    business_id = await make_business(pg)
    account_id = await make_platform_account(pg, business_id)
    campaign = await make_entity(pg, business_id, account_id)

    with pytest.raises(asyncpg.RaiseError, match="inmutable"):
        await pg.execute("UPDATE ad_entities SET level = 'ad_set' WHERE id = $1", campaign["id"])


async def test_same_external_id_twice_is_rejected(pg: asyncpg.Connection) -> None:
    business_id = await make_business(pg)
    account_id = await make_platform_account(pg, business_id)
    external_id = "dup-external-id"
    insert = """
        INSERT INTO ad_entities (business_id, platform_account_id, platform, level,
                                 external_id, name, status, platform_state_hash)
        VALUES ($1, $2, 'meta', 'campaign', $3, 'Campana', 'ACTIVE', $4)
    """
    await pg.execute(insert, business_id, account_id, external_id, state_hash(external_id))

    with pytest.raises(asyncpg.UniqueViolationError):
        await pg.execute(insert, business_id, account_id, external_id, state_hash(external_id))


async def test_drifted_partial_index_exists(pg: asyncpg.Connection) -> None:
    definition = await pg.fetchval(
        "SELECT indexdef FROM pg_indexes WHERE indexname = 'ix_ad_entities_drifted'"
    )

    assert definition is not None
    assert "WHERE (status = 'DRIFTED'::text)" in definition


async def test_parent_level_cannot_be_forged(pg: asyncpg.Connection) -> None:
    """El trigger reescribe `parent_level` con el nivel real del padre: mentir
    en el INSERT no salta el CHECK, lo provoca."""
    business_id = await make_business(pg)
    account_id = await make_platform_account(pg, business_id)
    campaign = await make_entity(pg, business_id, account_id)

    with pytest.raises(asyncpg.CheckViolationError, match="ad_entities_parent_level_check"):
        await pg.execute(
            """
            INSERT INTO ad_entities (business_id, platform_account_id, platform, level,
                                     external_id, parent_id, parent_level, name, status,
                                     platform_state_hash)
            VALUES ($1, $2, 'meta', 'ad', 'forjado', $3, 'ad_set', 'Anuncio', 'ACTIVE', $4)
            """,
            business_id,
            account_id,
            campaign["id"],
            state_hash("forjado"),
        )

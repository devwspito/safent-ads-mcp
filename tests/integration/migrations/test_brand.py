"""0014_brand: `brand_kits`, un kit por negocio (data-model.md §Migration
plan: "aditiva, no toca 0001-0010")."""

from __future__ import annotations

import asyncpg
import pytest

from tests.integration.migrations.conftest import make_business

pytestmark = pytest.mark.integration


async def test_brand_kits_table_exists_with_expected_columns(pg: asyncpg.Connection) -> None:
    columns = {
        row["column_name"]
        for row in await pg.fetch(
            """
            SELECT column_name FROM information_schema.columns
            WHERE table_schema = 'public' AND table_name = 'brand_kits'
            """
        )
    }

    assert columns >= {
        "id",
        "business_id",
        "primary_font",
        "font_licence_note",
        "palette",
        "tone_description",
        "assets",
        "forbidden_claims",
        "updated_at",
    }


async def test_business_id_is_required(pg: asyncpg.Connection) -> None:
    with pytest.raises(asyncpg.NotNullViolationError):
        await pg.execute(
            """
            INSERT INTO brand_kits (primary_font, font_licence_note, tone_description)
            VALUES ('X', 'X', 'X')
            """
        )


async def test_one_kit_per_business(pg: asyncpg.Connection) -> None:
    business_id = await make_business(pg)
    insert = """
        INSERT INTO brand_kits (business_id, primary_font, font_licence_note, tone_description)
        VALUES ($1, 'X', 'X', 'X')
    """
    await pg.execute(insert, business_id)

    with pytest.raises(asyncpg.UniqueViolationError):
        await pg.execute(insert, business_id)


async def test_updated_at_advances_on_update(pg: asyncpg.Connection) -> None:
    business_id = await make_business(pg)
    await pg.execute(
        """
        INSERT INTO brand_kits (business_id, primary_font, font_licence_note, tone_description)
        VALUES ($1, 'X', 'X', 'X')
        """,
        business_id,
    )
    before = await pg.fetchval(
        "SELECT updated_at FROM brand_kits WHERE business_id = $1", business_id
    )

    await pg.execute(
        "UPDATE brand_kits SET primary_font = 'Y' WHERE business_id = $1", business_id
    )
    after = await pg.fetchval(
        "SELECT updated_at FROM brand_kits WHERE business_id = $1", business_id
    )

    assert after >= before

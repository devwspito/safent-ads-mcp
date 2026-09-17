"""0016_brand_confirmed_website_host: `brand_kits.confirmed_website_host`
(F-8, checklists/website-brand-extractor-review.md)."""

from __future__ import annotations

import asyncpg
import pytest

from tests.integration.migrations.conftest import make_business

pytestmark = pytest.mark.integration


async def test_confirmed_website_host_column_exists_and_is_nullable(
    pg: asyncpg.Connection,
) -> None:
    row = await pg.fetchrow(
        """
        SELECT is_nullable FROM information_schema.columns
        WHERE table_schema = 'public' AND table_name = 'brand_kits'
          AND column_name = 'confirmed_website_host'
        """
    )

    assert row is not None
    assert row["is_nullable"] == "YES"


async def test_confirmed_website_host_defaults_to_null(pg: asyncpg.Connection) -> None:
    business_id = await make_business(pg)
    await pg.execute(
        """
        INSERT INTO brand_kits (business_id, primary_font, font_licence_note, tone_description)
        VALUES ($1, 'X', 'X', 'X')
        """,
        business_id,
    )

    host = await pg.fetchval(
        "SELECT confirmed_website_host FROM brand_kits WHERE business_id = $1", business_id
    )

    assert host is None


async def test_confirmed_website_host_stores_a_value(pg: asyncpg.Connection) -> None:
    business_id = await make_business(pg)
    await pg.execute(
        """
        INSERT INTO brand_kits
            (business_id, primary_font, font_licence_note, tone_description,
             confirmed_website_host)
        VALUES ($1, 'X', 'X', 'X', 'example-business.test')
        """,
        business_id,
    )

    host = await pg.fetchval(
        "SELECT confirmed_website_host FROM brand_kits WHERE business_id = $1", business_id
    )

    assert host == "example-business.test"

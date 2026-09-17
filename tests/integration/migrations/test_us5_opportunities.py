"""0027_us5_opportunities: `proposals.entity_ref` acepta el ambito de
cuenta (`<platform>:account:<external_account_id>`) validado contra
`platform_accounts`, sin tocar `ad_entities` -- `ad_entities` sigue
rechazando cualquier fila que no sea campaign/ad_set/ad/creative."""

from __future__ import annotations

import uuid

import asyncpg
import pytest

from tests.integration.migrations.conftest import make_business, make_platform_account

pytestmark = pytest.mark.integration

_NOT_FOUND_MATCH = "no existe en ad_entities ni platform_accounts"

_INSERT_PROPOSAL = """
    INSERT INTO proposals (
        id, business_id, entity_ref, parameter, current_value, proposed_value, diff_hash,
        classification, cause_key, cause, estimated_impact_amount, estimated_impact_currency,
        urgency, expires_at
    )
    VALUES (
        $1, $2, $3, 'new_campaign:test', '{"type":"json","value":null}'::jsonb,
        '{"type":"json","value":{}}'::jsonb, $4, 'important', 'opportunity_candidate:test:x',
        'causa de prueba', 0, 'EUR', 'minor', now() + interval '1 day'
    )
"""
_FAKE_DIFF_HASH = "a" * 64


async def _insert_proposal(
    pg: asyncpg.Connection, *, business_id: uuid.UUID, entity_ref: str
) -> uuid.UUID:
    proposal_id = uuid.uuid4()
    await pg.execute(_INSERT_PROPOSAL, proposal_id, business_id, entity_ref, _FAKE_DIFF_HASH)
    return proposal_id


async def test_account_scope_entity_ref_is_accepted_for_an_active_account(
    pg: asyncpg.Connection,
) -> None:
    business_id = await make_business(pg)
    external_account_id = f"act-{uuid.uuid4().hex[:10]}"
    await make_platform_account(pg, business_id, "google")
    await pg.execute(
        "UPDATE platform_accounts SET external_account_id = $1 "
        "WHERE business_id = $2 AND platform = 'google'",
        external_account_id,
        business_id,
    )

    proposal_id = await _insert_proposal(
        pg, business_id=business_id, entity_ref=f"google:account:{external_account_id}"
    )

    row = await pg.fetchrow("SELECT id FROM proposals WHERE id = $1", proposal_id)
    assert row is not None


async def test_account_scope_entity_ref_is_rejected_for_an_unknown_account(
    pg: asyncpg.Connection,
) -> None:
    business_id = await make_business(pg)

    with pytest.raises(asyncpg.RaiseError, match=_NOT_FOUND_MATCH):
        await _insert_proposal(
            pg, business_id=business_id, entity_ref=f"google:account:{uuid.uuid4().hex}"
        )


async def test_campaign_scope_entity_ref_still_requires_an_ad_entities_row(
    pg: asyncpg.Connection,
) -> None:
    business_id = await make_business(pg)

    with pytest.raises(asyncpg.RaiseError, match=_NOT_FOUND_MATCH):
        await _insert_proposal(
            pg, business_id=business_id, entity_ref=f"google:campaign:{uuid.uuid4().hex[:10]}"
        )


async def test_ad_entities_still_rejects_an_account_level_row(pg: asyncpg.Connection) -> None:
    """Regresion: la primera version de esta migracion insertaba en
    `ad_entities` y rompia `accounts.domain.ad_entity.AdEntity` (solo
    admite campaign/ad_set/ad/creative) -- confirma que sigue rechazado."""
    business_id = await make_business(pg)
    account_id = await make_platform_account(pg, business_id, "google")

    with pytest.raises(asyncpg.CheckViolationError, match="ad_entities_level_check"):
        await pg.execute(
            """
            INSERT INTO ad_entities (business_id, platform_account_id, platform, level,
                                     external_id, name, status, platform_state_hash)
            VALUES ($1, $2, 'google', 'account', 'act-x', 'Cuenta', 'ACTIVE', $3)
            """,
            business_id,
            account_id,
            "a" * 64,
        )

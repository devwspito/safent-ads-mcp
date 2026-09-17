"""0023_owner_settings: `businesses.active_hours_*`/`digest_hour`,
`proposals.postponed_reason`/`owner_context`, `owner_preferences`
(contracts/rest-api.md §Ajustes, §Propuestas)."""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, time, timedelta

import asyncpg
import pytest

from tests.integration.migrations.conftest import make_business, make_entity, make_platform_account

pytestmark = pytest.mark.integration

_SET_ACTIVE_HOURS = """
    UPDATE businesses SET active_hours_start = $2, active_hours_end = $3 WHERE id = $1
"""
_SET_DIGEST_HOUR = "UPDATE businesses SET digest_hour = $2 WHERE id = $1"

_INSERT_PROPOSAL = """
    INSERT INTO proposals (business_id, entity_ref, parameter, current_value, proposed_value,
                           diff_hash, classification, cause_key, cause,
                           estimated_impact_amount, estimated_impact_currency, urgency,
                           state, postponed_until, postponed_reason, owner_context, expires_at)
    VALUES ($1, $2, 'daily_budget', '{"amount": 60}'::jsonb, '{"amount": 78}'::jsonb, $3,
            'routine', 'limitada-por-presupuesto', 'Limitada por presupuesto', 310, 'EUR',
            'recommended', $4, $5, $6, $7, now() + interval '24 hours')
    RETURNING id
"""

_UPSERT_THEME = """
    INSERT INTO owner_preferences (owner_id, theme)
    VALUES ($1, $2)
    ON CONFLICT (owner_id) DO UPDATE SET theme = EXCLUDED.theme
    RETURNING theme, updated_at
"""


async def _entity(pg: asyncpg.Connection) -> tuple[uuid.UUID, str]:
    business_id = await make_business(pg)
    account_id = await make_platform_account(pg, business_id)
    entity = await make_entity(pg, business_id, account_id)
    return business_id, entity["entity_ref"]


async def _make_owner(pg: asyncpg.Connection) -> uuid.UUID:
    return await pg.fetchval(
        """
        INSERT INTO owners (email, password_hash)
        VALUES ($1, 'argon2id$fixture$not-a-real-hash')
        RETURNING id
        """,
        f"owner-{uuid.uuid4().hex[:10]}@safent.example",
    )


class TestBusinessActiveHoursAndDigestHour:
    async def test_accepts_a_valid_active_hours_pair(self, pg: asyncpg.Connection) -> None:
        business_id, _ = await _entity(pg)

        await pg.execute(_SET_ACTIVE_HOURS, business_id, time(8, 0), time(21, 0))

        row = await pg.fetchrow(
            "SELECT active_hours_start, active_hours_end FROM businesses WHERE id = $1",
            business_id,
        )
        assert row["active_hours_start"] == time(8, 0)
        assert row["active_hours_end"] == time(21, 0)

    async def test_rejects_only_one_side_of_the_pair(self, pg: asyncpg.Connection) -> None:
        business_id, _ = await _entity(pg)

        with pytest.raises(
            asyncpg.CheckViolationError, match="businesses_active_hours_pair_check"
        ):
            await pg.execute(
                "UPDATE businesses SET active_hours_start = $2 WHERE id = $1",
                business_id,
                time(8, 0),
            )

    async def test_accepts_digest_hour_in_range(self, pg: asyncpg.Connection) -> None:
        business_id, _ = await _entity(pg)

        await pg.execute(_SET_DIGEST_HOUR, business_id, 9)

        assert await pg.fetchval(
            "SELECT digest_hour FROM businesses WHERE id = $1", business_id
        ) == 9

    async def test_rejects_digest_hour_out_of_range(self, pg: asyncpg.Connection) -> None:
        business_id, _ = await _entity(pg)

        with pytest.raises(asyncpg.CheckViolationError):
            await pg.execute(_SET_DIGEST_HOUR, business_id, 24)


class TestProposalsPostponedReasonAndOwnerContext:
    async def test_postponed_state_requires_a_reason(self, pg: asyncpg.Connection) -> None:
        business_id, entity_ref = await _entity(pg)

        with pytest.raises(
            asyncpg.CheckViolationError, match="proposals_postponed_needs_reason_check"
        ):
            await pg.execute(
                _INSERT_PROPOSAL,
                business_id,
                entity_ref,
                "a" * 64,
                "postponed",
                None,
                None,
                None,
            )

    async def test_postponed_state_with_reason_and_until_is_accepted(
        self, pg: asyncpg.Connection
    ) -> None:
        business_id, entity_ref = await _entity(pg)
        postponed_until = datetime.now(UTC) + timedelta(hours=1)

        proposal_id = await pg.fetchval(
            _INSERT_PROPOSAL,
            business_id,
            entity_ref,
            "b" * 64,
            "postponed",
            postponed_until,
            "owner",
            None,
        )

        row = await pg.fetchrow(
            "SELECT postponed_reason, owner_context FROM proposals WHERE id = $1", proposal_id
        )
        assert row["postponed_reason"] == "owner"
        assert row["owner_context"] is None

    async def test_rejects_an_unknown_postponed_reason(self, pg: asyncpg.Connection) -> None:
        business_id, entity_ref = await _entity(pg)

        with pytest.raises(asyncpg.CheckViolationError):
            await pg.execute(
                _INSERT_PROPOSAL,
                business_id,
                entity_ref,
                "c" * 64,
                "pending",
                None,
                "not_a_valid_reason",
                None,
            )

    async def test_owner_context_over_500_chars_is_rejected(self, pg: asyncpg.Connection) -> None:
        business_id, entity_ref = await _entity(pg)

        with pytest.raises(asyncpg.CheckViolationError):
            await pg.execute(
                _INSERT_PROPOSAL,
                business_id,
                entity_ref,
                "d" * 64,
                "pending",
                None,
                None,
                "x" * 501,
            )

    async def test_owner_context_at_exactly_500_chars_is_accepted(
        self, pg: asyncpg.Connection
    ) -> None:
        business_id, entity_ref = await _entity(pg)

        proposal_id = await pg.fetchval(
            _INSERT_PROPOSAL,
            business_id,
            entity_ref,
            "e" * 64,
            "pending",
            None,
            None,
            "x" * 500,
        )

        assert await pg.fetchval(
            "SELECT char_length(owner_context) FROM proposals WHERE id = $1", proposal_id
        ) == 500


class TestOwnerPreferences:
    async def test_upsert_defaults_theme_to_system(self, pg: asyncpg.Connection) -> None:
        owner_id = await _make_owner(pg)

        theme = await pg.fetchval(
            "INSERT INTO owner_preferences (owner_id) VALUES ($1) RETURNING theme", owner_id
        )

        assert theme == "system"

    async def test_upsert_updates_the_theme_in_place(self, pg: asyncpg.Connection) -> None:
        owner_id = await _make_owner(pg)
        await pg.execute(_UPSERT_THEME, owner_id, "dark")

        row = await pg.fetchrow(_UPSERT_THEME, owner_id, "light")

        assert row["theme"] == "light"
        count = await pg.fetchval(
            "SELECT count(*) FROM owner_preferences WHERE owner_id = $1", owner_id
        )
        assert count == 1

    async def test_rejects_an_unknown_theme(self, pg: asyncpg.Connection) -> None:
        owner_id = await _make_owner(pg)

        with pytest.raises(asyncpg.CheckViolationError):
            await pg.execute(
                "INSERT INTO owner_preferences (owner_id, theme) VALUES ($1, 'neon')", owner_id
            )
